# -*- coding: utf-8 -*-
"""配置：全局 pipeline/config.yaml + 项目 project.yaml overrides + CLI --set。

优先级：全局 < project.yaml 的 overrides: 节 < CLI --set a.b.c=v（docs §2.2）。
合并规则：映射递归、标量覆盖、列表整体替换。
路径锚定：相对路径一律以仓库根（= pipeline 包父目录，由 __file__ 定位）解析，
与命令行 cwd 无关（边界：`python -m pipeline` 的包发现仍需 cwd=仓库根或 PYTHONPATH）。
校验分两级：
  * validate()      —— 结构性校验（必填/类型/数值域），一次性列出全部错误（fatal）
  * path_hints()    —— 路径/权重存在性提示（WARN 级；--strict 下才升级为 fatal，
                        依据本次模块任务书"权重/目录存在性提示"的措辞，见 caveats）
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import miniyaml

__all__ = [
    "ConfigError", "repo_root", "resolve_path", "deep_merge", "parse_set",
    "load_global", "load_project_file", "MergedConfig", "load_merged", "load",
    "config_hash", "stage_sections_hash", "STAGE_SECTIONS", "validate",
    "path_hints", "weight_checks", "Resolved", "resolved", "ensure_runtime_dirs",
]

_REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    pass


def repo_root() -> Path:
    """仓库根 = pipeline 包父目录（唯一事实源，勿依赖 cwd）。"""
    return _REPO_ROOT


def resolve_path(p) -> Path:
    """相对路径 → 仓库根锚定绝对路径；绝对路径原样。统一正斜杠显示。"""
    q = Path(str(p))
    if not q.is_absolute():
        q = _REPO_ROOT / q
    return Path(os.path.normpath(q))


# --------------------------------------------------------------------------- #
# 合并与 --set
# --------------------------------------------------------------------------- #
def deep_merge(base, over):
    """映射递归合并；标量覆盖；列表整体替换（不做列表合并）。不修改入参。"""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = deep_merge(out[k], v) if k in out else v
        return out
    return over


def _parse_set_value(raw: str):
    s = raw.strip()
    if s.lower() in ("null", "none", "~"):
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def parse_set(item: str) -> dict:
    """`a.b.c=v` → 嵌套 dict。值做类型推断（int/float/bool/null/str）。"""
    if "=" not in item:
        raise ConfigError("--set 需要 key=value 形式，得到: %r" % item)
    key, _, raw = item.partition("=")
    parts = [p.strip() for p in key.split(".") if p.strip()]
    if not parts:
        raise ConfigError("--set 键为空: %r" % item)
    node: dict = {}
    cur = node
    for p in parts[:-1]:
        cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = _parse_set_value(raw)
    return node


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def load_global(config_path=None) -> dict:
    p = Path(config_path) if config_path else repo_root() / "pipeline" / "config.yaml"
    if not p.is_file():
        raise ConfigError("全局配置缺失: %s" % p)
    try:
        cfg = miniyaml.load(p)
    except miniyaml.MiniYAMLError as e:
        raise ConfigError("全局配置解析失败: %s（%s）" % (p, e)) from e
    if not isinstance(cfg, dict):
        raise ConfigError("全局配置顶层必须是映射: %s" % p)
    return cfg


def load_project_file(project_dir) -> dict:
    """读 project.yaml；不存在时给可读错误。"""
    p = Path(project_dir) / "project.yaml"
    if not p.is_file():
        raise ConfigError("项目配置缺失: %s（先 `python -m pipeline new <名>`）" % p)
    try:
        data = miniyaml.load(p)
    except miniyaml.MiniYAMLError as e:
        raise ConfigError("project.yaml 解析失败: %s" % e) from e
    if not isinstance(data, dict):
        raise ConfigError("project.yaml 顶层必须是映射: %s" % p)
    return data


@dataclass
class MergedConfig:
    cfg: dict                                    # 合并后配置（含 overrides/--set）
    project_meta: dict | None                    # project.yaml 除 overrides 外的项目元数据
    project_name: str | None
    project_dir: Path | None
    config_hash: str = ""
    source: str = ""                             # 审计：加载链描述

    def stage_hash(self, stage: str) -> str:
        return stage_sections_hash(self.cfg, stage)


def load_merged(project_name: str | None = None, sets=None, config_path=None) -> MergedConfig:
    """全局 + 项目 overrides + CLI --set，按优先级深合并。

    CLI --set 在解析项目目录**之前**合并（paths.projects_root 可被 --set 覆盖，
    selftest 的 CLI 冒烟即依赖此行为定位临时 projects 根）。
    """
    cfg = load_global(config_path)
    chain = ["global: pipeline/config.yaml"]
    project_meta = None
    project_dir = None
    for item in sets or []:
        cfg = deep_merge(cfg, parse_set(item))
        chain.append("cli: --set %s" % item.split("=", 1)[0])
    if project_name:
        root = resolve_path(cfg.get("paths", {}).get("projects_root", "projects"))
        project_dir = root / str(project_name)
        if not project_dir.is_dir():
            raise ConfigError("项目目录不存在: %s（先 `python -m pipeline new %s`）"
                              % (project_dir, project_name))
        meta = load_project_file(project_dir)
        project_meta = {k: v for k, v in meta.items() if k != "overrides"}
        ov = meta.get("overrides")
        if ov is not None:
            if not isinstance(ov, dict):
                raise ConfigError("project.yaml 的 overrides 必须是映射")
            cfg = deep_merge(cfg, ov)
            chain.append("project: project.yaml overrides")
    return MergedConfig(
        cfg=cfg,
        project_meta=project_meta,
        project_name=project_name,
        project_dir=project_dir,
        config_hash=config_hash(cfg),
        source=" < ".join(chain),
    )


def load(project_dir=None, sets=None) -> dict:
    """探测友好入口：返回合并后配置 **dict**（不是 MergedConfig）。

    供段模块自举使用（如 stages/keyframes.py 的 load_merged_config 依次探测
    load/load_config/get_config/load_merged，取第一个返回 dict 的）。
    project_dir 可为项目名或项目目录绝对路径。
    """
    name = None
    if project_dir:
        name = str(project_dir)
    return load_merged(name, sets=sets).cfg


def config_hash(cfg: dict) -> str:
    """合并后配置的稳定哈希（写进各段 manifest，用于失效判定）。"""
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# 每段失效判定只看该段相关配置节（docs §5：config_hash（该段相关配置节））
STAGE_SECTIONS: dict[str, list[str]] = {
    "assets": ["channels", "assets"],
    "keyframes": ["channels"],
    "camera": ["camera"],
    "video": ["video"],
    "final": ["final"],
}


def stage_sections_hash(cfg: dict, stage: str) -> str:
    keys = STAGE_SECTIONS.get(stage)
    if keys is None:
        raise ConfigError("未知段: %r（合法：%s）" % (stage, ", ".join(STAGE_SECTIONS)))
    sub = {k: cfg.get(k) for k in keys}
    return config_hash(sub)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def validate(cfg: dict) -> list[str]:
    """结构性全量校验：一次性收集全部错误（不遇错即停）。"""
    errs: list[str] = []

    def need(d, path, checker, desc):
        cur = cfg
        for k in path.split("."):
            if not isinstance(cur, dict) or k not in cur:
                errs.append("缺少必填项 %s" % path)
                return
            cur = cur[k]
        if not checker(cur):
            errs.append("%s 应为 %s，得到 %r" % (path, desc, cur))

    comfy = cfg.get("comfy") or {}
    need(cfg, "comfy.server", _is_str, "URL 字符串")
    need(cfg, "comfy.poll_interval_s", lambda v: _is_num(v) and v > 0, "正数")
    need(cfg, "comfy.timeout_img_s", lambda v: _is_num(v) and v > 0, "正数")
    need(cfg, "comfy.timeout_video_s", lambda v: _is_num(v) and v > 0, "正数")
    if not (isinstance(comfy.get("input_dirs"), list) and comfy.get("input_dirs")):
        errs.append("comfy.input_dirs 应为非空列表（LoadImage 双目录为实测要求）")
    for k in ("portable_root", "models_dir"):
        need(cfg, "comfy.%s" % k, _is_str, "路径字符串")

    for ch in ("photo", "anime"):
        c = (cfg.get("channels") or {}).get(ch) or {}
        if not isinstance(c, dict) or not c:
            errs.append("缺少 channels.%s 通道配置" % ch)
            continue
        for k in ("width", "height"):
            need(cfg, "channels.%s.%s" % (ch, k), lambda v: isinstance(v, int) and v > 0, "正整数")
    for k in ("unet_gguf", "clip", "vae", "clip_type", "sampler", "sigmas"):
        need(cfg, "channels.photo.%s" % k, _is_str, "字符串")
    for k in ("checkpoint", "sampler", "negative"):
        need(cfg, "channels.anime.%s" % k, _is_str, "字符串")
    need(cfg, "channels.anime.steps", lambda v: isinstance(v, int) and v > 0, "正整数")
    need(cfg, "channels.anime.img2img_denoise", lambda v: _is_num(v) and 0 < v <= 1, "0-1 之间的小数")

    a = cfg.get("assets") or {}
    if not isinstance(a, dict):
        errs.append("assets 节缺失")
    else:
        for k in ("width", "height"):
            if not isinstance(a.get(k), int) or a.get(k, 0) <= 0:
                errs.append("assets.%s 应为正整数" % k)
        if not isinstance(a.get("candidates"), int) or a.get("candidates", 0) < 1:
            errs.append("assets.candidates 应为 ≥1 的整数")

    v = cfg.get("video") or {}
    if not isinstance(v, dict):
        errs.append("video 节缺失")
    else:
        for k in ("unet", "text_encoder", "clip_type", "lora_4step", "lora_8step",
                  "vae_video", "vae_audio", "sampler", "scheduler"):
            if not _is_str(v.get(k)):
                errs.append("video.%s 应为非空字符串（models_dir 相对路径或名称）" % k)
        for k in ("lora_min_bytes", "weight_min_bytes", "min_bytes", "fps", "k_min", "k_max"):
            if not isinstance(v.get(k), int) or v.get(k, 0) < 0:
                errs.append("video.%s 应为非负整数" % k)
        for k in ("width", "height"):
            if not isinstance(v.get(k), int) or v.get(k, 0) <= 0:
                errs.append("video.%s 应为正整数" % k)
        if not isinstance(v.get("steps_follow_lora"), bool):
            errs.append("video.steps_follow_lora 应为布尔")
        lg = v.get("length_grid")
        if not (isinstance(lg, dict) and isinstance(lg.get("a"), int) and lg.get("a", 0) >= 1
                and isinstance(lg.get("b"), int) and lg.get("b", 0) >= 0):
            errs.append("video.length_grid 应为 {a: 正整数, b: 非负整数}")
        if not (isinstance(v.get("k_min"), int) and isinstance(v.get("k_max"), int)
                and 0 < v["k_min"] < v["k_max"]):
            errs.append("video.k_min/k_max 应满足 0 < k_min < k_max")
        if v.get("duration_tolerance") != "half_step":
            errs.append("video.duration_tolerance 目前仅支持 half_step")
        for k in ("default_duration_s", "min_request_s", "max_request_s"):
            if not _is_num(v.get(k)):
                errs.append("video.%s 应为数值" % k)
        if _is_num(v.get("min_request_s")) and _is_num(v.get("max_request_s")) \
                and v["min_request_s"] >= v["max_request_s"]:
            errs.append("video.min_request_s 应小于 max_request_s")
        if _is_num(v.get("default_duration_s")) and _is_num(v.get("min_request_s")) \
                and _is_num(v.get("max_request_s")):
            d = v["default_duration_s"]
            if not (v["min_request_s"] <= d <= v["max_request_s"]):
                errs.append("video.default_duration_s=%s 超出请求域 [%s, %s]"
                            % (d, v["min_request_s"], v["max_request_s"]))

    f = cfg.get("final") or {}
    if not isinstance(f, dict):
        errs.append("final 节缺失")
    else:
        for k in ("ffmpeg", "ffprobe", "preset", "audio_codec", "audio_bitrate", "font", "sub_style"):
            if not _is_str(f.get(k)):
                errs.append("final.%s 应为非空字符串" % k)
        if not isinstance(f.get("crf"), int) or not (0 <= f.get("crf", -1) <= 51):
            errs.append("final.crf 应为 0-51 的整数")

    cam = cfg.get("camera") or {}
    if not isinstance(cam, dict):
        errs.append("camera 节缺失")
    else:
        if cam.get("mode_default") not in ("A", "B"):
            errs.append("camera.mode_default 应为 A 或 B")
        b = cam.get("blender") or {}
        if not isinstance(b, dict):
            errs.append("camera.blender 节缺失")
        else:
            for k in ("version", "install_dir", "download_base"):
                if not _is_str(b.get(k)):
                    errs.append("camera.blender.%s 应为非空字符串" % k)
            if not (isinstance(b.get("render_engine_enum"), list) and b.get("render_engine_enum")):
                errs.append("camera.blender.render_engine_enum 应为非空列表")
            for k in ("samples", "overscan", "timeout_s"):
                if not _is_num(b.get(k)) or b.get(k, 0) <= 0:
                    errs.append("camera.blender.%s 应为正数" % k)
        llm = cam.get("llm") or {}
        if not isinstance(llm, dict):
            errs.append("camera.llm 节缺失")
        else:
            for k in ("enabled", "require_review"):
                if not isinstance(llm.get(k), bool):
                    errs.append("camera.llm.%s 应为布尔" % k)
            if not isinstance(llm.get("retries"), int) or llm.get("retries", -1) < 0:
                errs.append("camera.llm.retries 应为非负整数")

    r = cfg.get("run") or {}
    if not isinstance(r, dict):
        errs.append("run 节缺失")
    else:
        if not isinstance(r.get("attempts"), int) or r.get("attempts", 0) < 1:
            errs.append("run.attempts 应为 ≥1 的整数（总尝试次数，含首次）")
        for k in ("retry_wait_s", "quiet_first_s"):
            if not _is_num(r.get(k)) or r.get(k, -1) < 0:
                errs.append("run.%s 应为非负数" % k)
        if not isinstance(r.get("server_precheck"), bool):
            errs.append("run.server_precheck 应为布尔")

    p = cfg.get("paths") or {}
    if not isinstance(p, dict) or not _is_str(p.get("projects_root")) or not _is_str(p.get("logs_dir")):
        errs.append("paths.projects_root / paths.logs_dir 应为非空字符串（仓库根相对路径）")
    return errs


# --------------------------------------------------------------------------- #
# 解析后的路径与存在性提示
# --------------------------------------------------------------------------- #
@dataclass
class Resolved:
    models_dir: Path
    portable_root: Path
    input_dirs: list
    projects_root: Path
    logs_dir: Path
    font: Path
    ffmpeg: str
    ffprobe: str
    server: str
    extra: dict = field(default_factory=dict)


def resolved(cfg: dict) -> Resolved:
    comfy = cfg.get("comfy") or {}
    final = cfg.get("final") or {}
    paths = cfg.get("paths") or {}
    return Resolved(
        models_dir=resolve_path(comfy.get("models_dir", "")),
        portable_root=resolve_path(comfy.get("portable_root", "")),
        input_dirs=[resolve_path(d) for d in comfy.get("input_dirs", [])],
        projects_root=resolve_path(paths.get("projects_root", "projects")),
        logs_dir=resolve_path(paths.get("logs_dir", "logs")),
        font=resolve_path(final.get("font", "")),
        ffmpeg=str(final.get("ffmpeg", "ffmpeg")),
        ffprobe=str(final.get("ffprobe", "ffprobe")),
        server=str(comfy.get("server", "http://127.0.0.1:8188")),
        extra={"server_precheck": bool((cfg.get("run") or {}).get("server_precheck", True))},
    )


def path_hints(cfg: dict) -> list[str]:
    """目录/字体存在性提示（WARN 级）。"""
    r = resolved(cfg)
    hints = []
    for label, p in (("comfy.portable_root", r.portable_root), ("comfy.models_dir", r.models_dir)):
        if str(p) and not p.is_dir():
            hints.append("%s 不存在: %s" % (label, p))
    if str(r.font) and not r.font.is_file():
        hints.append("%s 不存在: %s" % ("final.font", r.font))
    for d in r.input_dirs:
        if not d.is_dir():
            hints.append("comfy.input_dirs 条目不存在: %s" % d)
    return hints


def weight_checks(cfg: dict) -> list[dict]:
    """权重在盘检查（含大小门）。相对路径按 models_dir 拼全路径（docs §2.1 注）。"""
    r = resolved(cfg)
    ch = (cfg.get("channels") or {})
    v = cfg.get("video") or {}
    items = [
        ("channels.photo.unet_gguf", ch.get("photo", {}).get("unet_gguf"), 0),
        ("channels.photo.clip", ch.get("photo", {}).get("clip"), 0),
        ("channels.photo.vae", ch.get("photo", {}).get("vae"), 0),
        ("channels.anime.checkpoint", ch.get("anime", {}).get("checkpoint"), 0),
        ("video.unet", v.get("unet"), v.get("weight_min_bytes", 0)),
        ("video.text_encoder", v.get("text_encoder"), v.get("weight_min_bytes", 0)),
        ("video.lora_4step", v.get("lora_4step"), v.get("lora_min_bytes", 0)),
        ("video.lora_8step", v.get("lora_8step"), v.get("lora_min_bytes", 0)),
        ("video.vae_video", v.get("vae_video"), 0),
        ("video.vae_audio", v.get("vae_audio"), 0),
    ]
    out = []
    for label, rel, min_bytes in items:
        if not rel:
            out.append({"label": label, "path": None, "min_bytes": min_bytes or 0,
                        "exists": False, "size": 0, "ok": False, "problem": "配置缺失"})
            continue
        p = Path(str(rel))
        if not p.is_absolute():
            p = r.models_dir / p
        exists = p.is_file()
        size = p.stat().st_size if exists else 0
        ok = exists and size >= (min_bytes or 0)
        problem = ""
        if not exists:
            problem = "不在盘"
        elif not ok:
            problem = "大小 %d 低于下限 %d（残件？）" % (size, min_bytes)
        out.append({"label": label, "path": p, "min_bytes": min_bytes or 0,
                    "exists": exists, "size": size, "ok": ok, "problem": problem})
    return out


def ensure_runtime_dirs(cfg: dict) -> None:
    r = resolved(cfg)
    r.projects_root.mkdir(parents=True, exist_ok=True)
    r.logs_dir.mkdir(parents=True, exist_ok=True)
