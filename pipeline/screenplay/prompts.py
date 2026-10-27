# -*- coding: utf-8 -*-
"""screenplay 段0 提示词构造（write / breakdown 共用）。

内容边界：common.CONTENT_BOUNDARY_DEFAULT（中性·全成年）为硬底线，项目可在
project.yaml screenplay.content_rating 声明尺度，common.content_boundary() 负责合成。
台本结构沿用脚手架 剧本模板.md（docs §6）：元信息 → 人物设定（含 English anchor）
→ 三幕结构 → 分镜表（首帧/尾帧/H3/运镜/声音/字幕）→ 技术流水线。
"""
from __future__ import annotations

from . import common

__all__ = ["write_system", "write_user", "breakdown_system", "breakdown_user",
           "retry_feedback", "JSON_SPEC"]

JSON_SPEC = """{
  "characters": [            // 角色（≥1）
    {"id": "ASCII snake_case",            // 如 lin_yu；文件名安全
     "name": "中文名",
     "anchor_en": "一句英文一致性锚点：a 24-year-old ..., hair, eyes, figure（成年人）",
     "outfit_en": "标准着装英文（全剧一致性锚点以着装态为准）",
     "negative_extra": "该角色专属负面词（可空串）",
     "three_view": true,                   // 剪影/无脸角色可 false
     "expressions": ["worried", "smile"],  // ASCII；表情差分，可 []
     "props": ["vinyl_umbrella"]}          // ASCII；须出现在 props_catalog
  ],
  "props_catalog": {"vinyl_umbrella": "transparent vinyl umbrella, rain droplets on canopy"},
  "shots": [                // 分镜（按成片顺序）
    {"title": "中文镜名",
     "characters": ["lin_yu"],
     "scene": "英文场景+氛围（H3 提示词首段）",
     "scene_zh": "中文场景",
     "first": "英文首帧画面描述（构图/人物姿态/环境；不含画风锚点）",
     "first_zh": "中文首帧描述",
     "last": "英文末帧相对变化（相对 first 改了什么）",
     "last_zh": "中文末帧变化",
     "last_mode": "edit",                  // edit（保身份默认）| fresh（大机位变化）
     "motion": "[0s-2s] english action ...\\n[2s-5s] english action ...",  // H3 Timeline 多行
     "motion_zh": "中文动效时间线",
     "camera_move": "push_in",             // 只能取运镜词库 move
     "camera_intensity": "slow",           // slow|medium|fast
     "audio_hint": "english ambient sound",
     "duration_s": 5.2,                    // 5–15s；各镜合计不超总预算
     "subtitle": "该镜中文台词/旁白（一条）"}
  ]
}"""


def write_system(boundary: str, moves: list[str], intensities: list[str]) -> str:
    return """你是资深短剧编剧，为本 AI 漫剧生产框架撰写分镜台本（Markdown）。硬性要求：
1. %s
2. 只输出 Markdown 正文：不要代码围栏、不要任何解释性文字。
3. 结构固定五节（与项目台本模板一致）：
   ## 1. 元信息 —— 必须包含一行精确格式「- 预算: {N} 镜 × {T}s」（机器解析用，勿改格式勿增字）；
   ## 2. 人物设定 —— 每个角色一小节，含一行「English anchor: `...`」英文一致性锚点
      （年龄一律成年人；发型/瞳色/体态/标准着装/随身道具各一句话）与道具清单；
   ## 3. 三幕结构 —— 每幕一句话 + 覆盖镜号（如 第一幕 S1–S4）；
   ## 4. 分镜表 —— 每镜一节「### S<k> 镜名（约 X.XXs）」，依次字段：
      首帧（英文画面描述）/ 尾帧（英文相对变化）/ H3（[0s-Xs] 英文动作时间线，覆盖整镜）/
      运镜（move × intensity）/ Audio（英文环境声）/ 字幕（中文台词）；
   ## 5. 技术流水线 —— 一句话即可。
4. 运镜 move 只能取：%s；intensity 只能取：%s。
5. 每镜时长 5–15s，合计不超预算；相邻镜头首尾可衔接；末帧默认保身份改画（edit），
   只有存在明显大机位变化时才在该镜注明 fresh。
6. 剧情连贯、有起承转合；台词精炼（每镜一句为主）。""" % (
        boundary, "/".join(moves), "/".join(intensities))


def write_user(project_name: str, style_channel: str, premise: str, style_note: str,
               n_shots: int, total_s: float, rec_s: float) -> str:
    return """请为项目《%s》写台本。

- 题材一句话：%s
- 风格：%s
- 通道：%s（photo=写实漫剧 / anime=动画插画，仅作画面基调参考）
- 硬预算：恰好 %d 镜，总时长约 %s s；元信息必须含一行「- 预算: %d 镜 × %ss」
  （精确照写此格式，供机器解析）。
  每镜 5–15s，建议每镜约 %.2fs，可按叙事需要微调。
- 元信息节请同时给出类型/画幅（16:9，1344×768，24fps）/尺度决策一行。""" % (
        project_name, premise or "（自拟，中性叙事）", style_note or "（自拟，与题材相称）",
        style_channel, n_shots, common.fmt_secs(total_s), n_shots,
        common.fmt_secs(total_s), rec_s)


def breakdown_system(boundary: str, moves: list[str], intensities: list[str]) -> str:
    return """你是分镜拆解器：把台本（Markdown）拆成结构化 JSON 供渲染管线消费。硬性要求：
1. %s
2. 只输出一个 JSON 对象：不要代码围栏、不要注释、不要多余文字。
3. JSON 结构（字段名精确照用）：
%s
4. 语言：英文字段（scene/first/last/motion/audio_hint/anchor_en/outfit_en/
   props_catalog 的值）一律英文，且**不含画风锚点**（渲染时框架自动拼接）；中文锚字段
   （*_zh/name/title/subtitle）一律中文。
5. id 与 props/expressions 项一律 ASCII snake_case（作文件名）；shots[].characters
   必须引用 characters[].id；每个被引用的 prop 必须出现在 props_catalog。
6. camera_move 只能取：%s；camera_intensity 只能取：%s（运镜模式B 词库标注）。
7. duration_s 每镜 5–15；镜数与总预算服从给定约束（预算按各镜 duration_s 合计）。
8. motion 是多行时间线（用 \\n 分行），形如 "[0s-2s] ...\\n[2s-5s] ..."，覆盖整镜时长。
9. 每镜恰好一条 subtitle（中文台词/旁白；无对白镜用旁白或环境独白）。""" % (
        boundary, JSON_SPEC, "/".join(moves), "/".join(intensities))


def breakdown_user(script_text: str, n_required: int, budget_s: float) -> str:
    head = ["请把下面的台本拆解为 JSON。"]
    if n_required:
        head.append("镜数要求：恰好 %d 镜（S1…S%d，与台本分镜表一致）。" % (n_required, n_required))
    if budget_s:
        head.append("总时长预算：%.2fs（各镜 duration_s 合计不得超出，允许 ±0.71s 容差）。" % budget_s)
    return "\n".join(head) + "\n\n--- 台本开始 ---\n" + script_text.strip() + "\n--- 台本结束 ---"


def retry_feedback(problems: list[str], last_raw: str | None, kind: str) -> str:
    """有界退回的反馈消息：错误清单 + 上一轮输出节选。kind ∈ {write, breakdown}。"""
    lines = ["上一轮输出未通过%s校验，逐条修复后重新输出完整结果（不要只输出 diff）。"
             % ("结构" if kind == "write" else "schema")]
    lines.append("")
    for i, p in enumerate(problems, 1):
        lines.append("%d. %s" % (i, p))
    if last_raw:
        excerpt = last_raw[:1500]
        lines.append("")
        lines.append("--- 上一轮输出节选（供对照，勿原样重发）---")
        lines.append(excerpt + ("…" if len(last_raw) > 1500 else ""))
    return "\n".join(lines)
