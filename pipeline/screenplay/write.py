# -*- coding: utf-8 -*-
"""screenplay-write（段0·编剧）：创意/大纲 → 剧本.md。

输入：--premise（题材一句话）/--style-note（风格自由文本）/--shots（镜数）/
--total-s（总时长预算，秒）。预算先过网格可行性检查（镜数 × H3 时长可达域，
docs §4.4），不可行一句报错退出。

两种模式：
  - LLM 模式（config ``llm:`` 段已配置）：按项目台本模板结构生成 Markdown 台本
    （元信息含机器可读预算行 / 人物设定含 English anchor / 三幕结构 / 分镜表六要素 /
    技术流水线），生成后跑结构 lint；不过则把问题清单带回 LLM 重生成，**最多 3 次**，
    仍失败 → 剧本.draft.md + screenplay_review.txt 保留最后一次产物，exit 1。
  - 模板模式（未配置 LLM）：降级生成填好预算/题材的骨架 剧本.md（S1..SN 空表），
    提示信息友好（两个已核实 LLM 示例的拉起方式），exit 0。

产物：projects/<名>/剧本.md（覆盖前旧版备份 剧本.md.bak）。剧本是自由文体，
框架不解析它（docs §6），结构化数据只认 yaml——拆解归 breakdown。
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import config as config_mod
from .. import miniyaml
from . import common, llm_client, prompts

__all__ = ["run", "lint", "template_md", "MAX_ATTEMPTS"]

MAX_ATTEMPTS = 3
DEFAULT_N_SHOTS = 8
_REVIEW_FNAME = "screenplay_review.txt"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run(project_dir, cfg: dict, *, args=None, opts=None, chat_fn=None, log=print) -> int:
    pdir = Path(project_dir)
    dry = bool(getattr(opts, "dry_run", False))
    premise = str(getattr(args, "premise", "") or "").strip()
    style_note = str(getattr(args, "style_note", "") or "").strip()
    n_shots = int(getattr(args, "shots", 0) or 0) or DEFAULT_N_SHOTS
    total_s = float(getattr(args, "total_s", 0.0) or 0.0)
    if n_shots <= 0:
        log("[screenplay-write] --shots 须为正整数，得到 %r" % n_shots)
        return 1

    g = common.grid_bounds((cfg or {}).get("video") or {})
    if total_s <= 0:
        total_s = round(n_shots * g["default_s"], 2)
    try:
        common.feasibility(n_shots, total_s, g)
    except common.BudgetError as e:
        log("[screenplay-write] %s" % e)
        return 1
    rec_s = common.recommend_duration(n_shots, total_s, g)

    meta = _project_meta(pdir)
    boundary = common.content_boundary(meta)
    project_name = str(meta.get("name") or pdir.name)
    channel = str(meta.get("style") or "photo")
    moves, intensities = common.camera_vocab_names()
    target = pdir / "剧本.md"

    if chat_fn is None:
        chat_fn = llm_client.make_chat_fn(cfg)
    if chat_fn is None:
        return _template_mode(pdir, cfg, target, premise=premise, style_note=style_note,
                              project_name=project_name, channel=channel,
                              n_shots=n_shots, total_s=total_s, rec_s=rec_s, dry=dry, log=log)

    if not premise:
        log("[screenplay-write] LLM 模式需要 --premise（题材一句话）。"
            "或清空 config llm 段走模板模式（骨架 + 人工创作）。")
        return 1
    if dry:
        log("[DRY-RUN] screenplay-write（LLM 模式）：premise=%r 风格=%r 预算=%d 镜 × %ss"
            "（每镜建议 %.2fs）→ %s" % (premise, style_note or "-", n_shots,
                                       common.fmt_secs(total_s), rec_s, target))
        return 0

    messages = [
        {"role": "system", "content": prompts.write_system(boundary, moves, intensities)},
        {"role": "user", "content": prompts.write_user(project_name, channel, premise,
                                                       style_note, n_shots, total_s, rec_s)},
    ]
    feedback: list[str] | None = None
    last_text = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        msgs = list(messages)
        if feedback is not None:
            msgs.append({"role": "user",
                         "content": prompts.retry_feedback(feedback, last_text, "write")})
        try:
            text = chat_fn(msgs, temperature=0.7).strip()
        except llm_client.LLMError as e:
            log("[screenplay-write] %s" % e)
            return 1
        last_text = text
        problems = lint(text, n_shots, total_s)
        if not problems:
            _save_with_backup(target, text, log)
            log("剧本已生成：%s（%d 镜 / 预算 %ss / 每镜建议 %.2fs）"
                % (target, n_shots, common.fmt_secs(total_s), rec_s))
            log("下一步：python -m pipeline run %s --stage breakdown（拆解为 shots/characters/subtitles）"
                % project_name)
            return 0
        feedback = problems
        log("  第 %d/%d 次结构检查未过（%d 项），携问题清单重生成…" % (attempt, MAX_ATTEMPTS, len(problems)))
    _keep_draft(pdir, last_text, feedback or [], log, kind="剧本")
    return 1


# --------------------------------------------------------------------------- #
# 结构 lint（轻校验：章节/锚点/镜号连续/预算行；强 schema 校验在 breakdown）
# --------------------------------------------------------------------------- #
def lint(text: str, n_shots: int, total_s: float) -> list[str]:
    problems: list[str] = []
    for kw in ("人物设定", "三幕", "分镜表"):
        if kw not in text:
            problems.append("缺章节：%s（结构须与台本模板一致：元信息/人物设定/三幕/分镜表/流水线）" % kw)
    if "english anchor" not in text.lower():
        problems.append("人物设定缺 English anchor 行（每个角色一行反引号包裹的英文一致性锚点）")
    ids = [int(x) for x in re.findall(r"^###\s*S(\d+)", text, re.M)]
    uniq = sorted(set(ids))
    if not ids:
        problems.append("分镜表缺镜条目（每镜一节 `### S<k> 镜名（约 X.XXs）`）")
    elif uniq != list(range(1, n_shots + 1)):
        problems.append("镜号应恰为 S1–S%d 连续 %d 镜，当前：S%s"
                        % (n_shots, n_shots, "/".join(str(i) for i in uniq) or "无"))
    got_n, got_t = common.parse_budget_line(text)
    if not got_n:
        problems.append("元信息缺机器可读预算行：`- 预算: %d 镜 × %ss`（格式精确）"
                        % (n_shots, common.fmt_secs(total_s)))
    elif got_n != n_shots or abs(got_t - total_s) > 0.5:
        problems.append("预算行与要求不符：写的是 %d 镜 × %ss，要求 %d 镜 × %ss"
                        % (got_n, common.fmt_secs(got_t), n_shots, common.fmt_secs(total_s)))
    if "```" in text:
        problems.append("不要用代码围栏包裹 Markdown（直接输出正文）")
    if "<think>" in text.lower():
        problems.append("输出含 <think> 推理残留（只输出正文）")
    return problems


# --------------------------------------------------------------------------- #
# 模板模式（未配置 LLM 的降级路径）
# --------------------------------------------------------------------------- #
def _template_mode(pdir, cfg, target, *, premise, style_note, project_name, channel,
                   n_shots, total_s, rec_s, dry, log) -> int:
    hint = llm_client.not_configured_hint(cfg)
    if dry:
        log("[DRY-RUN] screenplay-write（模板模式）：%s" % hint)
        log("  将生成 %s：题材=%r 风格=%r 预算=%d 镜 × %ss（每镜建议 %.2fs）"
            % (target, premise or "（占位待填）", style_note or "-", n_shots,
               common.fmt_secs(total_s), rec_s))
        return 0
    text = template_md(project_name=project_name, channel=channel, premise=premise,
                       style_note=style_note, n_shots=n_shots, total_s=total_s, rec_s=rec_s)
    _save_with_backup(target, text, log)
    log("[screenplay-write] %s" % hint)
    log("已生成骨架 %s（%d 镜 / 每镜建议 %.2fs）。请人工创作后："
        % (target, n_shots, rec_s))
    log("  ① 继续用 LLM：配置 llm 段后重跑本段；② 纯人工：直接编辑三份 yaml，"
        "用 run breakdown（校验器模式）检查。")
    return 0


def template_md(*, project_name: str, channel: str, premise: str, style_note: str,
                n_shots: int, total_s: float, rec_s: float) -> str:
    """模板模式的骨架台本（结构=脚手架 剧本模板.md；预算行供 breakdown 解析）。"""
    rows = []
    for i in range(1, n_shots + 1):
        rows.append("### S%d （约 %.2fs）\n- 首帧: \n- 尾帧: \n- H3: \n"
                    "- 运镜: push_in × slow\n- Audio: \n- 字幕: " % (i, rec_s))
    return """# 《%s》 — 分镜台本（模板模式·待人工创作）

> 本文件由 screenplay-write 以模板模式生成（LLM 未配置）。结构沿用项目台本模板：
> 结构化数据只认 characters.yaml / shots.yaml / subtitles.yaml，本文件是创作中间层。

## 1. 元信息

- 题材: %s
- 风格: %s
- 预算: %d 镜 × %ss
- 每镜建议时长: 约 %.2fs（H3 时长网格可达 5.17–15.08s，按 17k+5 帧命中）
- 通道: %s（photo=写实漫剧 / anime=动画插画）；画幅 16:9（1344×768），24fps
- 尺度决策: 框架默认中性叙事、全体角色为成年人（18+）；项目尺度可在 project.yaml
  的 screenplay.content_rating 自行声明

## 2. 人物设定（每角色一小节；English anchor 是全链一致性锚点，请认真写）

**角色A（成年）** — 发型/瞳色/体态一句话；标准着装（一致性以着装态为准）；随身道具。
English anchor: `a 24-year-old ..., hair, eyes, figure, standard outfit, props`

## 3. 三幕结构（每幕一句话 + 覆盖镜号）

- 第一幕（S1–S%d）: 
- 第二幕（S…–S…）: 
- 第三幕（S…–S%d）: 

## 4. 分镜表（每镜六要素：首帧/尾帧/H3/运镜/Audio/字幕）

%s

## 5. 技术流水线

assets（角色三视图/表情/道具）→ keyframes（首/末帧）→ camera（模式B 词库 /
模式A Blender）→ video（H3 首尾帧图生视频）→ final（字幕烧录合成）。
本文件仅供创作与 breakdown 参考，框架不解析它。
""" % (project_name, premise or "（请填写题材一句话）", style_note or "（请填写风格）",
       n_shots, common.fmt_secs(total_s), rec_s, channel,
       max(1, n_shots // 3), n_shots, "\n\n".join(rows))


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _project_meta(pdir: Path) -> dict:
    try:
        meta = config_mod.load_project_file(pdir)
        return meta if isinstance(meta, dict) else {}
    except (config_mod.ConfigError, miniyaml.MiniYAMLError):
        return {}


def _save_with_backup(path: Path, text: str, log) -> None:
    if path.is_file():
        prev = path.read_bytes()
        if prev != text.encode("utf-8"):
            path.with_name(path.name + ".bak").write_bytes(prev)
            log("  旧版已备份：%s.bak" % path.name)
    path.write_text(text, encoding="utf-8", newline="\n")


def _keep_draft(pdir: Path, last_text: str, problems: list[str], log, kind: str) -> None:
    (pdir / ("%s.draft.md" % kind)).write_text(last_text or "", encoding="utf-8", newline="\n")
    review = "\n".join(["screenplay %s 校验问题（%d 项，共 %d 次尝试）：" % (kind, len(problems), MAX_ATTEMPTS)]
                       + ["- %s" % p for p in problems])
    (pdir / _REVIEW_FNAME).write_text(review + "\n", encoding="utf-8", newline="\n")
    log("[screenplay-write] %d 次均未通过结构检查：最后产物保留在 %s.draft.md，"
        "问题清单见 %s（供人工修补），退出码 1。" % (MAX_ATTEMPTS, kind, _REVIEW_FNAME))
