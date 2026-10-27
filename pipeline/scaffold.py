# -*- coding: utf-8 -*-
"""new-project 脚手架（docs §6）。

生成 projects/<名>/：project.yaml（按通道预填 style_anchors）、剧本模板.md、
characters.yaml、shots.yaml、subtitles.yaml、五个产物目录 + 空 manifest。
生成后立即跑 schema 校验（必填/类型/引用完整性），结果直接打印。

模板语法严格落在 miniyaml 支持集内（§2.3）——selftest 会把模板文件本身过一遍
miniyaml 并断言解析成功，规范与实现不允许漂移。
"""
from __future__ import annotations

from pathlib import Path

from . import manifest as manifest_mod
from . import schema

__all__ = ["new_project", "PROJECT_FILES", "STAGE_DIRS"]

STAGE_DIRS = ("assets", "keyframes", "camera", "video", "final")
PROJECT_FILES = ("project.yaml", "剧本模板.md", "characters.yaml", "shots.yaml", "subtitles.yaml")

_STYLE_ANCHORS = {
    "photo": "cinematic anime film still, Makoto Shinkai style rain, cel-shaded with soft moody lighting, wet asphalt reflections, muted dusk palette with warm neon accents, film grain, 16:9 widescreen composition",
    "anime": "masterpiece anime illustration, cinematic rain atmosphere, cel shading, film grain",
}
_NEG_ANCHORS = {
    "photo": "photorealistic, 3d render, western cartoon, text, watermark, logo, signature, extra fingers, deformed hands, extra limbs, bad anatomy, blurry, lowres, cropped",
    "anime": "photo, 3d render, text, watermark, bad anatomy, extra fingers, blurry, lowres",
}


def _project_yaml(name: str, style: str) -> str:
    return """\
# 项目配置（docs/FRAMEWORK_DESIGN.md §2.2）。overrides 节按路径深合并进全局配置：
# 映射递归、标量覆盖、列表整体替换；本文件其余字段是项目元数据，不进全局合并。
name: {name}
style: {style}                    # 项目默认通道（anime|photo），单镜可在 shots.yaml 覆盖
seed_base: 20261000               # §3 规则 4：全段 seed 派生基准（迁移老项目时逐镜写显式 seed）
style_anchors:                    # 画风锚点按通道各备一套，拼进所有图像提示词
  photo: "{photo_anchor}"
  anime: "{anime_anchor}"
negative_anchors:                 # 负面锚点同理
  photo: "{photo_neg}"
  anime: "{anime_neg}"
overrides:
  video: {{default_duration_s: 5.17}}
""".format(name=name, style=style,
           photo_anchor=_STYLE_ANCHORS["photo"], anime_anchor=_STYLE_ANCHORS["anime"],
           photo_neg=_NEG_ANCHORS["photo"], anime_neg=_NEG_ANCHORS["anime"])


_SCRIPT_MD = """\
# 《__NAME__》 — R18 动态漫剧 分镜台本（模板）

> 本文件是"人/LLM 写 shots.yaml"的中间层：框架不解析它（保持自由文体），
> 结构化数据只认 characters.yaml / shots.yaml / subtitles.yaml。
> 每节按"填写说明 → 示例"组织；写完分镜后把每镜字段誊进 shots.yaml。

## 1. 元信息（填写说明：类型/时长/画幅/引擎/尺度决策）

- 类型: （示例）都市雨夜 / 心理向 R18（氛围大于露骨）/ 动态漫画（漫剧）
- 正片: __N__ 镜 × 约 5–15s ≈ __TOTAL__s，16:9（1344×768），24fps
- 引擎: 关键帧 = 双通道（photo=Sakura-Image-2.1-Turbo / anime=waiIllustrious SDXL）；
  动态 = 本地 MiniMax H3（首尾帧图生视频）；运镜 = 模式B 词库直通 / 模式A Blender
- 尺度决策: （示例）敏感过程以 剪影/阴影/局部特写/反应镜头 呈现——尺度决策留在剧本层，
  不进框架代码（docs §10.9）

## 2. 人物设定（全片一致性锚点；逐字段誊进 characters.yaml）

**__CHAR1_NAME__（__CHAR1_AGE__）** — 发型/瞳色/体态一句话。
服装: （着装态是硬约定——一致性锚点以着装态为准）。
道具: （列进 characters.yaml 的 props 与 props_catalog）。
English anchor: `（一句话英文锚点：a ...-year-old ..., hair, eyes, outfit, props）`

**__CHAR2_NAME__** — （同上；无脸/剪影角色写明"face never visible"之类的负向约束）

**世界** — 季节/时段/场景链条（例：空荡站台 → 后巷 → 老楼门洞），贯穿天气。

**画风锚点**: `（誊自 project.yaml style_anchors，双通道各一句）`

## 3. 三幕结构（填写说明：每幕一句话 + 覆盖镜号）

- 第一幕 __ACT1__（S1–S__）: 
- 第二幕 __ACT2__（S__–S__）: 
- 第三幕 __ACT3__（S__–S__）: 

## 4. 分镜表（每镜五要素；誊进 shots.yaml 的一镜一块）

> 每镜写：首帧（first，英文镜头描述）→ 尾帧（last，相对变化）+ H3 动效（motion，
> [0s-2s]…时间线）+ 运镜（camera: move × intensity，词库见 pipeline/camera/vocab.yaml）
> + 声音（audio_hint）+ 字幕（誊进 subtitles.yaml 的 cues）。

### S1 __SHOT1_TITLE__（约 5.17s）
- 首帧: 
- 尾帧: 
- H3: 
- 运镜: push_in × slow
- Audio: 
- 字幕: 

## 5. 技术流水线（自动段，无需填写；排障时对照）

assets（角色三视图/表情/道具，人工挑选三视图）→ keyframes（每镜首/末帧，
末帧默认保身份 edit）→ camera（模式B 词库 / 模式A Blender 运镜）→
video（H3 首尾帧图生视频，时长须落在 5.0–15.0s 的 17k+5 帧网格）→
final（补轨→concat→字幕烧录→contact sheet）。
断点续跑：完成即跳过；改输入/上游/配置自动判 stale 重跑（docs §5）。
"""


def _characters_yaml() -> str:
    return """\
# 角色一致性锚点 + 资产需求清单（schema 见 docs/FRAMEWORK_DESIGN.md §6）。
# id 用 ASCII（文件名安全），name 中文；anchor_en 是全链拼接的英文一致性锚点。
characters:
  - id: xiaoyu                # 被 shots.yaml 的 characters: [id] 引用
    name: 小羽
    anchor_en: "a 14-year-old Japanese schoolgirl, shoulder-length straight black hair with blunt bangs, amber eyes, youthful slender figure"
    outfit_en: "navy sailor school uniform blouse with white triple-striped collar and red scarf, light grey pleated skirt, black over-knee socks, white sneakers"
    negative_extra: "mature woman, adult body type, old person"
    assets:                    # 资产需求清单：驱动 assets 段逐项出图
      three_view: true         # 全剧身份锚（三视图，标准全身着装，渲染多候选供人工挑选）
      expressions: [worried, tearful]   # 表情差分（可空 []）
      props: [vinyl_umbrella, school_bag]  # 道具差分（可空 []；名须在 props_catalog）
  - id: shadow_man
    name: 黑影男
    anchor_en: "a tall faceless man in a long black coat under a long black umbrella, always backlit silhouette, face never visible"
    outfit_en: "long black coat, black leather gloves"
    negative_extra: "visible face, frontal lighting on face"
    assets:
      three_view: false        # 剪影角色不需要三视图
      expressions: []
      props: [black_umbrella]
props_catalog:                 # 道具名 → 英文锚点（镜头出现道具时拼进提示词）
  vinyl_umbrella: "transparent vinyl umbrella, rain droplets on canopy"
  school_bag: "cream-colored shoulder school bag"
  black_umbrella: "long black umbrella"
"""


def _shots_yaml(project_name: str, style: str) -> str:
    return """\
# 分镜表（驱动 keyframes/camera/video/final）。取值优先级：单镜字段 > defaults > config 段默认。
# 所用语法全部落在 miniyaml 支持集内（docs §2.3）。追加镜：复制 S1 块改 id（S2、S3…）。
project: {project}
style: {style}                  # 项目级回退通道（单镜 style 可覆盖）
defaults: {{duration_s: 5.17, camera: {{mode: B, move: push_in, intensity: slow}}}}
shots:
  - id: S1                      # 纯镜号，排序即成片顺序
    title: 示例镜·雨夜站台
    duration_s: 5.17            # 5–15s，映射到 17k+5 帧网格（网格待首跑标定，§4.4）
    characters: [xiaoyu]        # 引 characters.yaml 的 id（可多角色）
    style: {style}              # 可选：单镜覆盖通道
    scene: "inside a rain-soaked bus stop shelter at night, cinematic rain atmosphere"  # H3 提示词首段；缺省=first+画风锚点
    first: "a schoolgirl sitting alone on the bench of a glass bus stop shelter at night, head slightly lowered, transparent vinyl umbrella dripping in her hand, school bag beside her, empty street and distant city light bokeh behind the glass"
    last: "she lifts her head, looking up at the rain-streaked bus timetable sign above the bench, faint worried frown, rain droplets running down the shelter glass"
    last_mode: edit             # edit（保身份，默认）| fresh（大机位变化）
    use_char_ref: false         # photo 通道可用三视图作参考图（B 级，待标定，默认关）
    camera: {{mode: B, move: push_in, intensity: slow, extra: ""}}
    motion: |                   # H3 Timeline 段（动作/时间线，必填）
      [0s-2s] rain streaks slanting under the shelter light, distant car headlights sweeping across the wet road
      [2s-5s] she slowly lifts her head toward the timetable sign, umbrella tilting slightly
    audio_hint: "steady rain, distant traffic"
    negative_extra: ""
    seed: 0                     # 可选：显式 seed（0=按 §3 规则 4 派生）
""".format(project=project_name, style=style)


def _subtitles_yaml(project_name: str) -> str:
    return """\
# 字幕/标题卡 cues（final 段消费）。t_in/t_out 为镜内秒；final 会按各镜时长算全局偏移。
project: {project}
title_card: {{text: "项 目 名", t_in: 0.9, t_out: 3.0}}
end_card: {{text: "—— 完 ——", tail_s: 4.0}}
cues:
  - {{shot: S1, t_in: 0.5, t_out: 2.5, text: "示例字幕：替换为台本台词"}}
""".format(project=project_name)


def new_project(name: str, style: str = "photo", projects_root=None,
                force: bool = False, log=print) -> Path:
    """生成项目骨架并当场校验。返回项目目录。"""
    if style not in ("photo", "anime"):
        raise ValueError("--style 须为 photo|anime，得到 %r" % style)
    root = Path(projects_root)
    pdir = root / name
    if pdir.exists():
        if not force:
            raise FileExistsError("项目已存在: %s（--force 覆盖重写配置文件）" % pdir)
        log("项目已存在，--force 覆盖配置文件: %s" % pdir)
    pdir.mkdir(parents=True, exist_ok=True)
    files = {
        "project.yaml": _project_yaml(name, style),
        "剧本模板.md": _SCRIPT_MD.replace("__NAME__", name),
        "characters.yaml": _characters_yaml(),
        "shots.yaml": _shots_yaml(name, style),
        "subtitles.yaml": _subtitles_yaml(name),
    }
    for fname, text in files.items():
        (pdir / fname).write_text(text, encoding="utf-8", newline="\n")
    for stage in STAGE_DIRS:
        sd = pdir / stage
        sd.mkdir(exist_ok=True)
        manifest_mod.save(sd / ("%s_manifest.json" % stage),
                          manifest_mod._skeleton(stage, name))
    log("已生成项目骨架: %s" % pdir)
    for fname in PROJECT_FILES:
        log("  - %s" % fname)
    # 生成后立即 schema 校验（docs §6：校验结果直接打印）
    try:
        data = schema.load_project_data(pdir)
    except schema.SchemaError as e:
        log("schema 校验失败（模板与实现漂移，属框架 bug）：%s" % e)
        raise
    problems = schema.validate_all(data)
    if problems:
        raise schema.SchemaError(problems)
    log("schema 校验通过：%d 角色 / %d 镜 / defaults 与引用完整"
        % (len(data["characters"]["characters"]), len(data["shots"]["shots"])))
    return pdir
