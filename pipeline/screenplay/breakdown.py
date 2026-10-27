# -*- coding: utf-8 -*-
"""breakdown（段0·拆解）：剧本.md → shots.yaml + characters.yaml + subtitles.yaml。

LLM 模式（config ``llm:`` 段已配置）：
  - 台本全文交给 LLM，要求输出结构化 JSON（字段契约见 prompts.JSON_SPEC）；
  - 本地确定性后处理（不依赖 LLM 自觉）：时长逐镜夹取进 H3 网格请求域并按
    17k+5 网格落点（docs §4.4），总预算超限时逐镜降档直至命中（±1 步容差）；
    运镜词库标注（模式B move × intensity）；每镜一条字幕 cue（t_out 按镜长夹取）；
    seed 一律写 0（= 按 §3 规则 4 派生，迁移老项目才写显式值）；
  - **schema 强校验 + 有界退回**：用 pipeline.schema（与人工填写同一把尺）校验
    characters/shots/subtitles 与引用完整性；不过 → 错误清单带回 LLM 重新生成，
    最多 3 次；仍失败 → 非零退出，最后一次产物保留为 *.draft.yaml +
    breakdown_last_output.txt + screenplay_review.txt 供人工修补；
  - 落盘走「内存校验 → 自写 YAML 往返校验 → 备份旧版 → 写入 → 磁盘终检」，
    终检不过自动回滚旧版。

校验器模式（未配置 LLM 的降级路径）：不生成，只对人工填写的三份 yaml 跑同一套
schema 校验并给友好报告（角色/镜数/cues/总时长 vs 剧本预算行）。

产物：projects/<名>/{shots,characters,subtitles}.yaml（覆盖前旧版备份 *.bak）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import config as config_mod
from .. import miniyaml, schema
from . import common, llm_client, prompts

__all__ = ["run", "MAX_ATTEMPTS", "BreakdownError", "extract_json",
           "build_docs", "fit_durations", "emit_yaml"]

MAX_ATTEMPTS = 3
_REVIEW_FNAME = "screenplay_review.txt"
_LAST_RAW_FNAME = "breakdown_last_output.txt"
_TARGETS = ("shots.yaml", "characters.yaml", "subtitles.yaml")
_MULTILINE_OK = True  # 多行字段用 |- 块标量（miniyaml 支持集内）


class BreakdownError(ValueError):
    """拆解/校验失败；.problems 为面向 LLM 反馈的问题清单。"""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("breakdown 问题（%d 项）：%s" % (len(self.problems), "; ".join(self.problems)))


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run(project_dir, cfg: dict, *, args=None, opts=None, chat_fn=None, log=print) -> int:
    pdir = Path(project_dir)
    cfg = cfg or {}
    dry = bool(getattr(opts, "dry_run", False))
    n_cli = int(getattr(args, "shots", 0) or 0)
    t_cli = float(getattr(args, "total_s", 0.0) or 0.0)

    if chat_fn is None:
        chat_fn = llm_client.make_chat_fn(cfg)

    script = pdir / "剧本.md"
    if chat_fn is None:
        return _validator_mode(pdir, cfg, log)

    if not script.is_file():
        log("[breakdown] 缺 %s：先 run --stage screenplay-write 生成，"
            "或人工创作后保存为 剧本.md（校验器模式则无需它）。" % script)
        return 1
    try:
        script_text = script.read_text(encoding="utf-8-sig")
    except OSError as e:
        log("[breakdown] 读取剧本失败：%s" % e)
        return 1
    n_script, t_script = common.parse_budget_line(script_text)
    n_req = n_cli or n_script or 0
    budget = t_cli or t_script or 0.0
    if n_cli and n_script and n_cli != n_script:
        log("  [提示] --shots=%d 覆盖剧本预算行的 %d 镜。" % (n_cli, n_script))
    if t_cli and t_script and abs(t_cli - t_script) > 0.5:
        log("  [提示] --total-s=%s 覆盖剧本预算行的 %ss。"
            % (common.fmt_secs(t_cli), common.fmt_secs(t_script)))

    meta = _project_meta(pdir)
    boundary = common.content_boundary(meta)
    project_name = str(meta.get("name") or pdir.name)
    style_channel = str(meta.get("style") or "photo")
    if style_channel not in ("photo", "anime"):
        style_channel = "photo"
    moves, intensities = common.camera_vocab_names()
    try:
        g = common.grid_bounds(cfg.get("video") or {})
    except common.BudgetError as e:
        log("[breakdown] %s" % e)
        return 1
    if n_req and budget:
        try:
            common.feasibility(n_req, budget, g)
        except common.BudgetError as e:
            log("[breakdown] %s" % e)
            return 1

    if dry:
        log("[DRY-RUN] breakdown（LLM 模式）：剧本=%s 预算=%s镜 × %ss → %s"
            % (script.name, n_req or "?", common.fmt_secs(budget) if budget else "?",
               "/".join(_TARGETS)))
        return 0

    messages = [
        {"role": "system", "content": prompts.breakdown_system(boundary, moves, intensities)},
        {"role": "user", "content": prompts.breakdown_user(script_text, n_req, budget)},
    ]
    feedback: list[str] | None = None
    last_raw = ""
    docs: tuple[dict, dict, dict] | None = None
    warnings: list[str] = []
    solved = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        msgs = list(messages)
        if feedback is not None:
            msgs.append({"role": "user",
                         "content": prompts.retry_feedback(feedback, last_raw, "breakdown")})
        try:
            raw = chat_fn(msgs, temperature=0.4)
        except llm_client.LLMError as e:
            log("[breakdown] %s" % e)
            return 1
        last_raw = raw
        docs = None
        try:
            data = extract_json(raw)
        except ValueError as e:
            feedback = [str(e)]
            log("  第 %d/%d 次：JSON 解析失败，携错误重试…" % (attempt, MAX_ATTEMPTS))
            continue
        try:
            shots_doc, chars_doc, subs_doc, warnings = build_docs(
                data, cfg=cfg, n_required=n_req, budget=budget,
                project_name=project_name, style_channel=style_channel,
                moves=moves, intensities=intensities)
            docs = (shots_doc, chars_doc, subs_doc)
        except BreakdownError as e:
            feedback = e.problems
            log("  第 %d/%d 次：拆解校验未过（%d 项），携清单重试…"
                % (attempt, MAX_ATTEMPTS, len(e.problems)))
            continue
        problems = _validate_docs(pdir, docs)
        if not problems:
            solved = True
            break
        feedback = problems
        log("  第 %d/%d 次：schema 校验未过（%d 项），携清单重试…"
            % (attempt, MAX_ATTEMPTS, len(problems)))
    if not solved:
        _keep_drafts(pdir, docs, last_raw, feedback or [], log)
        return 1

    assert docs is not None
    texts = {}
    for fname, doc in zip(_TARGETS, docs):
        text = emit_yaml(doc)
        if miniyaml.parse(text) != doc:  # 往返校验：防自写发射器漂移
            log("  [提示] %s 往返不等价，回退 miniyaml.dumps（请报告框架 bug）。" % fname)
            text = miniyaml.dumps(doc)
        texts[fname] = text
    ok, err = _commit(pdir, texts)
    if not ok:
        log("[breakdown] %s" % err)
        _keep_drafts(pdir, docs, last_raw, ["写入/终检失败：%s" % err], log)
        return 1
    try:  # 磁盘终检：装载路径与人工填写完全一致
        schema.load_project_data(pdir, cfg)
    except (schema.SchemaError, miniyaml.MiniYAMLError) as e:
        _rollback(pdir)
        log("[breakdown] 磁盘终检未过（已回滚旧版）：%s" % e)
        _keep_drafts(pdir, docs, last_raw, [str(e)], log)
        return 1
    for w in warnings:
        log("  [提示] %s" % w)
    shots_doc, chars_doc, subs_doc = docs
    total = sum(float(sh.get("duration_s")) for sh in shots_doc["shots"])
    budget_note = ("（预算 %ss）" % common.fmt_secs(budget)) if budget else ""
    log("breakdown 完成：%d 镜 / 总时长 %.2fs%s / %d 角色 / cues %d → %s（旧版备份 *.bak）"
        % (len(shots_doc["shots"]), total, budget_note,
           len(chars_doc["characters"]), len(subs_doc["cues"]), ", ".join(_TARGETS)))
    log("下一步：人工过目三份 yaml → python -m pipeline run %s --stage assets" % project_name)
    return 0


# --------------------------------------------------------------------------- #
# 校验器模式（未配置 LLM）
# --------------------------------------------------------------------------- #
def _validator_mode(pdir: Path, cfg: dict, log) -> int:
    log("breakdown 校验器模式（未配置 LLM）：只校验人工填写的三份 yaml，不生成、不写盘。")
    log("  %s" % llm_client.not_configured_hint(cfg))
    try:
        data = schema.load_project_data(pdir, cfg)
    except (schema.SchemaError, miniyaml.MiniYAMLError) as e:
        log("校验未通过：")
        for line in str(e).splitlines():
            log("  %s" % line)
        log("逐条修正后重跑；配置 llm 段后本段改为 LLM 自动拆解。")
        return 1
    shots = data["shots"]["shots"]
    cues = (data.get("subtitles") or {}).get("cues") or []
    total = sum(schema.eff_duration(sh, data["shots"], cfg) for sh in shots)
    log("校验通过：角色 %d / 镜 %d / 字幕 cues %d / 总时长 %.2fs。"
        % (len(data["characters"]["characters"]), len(shots), len(cues), total))
    script = pdir / "剧本.md"
    src = script if script.is_file() else (pdir / "剧本模板.md")
    if src.is_file():
        _n, t = common.parse_budget_line(src.read_text(encoding="utf-8-sig"))
        if t:
            g = common.grid_bounds((cfg or {}).get("video") or {})
            if total > t + g["step_s"]:
                log("[提示] 总时长 %.2fs 超出 %s 预算行 %ss（仍可渲染；如需收紧请压时长或减镜）。"
                    % (total, src.name, common.fmt_secs(t)))
    return 0


# --------------------------------------------------------------------------- #
# JSON 抽取与确定性后处理
# --------------------------------------------------------------------------- #
def extract_json(raw: str) -> dict:
    """从 LLM 输出抽取 JSON 对象（容忍代码围栏/前后缀文本）；失败抛 ValueError。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
        if m:
            text = m.group(1)
    try:
        obj = json.loads(text)
    except ValueError:
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise ValueError("LLM 输出中找不到 JSON 对象（前 120 字：%s…）" % text[:120])
        try:
            obj = json.loads(text[i:j + 1])
        except ValueError as e:
            raise ValueError("JSON 解析失败：%s（截取段前 120 字：%s…）" % (e, text[i:i + 120]))
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层须为对象，得到 %s" % type(obj).__name__)
    return obj


def fit_durations(raw: list[float], budget: float, g: dict, warnings: list[str]) -> list[float]:
    """逐镜：夹取进请求域 → 网格落点；总预算超限时降最长镜档位直至命中（±1 步容差）。

    返回可直接写进 shots.yaml 的请求值（2 位小数、且重映射回同一 k）。
    """
    fps, a, b = g["fps"], g["a"], g["b"]
    ks = []
    for d in raw:
        d = float(d)
        if not (g["min_request_s"] - 1e-9 <= d <= g["max_request_s"] + 1e-9):
            clamped = min(max(d, g["min_request_s"]), g["max_request_s"])
            warnings.append("时长 %ss 超出请求域，已夹取为 %ss（网格按 docs §4.4）。"
                            % (common.fmt_secs(d), common.fmt_secs(clamped)))
            d = clamped
        k = int(round((d * fps - b) / a))
        ks.append(min(max(k, g["k_min"]), g["k_max"]))
    if budget > 0:
        slack = g["step_s"]
        total = sum(a * k + b for k in ks) / fps
        while total > budget + slack:
            cand = [i for i, k in enumerate(ks) if k > g["k_min"]]
            if not cand:
                raise BreakdownError([
                    "预算不可行：%d 镜 × 最短 %.2fs ≈ %.2fs > 预算 %.2fs——"
                    "请合并镜头减少镜数，或要求提高预算（当前约束来自 --total-s/剧本预算行）"
                    % (len(ks), g["min_eff"], len(ks) * g["min_eff"], budget)])
            pick = max(cand, key=lambda i: (ks[i], -i))
            ks[pick] -= 1
            total = sum(a * k + b for k in ks) / fps
        if total < budget - 10 * g["step_s"]:
            warnings.append("各镜合计 %.2fs 明显低于预算 %.2fs（如非刻意留白可上调部分镜时长）。"
                            % (total, budget))
    out = []
    for k in ks:
        eff = (a * k + b) / fps
        v = round(min(eff, g["max_request_s"]), 2)
        k2 = int(round((v * fps - b) / a))
        if k2 != k:  # 2 位小数重映射漂移（理论上不发生）：加精度
            v = round(min(eff, g["max_request_s"]), 3)
        out.append(v)
    return out


def _norm_motion(text: str) -> str:
    lines = [ln.rstrip() for ln in str(text or "").replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip("\n")


def build_docs(data: dict, *, cfg: dict, n_required: int, budget: float,
               project_name: str, style_channel: str, moves: list[str],
               intensities: list[str]) -> tuple[dict, dict, dict, list[str]]:
    """LLM JSON → (shots_doc, chars_doc, subs_doc, warnings)；问题攒 BreakdownError。"""
    g = common.grid_bounds((cfg or {}).get("video") or {})
    warnings: list[str] = []
    problems: list[str] = []
    chars_raw = data.get("characters")
    shots_raw = data.get("shots")
    catalog_raw = data.get("props_catalog") or {}
    if not isinstance(chars_raw, list) or not chars_raw:
        problems.append("characters 须为非空列表")
    if not isinstance(shots_raw, list) or not shots_raw:
        problems.append("shots 须为非空列表")
    if not isinstance(catalog_raw, dict):
        problems.append("props_catalog 须为 道具名→英文锚点 的映射")
    if problems:
        raise BreakdownError(problems)
    if n_required and len(shots_raw) != n_required:
        problems.append("镜数 %d 与要求 %d 不符（要求来自 --shots 或剧本预算行；"
                        "请严格按台本分镜表逐镜输出）" % (len(shots_raw), n_required))

    # ---- characters ----
    chars_doc, keep_ids = [], set()
    for i, ch in enumerate(chars_raw, 1):
        if not isinstance(ch, dict):
            problems.append("characters[%d] 须为映射" % i)
            continue
        cid = str(ch.get("id") or "").strip()
        if not common.ASCII_ID_RE.match(cid):
            problems.append("characters[%d].id=%r 须为 ASCII snake_case（文件名安全）" % (i, cid))
            continue
        if cid in keep_ids:
            problems.append("characters.id 重复: %s" % cid)
            continue
        keep_ids.add(cid)
        anchor = str(ch.get("anchor_en") or "").strip()
        if not anchor:
            problems.append("characters[%s].anchor_en 缺失（英文一致性锚点）" % cid)
        expressions, props = [], []
        for key, bucket in (("expressions", expressions), ("props", props)):
            v = ch.get(key) or []
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                problems.append("characters[%s].assets.%s 须为字符串列表" % (cid, key))
                continue
            for x in v:
                x = x.strip()
                if not x:
                    continue
                if not common.ASCII_ID_RE.match(x):
                    problems.append("characters[%s].%s 项 %r 须为 ASCII（作产物文件名）" % (cid, key, x))
                    continue
                bucket.append(x)
        chars_doc.append({
            "id": cid,
            "name": str(ch.get("name") or cid).strip() or cid,
            "anchor_en": anchor,
            "outfit_en": str(ch.get("outfit_en") or "").strip(),
            "negative_extra": str(ch.get("negative_extra") or "").strip(),
            "assets": {"three_view": bool(ch.get("three_view", True)),
                       "expressions": expressions, "props": props},
        })
    catalog_doc = {}
    for k, v in catalog_raw.items():
        key = str(k).strip()
        if not common.ASCII_ID_RE.match(key):
            problems.append("props_catalog 键 %r 须为 ASCII（作产物文件名）" % key)
            continue
        if not isinstance(v, str) or not v.strip():
            problems.append("props_catalog[%s] 须为非空英文锚点" % key)
            continue
        catalog_doc[key] = v.strip()

    # ---- durations ----
    if problems:
        raise BreakdownError(problems)
    durs_raw = []
    for j, sh in enumerate(shots_raw, 1):
        d = sh.get("duration_s", g["default_s"]) if isinstance(sh, dict) else None
        if isinstance(d, bool) or not isinstance(d, (int, float)):
            problems.append("shots[%d].duration_s=%r 须为数值（5–15）" % (j, d))
            durs_raw.append(g["default_s"])
        else:
            durs_raw.append(float(d))
    try:
        durs = fit_durations(durs_raw, budget, g, warnings)
    except BreakdownError as e:
        problems.extend(e.problems)
        raise BreakdownError(problems)

    # ---- shots + cues ----
    shot_docs, cues = [], []
    for j, (sh, dur) in enumerate(zip(shots_raw, durs), 1):
        sid = "S%d" % j
        if not isinstance(sh, dict):
            problems.append("shots[%d] 须为映射" % j)
            continue
        for key in ("first", "last", "motion"):
            if not str(sh.get(key) or "").strip():
                problems.append("%s.%s 缺失（英文，H3/生图链路必需）" % (sid, key))
        for key in ("first_zh", "last_zh", "motion_zh"):
            if not str(sh.get(key) or "").strip():
                problems.append("%s.%s 缺失（中文对照，双语契约）" % (sid, key))
        sub = str(sh.get("subtitle") or "").strip()
        if not sub:
            problems.append("%s.subtitle 缺失（每镜一条中文台词/旁白）" % sid)
        move = str(sh.get("camera_move") or "push_in").strip()
        intensity = str(sh.get("camera_intensity") or "slow").strip()
        if move not in moves:
            problems.append("%s.camera_move=%r 不在运镜词库（可用：%s）"
                            % (sid, move, "/".join(moves)))
        if intensity not in intensities:
            problems.append("%s.camera_intensity=%r 须为 %s" % (sid, intensity, "/".join(intensities)))
        last_mode = str(sh.get("last_mode") or "edit").strip()
        if last_mode not in ("edit", "fresh"):
            problems.append("%s.last_mode=%r 须为 edit|fresh" % (sid, last_mode))
            last_mode = "edit"
        chars_list = sh.get("characters") or []
        if not isinstance(chars_list, list) or not all(isinstance(x, str) for x in chars_list):
            problems.append("%s.characters 须为字符串列表（引用 characters.id）" % sid)
            chars_list = []
        shot_docs.append({
            "id": sid,
            "title": str(sh.get("title") or ("镜头%d" % j)).strip() or ("镜头%d" % j),
            "duration_s": dur,
            "characters": [str(c).strip() for c in chars_list],
            "scene": str(sh.get("scene") or "").strip(),
            "scene_zh": str(sh.get("scene_zh") or "").strip(),
            "first": str(sh.get("first") or "").strip(),
            "first_zh": str(sh.get("first_zh") or "").strip(),
            "last": str(sh.get("last") or "").strip(),
            "last_zh": str(sh.get("last_zh") or "").strip(),
            "last_mode": last_mode,
            "use_char_ref": False,
            "camera": {"mode": "B", "move": move, "intensity": intensity, "extra": ""},
            "motion": _norm_motion(sh.get("motion")),
            "motion_zh": _norm_motion(sh.get("motion_zh")),
            "audio_hint": str(sh.get("audio_hint") or "").strip(),
            "negative_extra": "",
            "seed": 0,  # 0 = 按 docs §3 规则 4 派生（keyframes/video 段统一公式）
        })
        if sub:
            t_in = 0.5
            t_out = round(min(dur - 0.3, 0.5 + max(1.0, len(sub) / 5.0)), 2)
            if t_out <= t_in:
                t_out = round(min(dur, t_in + 1.0), 2)
            cues.append({"shot": sid, "t_in": t_in, "t_out": t_out, "text": sub})
    if problems:
        raise BreakdownError(problems)

    default_dur = sorted(durs)[len(durs) // 2]
    shots_doc = {
        "project": project_name,
        "style": style_channel,
        "defaults": {"duration_s": default_dur,
                     "camera": {"mode": "B", "move": "push_in", "intensity": "slow"}},
        "shots": shot_docs,
    }
    chars_full = {"characters": chars_doc, "props_catalog": catalog_doc}
    subs_doc = {
        "project": project_name,
        "title_card": {"text": project_name, "t_in": 0.9, "t_out": 3.0},
        "end_card": {"text": "—— 完 ——", "tail_s": 4.0},
        "cues": cues,
    }
    return shots_doc, chars_full, subs_doc, warnings


def _validate_docs(pdir: Path, docs) -> list[str]:
    """内存态 schema 校验（与人工填写同一把尺：pipeline.schema）。"""
    shots_doc, chars_doc, subs_doc = docs
    try:
        project_doc = config_mod.load_project_file(pdir)
    except (config_mod.ConfigError, miniyaml.MiniYAMLError) as e:
        return ["project.yaml 无法装载：%s" % e]
    data = {"dir": Path(pdir), "project": project_doc, "characters": chars_doc,
            "shots": shots_doc, "subtitles": subs_doc}
    return schema.validate_all(data)


# --------------------------------------------------------------------------- #
# YAML 发射（自写美化版；发射后立即 miniyaml 往返校验，不等价则回退 dumps）
# --------------------------------------------------------------------------- #
_BARE_RE = re.compile(r"^[A-Za-z0-9_]+$")
_BARE_FORBIDDEN = {"true", "false", "null", "none", "~", "yes", "no", "on", "off", "y", "n"}


def _scalar(v) -> str:
    if v is None:
        return '""'
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(round(float(v), 3))
    s = str(v)
    if _BARE_RE.match(s) and s.lower() not in _BARE_FORBIDDEN:
        return s
    return json.dumps(s, ensure_ascii=False)  # 双引号转义与 miniyaml 解析规则兼容


def _kv(out: list, indent: int, key: str, v, prefix: str | None = None) -> None:
    pad = " " * indent
    head = (prefix if prefix is not None else pad) + key + ":"
    if isinstance(v, dict):
        if not v:
            out.append(head + " {}")
            return
        out.append(head)
        for k2, v2 in v.items():
            _kv(out, indent + 2, str(k2), v2)
    elif isinstance(v, list):
        if not v:
            out.append(head + " []")
            return
        if all(isinstance(x, str) and _BARE_RE.match(x) for x in v) \
                and sum(len(x) + 2 for x in v) <= 56:
            out.append(head + " [" + ", ".join(v) + "]")
            return
        out.append(head)
        _seq(out, indent + 2, v)
    elif isinstance(v, str) and "\n" in v and _MULTILINE_OK:
        out.append(head + " |-")
        for ln in v.split("\n"):
            out.append((" " * (indent + 2) + ln) if ln.strip() else "")
    else:
        out.append(head + " " + _scalar(v))


def _seq(out: list, indent: int, items: list) -> None:
    pad = " " * indent
    for item in items:
        if isinstance(item, dict) and item:
            first = True
            for k, v in item.items():
                _kv(out, indent + 2, str(k), v, prefix=(pad + "- ") if first else None)
                first = False
        elif isinstance(item, list) and item:
            out.append(pad + "-")
            _seq(out, indent + 2, item)
        else:
            out.append(pad + "- " + _scalar(item))


def emit_yaml(doc: dict) -> str:
    """dict → 受 miniyaml 支持集的 YAML 文本（块风格 + 多行 |- + 短 flow 序列）。"""
    out: list[str] = []
    for k, v in doc.items():
        _kv(out, 0, str(k), v)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# 落盘：备份 → 写入 → 终检 → 失败回滚
# --------------------------------------------------------------------------- #
def _commit(pdir: Path, texts: dict) -> tuple[bool, str]:
    saved: dict[str, bytes | None] = {}
    try:
        for fname, text in texts.items():
            p = pdir / fname
            saved[fname] = p.read_bytes() if p.is_file() else None
            if saved[fname] is not None:
                (pdir / (fname + ".bak")).write_bytes(saved[fname])
            p.write_text(text, encoding="utf-8", newline="\n")
    except OSError as e:
        _rollback(pdir, saved)
        return False, "写入失败：%s（已回滚）" % e
    return True, ""


def _rollback(pdir: Path, saved: dict | None = None) -> None:
    if saved is None:
        saved = {}
        for fname in _TARGETS:
            p = pdir / fname
            saved[fname] = p.read_bytes() if p.is_file() else None
    for fname, prev in saved.items():
        p = pdir / fname
        try:
            if prev is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(prev)
        except OSError:
            pass  # 回滚尽力而为；错误路径以日志与 draft 为准


def _keep_drafts(pdir: Path, docs, last_raw: str, problems: list[str], log) -> None:
    if docs is not None:
        for fname, doc in zip(_TARGETS, docs):
            try:
                (pdir / fname.replace(".yaml", ".draft.yaml")).write_text(
                    emit_yaml(doc), encoding="utf-8", newline="\n")
            except OSError as e:
                log("  [警告] draft 写入失败 %s：%s" % (fname, e))
    if last_raw:
        (pdir / _LAST_RAW_FNAME).write_text(last_raw, encoding="utf-8", newline="\n")
    review = "\n".join(["breakdown 校验问题（%d 项，共 %d 次尝试）：" % (len(problems), MAX_ATTEMPTS)]
                       + ["- %s" % p for p in problems])
    (pdir / _REVIEW_FNAME).write_text(review + "\n", encoding="utf-8", newline="\n")
    log("[breakdown] %d 次均未通过校验：最后一次产物保留为 *.draft.yaml（如已成型）与 %s，"
        "问题清单见 %s（供人工修补），退出码 1。"
        % (MAX_ATTEMPTS, _LAST_RAW_FNAME, _REVIEW_FNAME))


def _project_meta(pdir: Path) -> dict:
    try:
        meta = config_mod.load_project_file(pdir)
        return meta if isinstance(meta, dict) else {}
    except (config_mod.ConfigError, miniyaml.MiniYAMLError):
        return {}
