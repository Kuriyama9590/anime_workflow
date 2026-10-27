# -*- coding: utf-8 -*-
"""项目数据装载与 schema 校验（project.yaml / characters.yaml / shots.yaml / subtitles.yaml）。

- 解析一律走 pipeline.miniyaml（规范与实现不允许漂移，docs §2.3）；
- 校验一次性收集全部问题（不是遇到第一个才炸），SchemaError.messages 带完整清单；
- 引用完整性：shots.characters ⊆ characters.id；characters.assets.props ⊆ props_catalog；
  subtitles.cues.shot ⊆ shots.id；
- shots 取值优先级（docs §4.0）：单镜字段 > shots.defaults: > config 段默认值；
  defaults 是纯缺省值表，任何单镜字段的出现都整体覆盖对应键，不合并（camera 节同理）。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import miniyaml

__all__ = ["SchemaError", "load_project_data", "validate_all",
           "shot_style", "eff_duration", "eff_camera", "shot_last_mode"]

_ASCII_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_SHOT_ID_RE = re.compile(r"^S\d+$")
_STYLES = ("photo", "anime")
_LAST_MODES = ("edit", "fresh")
_CAM_MODES = ("A", "B")
_CAM_OUTPUTS = ("endpoints", "full_video")


class SchemaError(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("schema 校验失败（%d 项）：\n  - %s"
                         % (len(self.problems), "\n  - ".join(self.problems)))


# --------------------------------------------------------------------------- #
# 装载
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path, problems, required=True):
    if not path.is_file():
        if required:
            problems.append("缺文件: %s" % path.name)
        return None
    try:
        return miniyaml.load(path)
    except miniyaml.MiniYAMLError as e:
        problems.append("%s 解析失败: %s" % (path.name, e))
        return None


def load_project_data(project_dir, cfg: dict | None = None) -> dict:
    """读并校验项目四件 yaml。任何结构性问题 → SchemaError（全量列出）。"""
    d = Path(project_dir)
    problems: list = []
    project = _read_yaml(d / "project.yaml", problems) or {}
    characters = _read_yaml(d / "characters.yaml", problems) or {}
    shots = _read_yaml(d / "shots.yaml", problems) or {}
    subtitles = _read_yaml(d / "subtitles.yaml", problems, required=False)
    data = {"dir": d, "project": project, "characters": characters,
            "shots": shots, "subtitles": subtitles}
    problems += _check_project(project, cfg)
    problems += _check_characters(characters)
    problems += _check_shots(shots, characters, cfg)
    if subtitles is not None:
        problems += _check_subtitles(subtitles, shots)
    if problems:
        raise SchemaError(problems)
    return data


def validate_all(data: dict, cfg: dict | None = None) -> list:
    """已装载数据的再校验（scaffold 生成后立即跑，结果直接打印）。"""
    problems: list = []
    problems += _check_project(data.get("project") or {}, cfg)
    problems += _check_characters(data.get("characters") or {})
    problems += _check_shots(data.get("shots") or {}, data.get("characters") or {}, cfg)
    if data.get("subtitles") is not None:
        problems += _check_subtitles(data["subtitles"], data.get("shots") or {})
    return problems


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _check_project(p: dict, cfg) -> list:
    errs = []
    if not isinstance(p, dict) or not p:
        return ["project.yaml 顶层须为非空映射"]
    if not isinstance(p.get("name"), str) or not p["name"].strip():
        errs.append("project.name 缺失或为空")
    if p.get("style") not in _STYLES:
        errs.append("project.style=%r 须为 photo|anime" % (p.get("style"),))
    sb = p.get("seed_base")
    if not isinstance(sb, int) or isinstance(sb, bool) or sb < 0:
        errs.append("project.seed_base=%r 须为非负整数（§3 规则 4 派生基准）" % (sb,))
    else:
        # seed 域防溢出：video 段 +2_000_000+镜序，上限保护
        if sb + 3_000_000 > 2**31 - 1:
            errs.append("project.seed_base=%d 过大（派生域溢出）" % sb)
    anchors = p.get("style_anchors")
    if not isinstance(anchors, dict) or not isinstance(anchors.get(p.get("style")), str) \
            or not anchors.get(p.get("style"), "").strip():
        errs.append("project.style_anchors.%s 缺失或为空（所选通道必须有画风锚点）" % p.get("style"))
    ov = p.get("overrides")
    if ov is not None and not isinstance(ov, dict):
        errs.append("project.overrides 须为映射")
    return errs


def _check_characters(c: dict) -> list:
    errs = []
    if not isinstance(c, dict):
        return ["characters.yaml 顶层须为映射"]
    chars = c.get("characters")
    if not isinstance(chars, list) or not chars:
        return ["characters.characters 须为非空列表"]
    seen = set()
    for i, ch in enumerate(chars, 1):
        if not isinstance(ch, dict):
            errs.append("characters[%d] 须为映射" % i)
            continue
        cid = ch.get("id")
        if not isinstance(cid, str) or not _ASCII_ID_RE.match(cid or ""):
            errs.append("characters[%d].id=%r 须为 ASCII（文件名安全：[A-Za-z0-9_]）" % (i, cid))
        elif cid in seen:
            errs.append("characters.id 重复: %s" % cid)
        else:
            seen.add(cid)
        if not isinstance(ch.get("name"), str) or not ch["name"].strip():
            errs.append("characters[%s].name 缺失（中文名，仅展示）" % cid)
        if not isinstance(ch.get("anchor_en"), str) or not ch["anchor_en"].strip():
            errs.append("characters[%s].anchor_en 缺失（英文一致性锚点，全链拼接用）" % cid)
        for k in ("outfit_en", "negative_extra"):
            v = ch.get(k)
            if v is not None and not isinstance(v, str):
                errs.append("characters[%s].%s 须为字符串" % (cid, k))
        a = ch.get("assets")
        if not isinstance(a, dict):
            errs.append("characters[%s].assets 须为映射 {three_view, expressions, props}" % cid)
            continue
        if not isinstance(a.get("three_view", True), bool):
            errs.append("characters[%s].assets.three_view 须为布尔" % cid)
        for k in ("expressions", "props"):
            v = a.get(k)
            if v is None:
                continue
            if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
                errs.append("characters[%s].assets.%s 须为字符串列表" % (cid, k))
    pc = c.get("props_catalog")
    if pc is not None:
        if not isinstance(pc, dict) or not all(isinstance(v, str) for v in pc.values()):
            errs.append("props_catalog 须为 道具名→英文锚点 的映射")
        else:
            for ch in chars:
                if not isinstance(ch, dict):
                    continue
                a = ch.get("assets") or {}
                for prop in (a.get("props") or []):
                    if prop not in pc:
                        errs.append("characters[%s].assets.props 引用的道具 %r 不在 props_catalog"
                                    % (ch.get("id"), prop))
    return errs


def _check_shots(s: dict, c: dict, cfg) -> list:
    errs = []
    if not isinstance(s, dict):
        return ["shots.yaml 顶层须为映射"]
    shots = s.get("shots")
    if not isinstance(shots, list) or not shots:
        return ["shots.shots 须为非空列表（至少一镜）"]
    defaults = s.get("defaults")
    if defaults is not None and not isinstance(defaults, dict):
        errs.append("shots.defaults 须为映射（纯缺省值表）")
        defaults = {}
    defaults = defaults or {}
    char_ids = {ch.get("id") for ch in (c.get("characters") or [])
                if isinstance(ch, dict) and isinstance(ch.get("id"), str)}
    vcfg = (cfg or {}).get("video") or {}
    lo = float(vcfg.get("min_request_s", 5.0))
    hi = float(vcfg.get("max_request_s", 15.0))
    seen = set()
    for i, sh in enumerate(shots, 1):
        if not isinstance(sh, dict):
            errs.append("shots[%d] 须为映射" % i)
            continue
        sid = sh.get("id")
        if not isinstance(sid, str) or not _SHOT_ID_RE.match(sid or ""):
            errs.append("shots[%d].id=%r 须形如 S1/S2（纯镜号，排序即成片顺序）" % (i, sid))
            sid = None
        elif sid in seen:
            errs.append("shots.id 重复: %s" % sid)
        else:
            seen.add(sid)
        where = sid or ("shots[%d]" % i)
        chars = sh.get("characters")
        if chars is not None:
            if not isinstance(chars, list) or not all(isinstance(x, str) for x in chars):
                errs.append("%s.characters 须为字符串列表（flow: [id, …]）" % where)
            else:
                for cid in chars:
                    if cid not in char_ids:
                        errs.append("%s 引用 characters.yaml 不存在的角色 %r" % (where, cid))
        style = sh.get("style")
        if style is not None and style not in _STYLES:
            errs.append("%s.style=%r 须为 photo|anime" % (where, style))
        d = sh.get("duration_s", defaults.get("duration_s"))
        if d is None and cfg:
            d = vcfg.get("default_duration_s")
        if not isinstance(d, (int, float)) or isinstance(d, bool):
            errs.append("%s.duration_s=%r 须为数值" % (where, d))
        elif not (lo <= float(d) <= hi):
            errs.append("%s.duration_s=%s 超出请求域 [%s, %s]（H3 时长网格，§4.4）" % (where, d, lo, hi))
        if not isinstance(sh.get("first"), str) or not (sh.get("first") or "").strip():
            errs.append("%s.first 缺失（首帧镜头描述，英文）" % where)
        last = sh.get("last")
        if not isinstance(last, str) or not (last or "").strip():
            errs.append("%s.last 缺失（末帧相对变化描述；fresh 模式亦用此字段）" % where)
        lm = sh.get("last_mode", defaults.get("last_mode", "edit"))
        if lm not in _LAST_MODES:
            errs.append("%s.last_mode=%r 须为 edit|fresh" % (where, lm))
        ucr = sh.get("use_char_ref", defaults.get("use_char_ref", False))
        if not isinstance(ucr, bool):
            errs.append("%s.use_char_ref 须为布尔" % where)
        elif ucr and style is not None and style != "photo":
            errs.append("%s.use_char_ref=true 仅支持 photo 通道（§4.2）" % where)
        cam = sh.get("camera", defaults.get("camera"))
        if cam is not None:
            errs += _check_camera(where, cam)
        motion = sh.get("motion", defaults.get("motion"))
        if not isinstance(motion, str) or not motion.strip():
            errs.append("%s.motion 缺失（H3 Timeline 段，| 多行块）" % where)
        for k in ("scene", "audio_hint", "negative_extra", "title"):
            v = sh.get(k)
            if v is not None and not isinstance(v, str):
                errs.append("%s.%s 须为字符串" % (where, k))
        seed = sh.get("seed")
        if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or seed < 0):
            errs.append("%s.seed=%r 须为非负整数（0=按 §3 规则 4 派生）" % (where, seed))
    return errs


def _check_camera(where: str, cam) -> list:
    errs = []
    if not isinstance(cam, dict):
        return ["%s.camera 须为映射 {mode, move, intensity, extra}" % where]
    mode = cam.get("mode")
    if mode is not None and mode not in _CAM_MODES:
        errs.append("%s.camera.mode=%r 须为 A|B" % (where, mode))
    for k in ("move", "intensity", "extra"):
        v = cam.get(k)
        if v is not None and not isinstance(v, str):
            errs.append("%s.camera.%s 须为字符串" % (where, k))
    # 词库白名单（人工编辑分镜表的第一道防线：非法 move/intensity 会让 run_camera
    # 的 argparse 抛 SystemExit，必须在校验层拦下而非运行期崩溃）
    wl = _vocab_whitelist()
    if wl:
        moves, intensities = wl
        mv = cam.get("move")
        if isinstance(mv, str) and mv and mv not in moves:
            errs.append("%s.camera.move=%r 不在词库（可用：%s；`python -m pipeline.camera.run_camera vocab` 查看）"
                        % (where, mv, "/".join(sorted(moves))))
        it = cam.get("intensity")
        if isinstance(it, str) and it and it not in intensities:
            errs.append("%s.camera.intensity=%r 须为 %s" % (where, it, "/".join(sorted(intensities))))
    out = cam.get("output")
    if out is not None and out not in _CAM_OUTPUTS:
        errs.append("%s.camera.output=%r 须为 endpoints|full_video" % (where, out))
    return errs


_VOCAB_CACHE: list = []


def _vocab_whitelist():
    """camera 词库白名单 (moves, intensities)；词库不可用时返回 None（降级为仅类型检查）。"""
    if _VOCAB_CACHE:
        return _VOCAB_CACHE[0]
    try:
        from pipeline.camera import vocab as _vocab
        v = _vocab.load_vocab()
        moves = set((v.get("moves") or {}).keys())
        # 词库是唯一事实源：vocab.yaml 顶层 intensities 列表 > moves 并集兜底
        intensities = set(v.get("intensities") or []) or {
            i for entries in (v.get("moves") or {}).values() for i in entries}
        wl = (moves, intensities) if moves else None
    except Exception:  # noqa: BLE001 —— camera 模块/词库缺失时不阻塞 schema 校验
        wl = None
    _VOCAB_CACHE.append(wl)
    return wl


def _check_subtitles(sub: dict, shots: dict) -> list:
    errs = []
    if not isinstance(sub, dict):
        return ["subtitles.yaml 顶层须为映射"]
    shot_ids = {sh.get("id") for sh in (shots.get("shots") or []) if isinstance(sh, dict)}
    cues = sub.get("cues")
    if not isinstance(cues, list) or not cues:
        errs.append("subtitles.cues 须为非空列表（{shot, t_in, t_out, text}）")
    else:
        for i, cue in enumerate(cues, 1):
            if not isinstance(cue, dict):
                errs.append("cues[%d] 须为映射" % i)
                continue
            if cue.get("shot") not in shot_ids:
                errs.append("cues[%d].shot=%r 不在 shots.yaml" % (i, cue.get("shot")))
            for k in ("t_in", "t_out"):
                v = cue.get(k)
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                    errs.append("cues[%d].%s=%r 须为非负数值（镜内秒）" % (i, k, v))
            if isinstance(cue.get("t_in"), (int, float)) and isinstance(cue.get("t_out"), (int, float)) \
                    and cue["t_out"] <= cue["t_in"]:
                errs.append("cues[%d].t_out 须大于 t_in" % i)
            if not isinstance(cue.get("text"), str) or not cue["text"].strip():
                errs.append("cues[%d].text 缺失" % i)
    for k in ("title_card", "end_card"):
        v = sub.get(k)
        if v is not None and not isinstance(v, dict):
            errs.append("subtitles.%s 须为映射 {text, …}" % k)
    return errs


# --------------------------------------------------------------------------- #
# 取值优先级辅助（单镜 > defaults > config 默认）
# --------------------------------------------------------------------------- #
def shot_style(shot: dict, shots_doc: dict, project: dict) -> str:
    v = shot.get("style")
    if v is not None:
        return v
    v = shots_doc.get("style")
    if v is not None:
        return v
    v = (project or {}).get("style")
    return v if v in _STYLES else "photo"


def eff_duration(shot: dict, shots_doc: dict, cfg: dict | None) -> float:
    v = shot.get("duration_s")
    if v is None:
        v = (shots_doc.get("defaults") or {}).get("duration_s")
    if v is None:
        v = ((cfg or {}).get("video") or {}).get("default_duration_s", 5.17)
    return float(v)


def eff_camera(shot: dict, shots_doc: dict) -> dict:
    """camera 节整体覆盖（单镜 camera 出现即整体替换 defaults.camera，不合并）。"""
    v = shot.get("camera")
    if isinstance(v, dict):
        return v
    d = (shots_doc.get("defaults") or {}).get("camera")
    return d if isinstance(d, dict) else {}


def shot_last_mode(shot: dict, shots_doc: dict) -> str:
    v = shot.get("last_mode")
    if v is None:
        v = (shots_doc.get("defaults") or {}).get("last_mode", "edit")
    return v if v in _LAST_MODES else "edit"
