# -*- coding: utf-8 -*-
"""各段 manifest：状态记录与断点续跑（docs §5）。

统一 schema（每段一个 JSON，位于该段产物目录）：
  {"stage", "project", "schema": 1, "config_hash", "updated_at",
   "units": {"<unit_id>": {"status", "files", "prompt_id", "seed", "channel",
                            "inputs_hash", "upstream", "stage_cfg_hash",
                            "attempts", "error", "updated_at", ...}}}
status ∈ pending | running | done | failed | skipped | stale

完成判定（三条件同时满足）：status==done ∧ 文件存在 ∧ 大小 ≥ 段下限（图 100KB/视频 200KB）。
失效判定：inputs_hash（yaml 输入行）∨ upstream 指纹（路径|大小|mtime_ns）∨ 段配置哈希 任一变化。
原子写：tmp + os.replace，任何时刻被杀不留半个 manifest。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

__all__ = [
    "ManifestError", "SCHEMA", "stage_dir_for", "manifest_path_for",
    "load", "ensure", "save", "unit", "mark_unit",
    "fingerprint_file", "hash_text", "hash_fields",
    "unit_complete", "stale_reason", "summary",
]

SCHEMA = 1

STAGE_DIRS = {"assets": "assets", "keyframes": "keyframes", "camera": "camera",
              "video": "video", "final": "final"}


class ManifestError(RuntimeError):
    pass


def stage_dir_for(project_dir, stage: str) -> Path:
    return Path(project_dir) / STAGE_DIRS[stage]


def manifest_path_for(project_dir, stage: str) -> Path:
    return stage_dir_for(project_dir, stage) / ("%s_manifest.json" % stage)


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def _skeleton(stage: str, project: str, config_hash: str = "") -> dict:
    return {"stage": stage, "project": project, "schema": SCHEMA,
            "config_hash": config_hash, "updated_at": _now(), "units": {}}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load(path) -> dict:
    """读 manifest；不存在 → 顶层骨架（units 空）；损坏 → ManifestError（可读错误）。"""
    p = Path(path)
    if not p.is_file():
        return _skeleton(_guess_stage(p), _guess_project(p))
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ManifestError("manifest 损坏，无法解析: %s（%s）——请人工检查或删除重建" % (p, e)) from e
    if not isinstance(m, dict) or not isinstance(m.get("units"), dict):
        raise ManifestError("manifest 结构不合法: %s" % p)
    m.setdefault("schema", SCHEMA)
    m.setdefault("stage", _guess_stage(p))
    m.setdefault("project", _guess_project(p))
    m.setdefault("config_hash", "")
    m.setdefault("updated_at", "")
    return m


def _guess_stage(p: Path) -> str:
    name = p.name.replace("_manifest.json", "")
    return name if name in STAGE_DIRS else "unknown"


def _guess_project(p: Path) -> str:
    try:
        return p.parent.parent.name
    except Exception:
        return ""


def ensure(path, stage: str, project: str, config_hash: str = "") -> dict:
    """读（不存在则建骨架），补齐 config_hash，返回 dict（不落盘；由 save 落盘）。"""
    m = load(path)
    if m["stage"] != stage:
        raise ManifestError("manifest 段名不符: 期望 %s，文件 %s 声明 %s" % (stage, path, m["stage"]))
    if config_hash:
        m["config_hash"] = config_hash
    return m


def save(path, m: dict) -> None:
    """原子写：同目录 tmp + os.replace。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    m = dict(m)
    m["updated_at"] = _now()
    tmp = p.with_name("%s.tmp-%d" % (p.name, os.getpid()))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)


# --------------------------------------------------------------------------- #
# unit 操作
# --------------------------------------------------------------------------- #
def unit(m: dict, uid: str) -> dict:
    """取 unit；不存在则建 pending 骨架并写回 manifest（调用方负责 save）。"""
    u = m["units"].get(uid)
    if u is None:
        u = {"status": "pending", "files": {}, "prompt_id": None, "seed": None,
             "attempts": 0, "error": None, "updated_at": _now()}
        m["units"][uid] = u
    return u


def mark_unit(m: dict, uid: str, **fields) -> dict:
    u = unit(m, uid)
    u.update(fields)
    u["updated_at"] = _now()
    return u


# --------------------------------------------------------------------------- #
# 指纹与哈希
# --------------------------------------------------------------------------- #
def fingerprint_file(path) -> str:
    """上游产物指纹 = `路径|字节大小|mtime_ns`（大文件 O(1)，不哈希内容）。"""
    p = Path(path)
    if not p.is_file():
        return "missing"
    st = p.stat()
    return "%s|%d|%d" % (p.name, st.st_size, st.st_mtime_ns)


def hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_file_content(path) -> str:
    """小文本文件（yaml/词库/camdesc）用内容哈希。"""
    p = Path(path)
    if not p.is_file():
        return "missing"
    return hash_text(p.read_text(encoding="utf-8", errors="replace"))


def hash_fields(d) -> str:
    """unit 的 yaml 输入行指纹：键值规范化后整体哈希。"""
    blob = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 完成与失效判定
# --------------------------------------------------------------------------- #
def unit_complete(rec: dict, stage_dir, min_bytes) -> bool:
    """done 判定：status==done ∧ 每个 files 条目存在 ∧ 大小达标。

    min_bytes：int → 对所有文件统一；dict → 按 files 的逻辑键分别指定；0/None → 仅存在性。
    """
    if rec.get("status") != "done":
        return False
    files = rec.get("files") or {}
    if not files:
        return False
    base = Path(stage_dir)
    for logical, name in files.items():
        p = base / str(name)
        if not p.is_file():
            return False
        mb = min_bytes.get(logical, 0) if isinstance(min_bytes, dict) else min_bytes
        if p.stat().st_size < (mb or 0):
            return False
    return True


def stale_reason(rec: dict, *, inputs_hash: str | None = None,
                 upstream: dict | None = None, stage_cfg_hash: str | None = None) -> str | None:
    """失效判定：仅对 done 的 unit 有意义；返回原因或 None。

    upstream 形如 {"first_png": "S1_first.png|1048576|1698…"}（当前指纹）；
    与记录指纹逐键比对，键集合不同也算失效。
    """
    if rec.get("status") != "done":
        return None
    if inputs_hash is not None and rec.get("inputs_hash") != inputs_hash:
        return "inputs_hash 变化（yaml 输入行被编辑）"
    if stage_cfg_hash is not None and rec.get("stage_cfg_hash") != stage_cfg_hash:
        return "段配置哈希变化"
    if upstream is not None:
        prev = rec.get("upstream") or {}
        cur = upstream or {}
        if set(prev) != set(cur):
            missing = sorted(set(prev) ^ set(cur))
            return "上游依赖集合变化（%s）" % ",".join(missing)
        for k, fp in cur.items():
            if prev.get(k) != fp:
                return "上游 %s 指纹变化（%s → %s）" % (k, prev.get(k), fp)
    return None


def summary(m: dict) -> dict:
    """status 计数（status 命令用）。"""
    counts: dict = {}
    for u in (m.get("units") or {}).values():
        s = str(u.get("status", "pending"))
        counts[s] = counts.get(s, 0) + 1
    return counts
