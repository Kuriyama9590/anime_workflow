# -*- coding: utf-8 -*-
"""assets 段（段1）：按 characters.yaml 为每角色产出设定资产（人物素材资产模块）。

设计依据（docs/FRAMEWORK_DESIGN.md）：
- §4.1 段契约：每角色 × {三视图, 每个表情差分, 每个道具} 生成 t2i，1024×1024（§3 规则 3
  的唯一例外；方图为 assets 段首跑标定点）；三视图渲 ``assets.candidates`` 个候选供人工挑，
  pick 后定名，未 pick 时 keyframes 段对该角色拒绝开跑（宁可停，不糊）。
- §3 角则 4 seed（唯一实现 pipeline/seeds.py，角色序/资产序/候选序均**从 1 起**）：
  ``seed_base + 1_000_000 + 1000×角色序 + 10×资产序``；三视图第 i 候选再 ``+ (i-1)``。
  **资产序枚举顺序（本模块定义，写死不变）**：threeview=1，其后 expressions 按列出顺序、
  props 按列出顺序递增；characters.yaml 角色级 ``seed:`` 显式值 = 该角色的派生基整体替换
  （以该值为基 +10×(资产序-1)+候选偏移 同式展开；characters.yaml 无 per-asset seed 粒度）。
- §3 规则 5 提示词次序：镜头描述(模板) + 角色锚点 + 道具锚点 + 画风锚点；负面同理。
- §3 规则 2 双通道：photo = Sakura-Image-2.1-Turbo（GGUF + SakuraQwenImage21Tools 节点，
  图=pipeline/workflows/photo_sakura.build_t2i，A 级 img_wf 的参数化移植）；
  anime = waiIllustriousSDXL_v170（SDXL t2i，C 级待标定，参数来自 config channels.anime）。
  stages 层不感知具体节点图，一律经 pipeline.workflows.build_t2i(channel, …)。
- §4.0 前置：服务器可达预检（不可达 → exit 2）；孤儿 ``running`` unit 收敛（history 命中→
  收割下载；queue 在列→等待收割绝不重提；都查无→置回 pending）。
- §5 manifest：pipeline/manifest.py 共享模块（原子写/三条件完成判定/三路失效判定）；
  unit 键 = ``<char_id>:threeview | <char_id>:expr:<名> | <char_id>:prop:<名>``。

╔══ 内容边界（硬约定，不可配置关闭）══════════════════════════════════════╗
║ 本模块产物是**角色一致性锚点资产**，一律为内容中性、全身着装的标准设定图：  ║
║   · 三视图   = 标准 full-body 站姿、standard outfit 全身着装、中性平背景   ║
║   · 表情差分 = 单人半身像、同脸同发、着装态                                ║
║   · 道具图   = 纯物件、素背景、无人物                                      ║
║ 边界落在三处：                                                              ║
║   1) 默认提示词模板 PROMPT_THREEVIEW / PROMPT_EXPR / PROMPT_PROP（含       ║
║      "fully clothed / modest / safe for work" 措辞，模板常量即文档）；      ║
║   2) BOUNDARY_NEGATIVE 负面词表**永远**追加在每条负面提示词末尾（不能通过   ║
║      characters.yaml / project.yaml / --set 关闭）；                        ║
║   3) 本模块文档（docstring + 设计文档 §4.1）明示该边界。                    ║
║ R18 尺度决策留在剧本层（设计 §10.9），不进入本模块；本模块不产出任何尺度   ║
║ 敏感素材——资产是全剧身份锚，必须可在任何上下文中安全复用。                ║
╚═══════════════════════════════════════════════════════════════════════════╝

产物（<项目>/assets/）：
  <char>_threeview_c1..cN.png   三视图候选（每候选独立 seed，§3 规则 4）
  <char>_threeview.png          pick 后定名（keyframes 段的身份锚，§4.1 人工关口）
  <char>_expr_<名>.png          表情差分
  <char>_prop_<名>.png          道具图
  <名>.png.meta.json            每张图伴生 meta：模型/种子/提示词/负面/采样参数（可复现）
  assets_manifest.json          段 manifest（§5 schema；keyframes.check_assets_gate 消费）

对外入口（stages 包约定，与 keyframes/video 同构）：
    run_stage(project_dir, config=None, args=None) -> int   # 供 pipeline.cli 调度
    main(argv=None) -> int     # python -m pipeline.stages.assets <项目> [flags]
退出码：0=全部成功或跳过；1=存在失败 unit 或输入错误；2=ComfyUI 不可达（§4.0.1）。

用法：
  python -m pipeline.stages.assets 我的新剧 --dry-run        # 计划预览（不连服务器、不写盘）
  python -m pipeline.stages.assets 我的新剧                  # 渲染全部缺失资产
  python -m pipeline.stages.assets 我的新剧 --only detective,detective:prop:briefcase
  python -m pipeline.stages.assets 我的新剧 --pick detective:c2   # 人工挑三视图候选
  python -m pipeline.stages.assets 我的新剧 --auto-pick      # 无人值守：全部取第 1 候选
  python -m pipeline.stages.assets --selftest                # 干跑自检（不碰 GPU/ComfyUI）
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pipeline import miniyaml
from pipeline import comfy as comfy_mod
from pipeline.workflows import build_t2i

try:  # 核心模块共享层（已落地；探测式导入保证并行构建期本模块可独立 import）
    from pipeline import config as config_mod
    from pipeline import manifest as manifest_mod
    from pipeline import seeds as seeds_mod
except ImportError:  # pragma: no cover - 共享层缺失时给出可读报错而非静默降级
    config_mod = None
    manifest_mod = None
    seeds_mod = None

STAGE = "assets"
NEEDS_COMFY = True  # §4.0：服务器预检仅对 needs_comfy 段执行

#: 仓库根 = pipeline 包父目录（设计 §2.1 paths 注：与 cwd 无关）
REPO_ROOT = Path(__file__).resolve().parents[2]

#: §5 完成判定：图 ≥100KB（段配置下限；config assets.min_bytes 可覆盖）
MIN_BYTES_DEFAULT = 100_000

#: 支持的通道（与 pipeline/workflows/__init__.py 一致）
CHANNELS = ("photo", "anime")

# --------------------------------------------------------------------------- #
# 内容边界（见模块 docstring 的边框说明——三处落地之一：提示词常量）
# --------------------------------------------------------------------------- #
#: 边界措辞：拼进三个默认提示词模板（设计 §4.1 模板 + 全身着装/中性/sfw 硬边界）
BOUNDARY_PHRASE = (
    "fully clothed in the standard outfit, modest neutral content, safe for work design sheet"
)

#: 边界负面：永远追加在负面提示词末尾，不可被 characters.yaml / project.yaml / --set 关闭
BOUNDARY_NEGATIVE = (
    "nude, nudity, naked, topless, bottomless, nsfw, explicit content, lingerie, swimsuit, "
    "see-through clothing, suggestive pose, sexual content, partially undressed, missing clothes"
)

#: 三视图模板 = 设计 §4.1 指定措辞 + 边界措辞 + 一致性补充
PROMPT_THREEVIEW = (
    "character reference sheet, front view / side view / back view, standing full body, "
    "standard outfit, neutral flat background, three views of the same one character with "
    "consistent design and proportions, " + BOUNDARY_PHRASE
)

#: 表情差分模板 = 设计 §4.1 指定措辞 + 边界措辞
PROMPT_EXPR = (
    "expression sheet, single portrait bust, {expr} expression, same face same hair, "
    + BOUNDARY_PHRASE
)

#: 道具模板 = 设计 §4.1 指定措辞 + 边界措辞 + 无人物
PROMPT_PROP = (
    "prop sheet, single object on plain background, neutral product reference, no people, "
    + BOUNDARY_PHRASE
)

#: id 约束：与 pipeline/schema.py 的 _ASCII_ID_RE 严格一致（ASCII 文件名安全，无连字符）
_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
#: 文件名安全化：去掉 Windows 路径敌对字符，空白折叠为下划线（保留中文）
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\r\n\t]+')

#: 默认配置 = docs §2.1 相关节镜像（pipeline.config 落地前回退 + 缺键补全基底）
DEFAULTS: Dict[str, Any] = {
    "comfy": {
        "server": "http://127.0.0.1:8188",
        "client_id": "pipeline",
        "models_dir": "E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable/ComfyUI/models",
        "poll_interval_s": 3,
        "timeout_img_s": 900,
    },
    "channels": {
        "photo": {
            "unet_gguf": "diffusion_models/Sakura-Image-2.1-Turbo-Q5_K_M-4.60GiB.gguf",
            "clip": "text_encoders/Sakura-TextEncoder-Qwen3VL-8B-Uncensored-bf16.safetensors",
            "vae": "vae/qwen_image_2.1_vae_bf16.safetensors",
            "clip_type": "qwen_image",
            "resolution": 1024,
            "width": 1344,
            "height": 768,
            "sampler": "euler",
            "sigmas": "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0",
            "cfg": 1.0,
        },
        "anime": {
            "checkpoint": "checkpoints/waiIllustriousSDXL_v170.safetensors",
            "width": 1344,
            "height": 768,
            "steps": 28,
            "cfg": 5.0,
            "sampler": "euler_ancestral",
            "img2img_denoise": 0.55,
            "negative": "photo, 3d, realistic, text, watermark, bad anatomy, extra fingers, lowres",
        },
    },
    "assets": {
        "width": 1024,
        "height": 1024,
        "candidates": 2,
        "min_bytes": MIN_BYTES_DEFAULT,
    },
    "run": {
        "attempts": 2,
        "retry_wait_s": 20,
        "quiet_first_s": 5,
        "server_precheck": True,
    },
    "paths": {"projects_root": "projects", "logs_dir": "logs"},
}


class AssetsError(RuntimeError):
    """assets 段输入/结构错误（可读消息，逐条列出）。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _safe_print(line: str) -> None:
    """Windows 控制台 UTF-8 安全输出。"""
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


class StageLog:
    """段日志：print + 追加写 logs/<项目>_assets.log（§5 运行日志，含完整提示词）。"""

    def __init__(self, path: Optional[Path], echo: bool = True):
        self.path = Path(path) if path else None
        self.echo = echo

    def __call__(self, msg: str) -> None:
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        if self.echo:
            _safe_print(line)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # 日志失败不阻断渲染


def _noop_log(msg: str) -> None:  # dry-run / 测试注入
    _safe_print(msg)


def safe_name(raw: str) -> str:
    """表情/道具名 → 文件名安全形式（保留中文；空白与敌对字符折叠为 _）。"""
    t = _UNSAFE_RE.sub("_", str(raw).strip())
    t = re.sub(r"\s+", "_", t).strip("_")
    return t


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """§2.2 合并规则：映射递归、标量覆盖、列表整体替换（config.py 缺失时的回退实现）。"""
    if config_mod is not None:
        return config_mod.deep_merge(base, over)
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_path(value, base: Path = REPO_ROOT) -> Path:
    if config_mod is not None:
        return config_mod.resolve_path(value)
    q = Path(str(value))
    if not q.is_absolute():
        q = base / q
    return Path(os.path.normpath(str(q)))


# --------------------------------------------------------------------------- #
# 配置加载（config 参数 > pipeline.config 组合原语 > 内置回退）
# --------------------------------------------------------------------------- #
def _load_stage_config(project_spec=None, sets=None,
                       config: Optional[Dict[str, Any]] = None
                       ) -> Tuple[Dict[str, Any], Dict[str, Any], Path]:
    """加载配置并解析项目目录（**私有**：刻意不叫 load_stage_config——那是
    stages/__init__.py invoke_stage 识别 video 段"自带装配"约定的魔法名，
    本段走标准分派 run_stage(project_dir, config=merged_cfg, args=StageOpts)，
    命名撞车会导致 auto_pick/sets 被丢（复审实测）。返回 (cfg, project_doc, project_dir)。

    优先级（§2.2）：全局 config.yaml < project.yaml overrides < --set；
    调用方（pipeline.cli / 自检）直接传入已合并 dict 时原样采用（仅补默认键），
    project.yaml 仍从解析出的项目目录读取（元数据与 overrides 均取自它）。
    """
    spec_dir = Path(str(project_spec)) if project_spec is not None else None
    if spec_dir is not None and not spec_dir.is_dir():
        spec_dir = None  # 是项目名，走 projects_root 解析

    def _read_project_doc(pdir: Path) -> Dict[str, Any]:
        pp = pdir / "project.yaml"
        if not pp.is_file():
            raise AssetsError("未找到项目配置 %s（先 `python -m pipeline new <名>` 脚手架）" % pp)
        doc = miniyaml.load(pp)
        if not isinstance(doc, dict):
            raise AssetsError("project.yaml 顶层必须是映射: %s" % pp)
        return doc

    if isinstance(config, dict) and config:
        cfg = _deep_merge(DEFAULTS, copy.deepcopy(config))
        project_dir = spec_dir or _resolve_project_dir(str(project_spec), cfg)
        return cfg, _read_project_doc(project_dir), project_dir

    if config_mod is not None:
        cfg = config_mod.load_global()
        project_dir = spec_dir or _resolve_project_dir(str(project_spec), cfg)
        project_doc = _read_project_doc(project_dir)
        ov = project_doc.get("overrides")
        if ov is not None:
            if not isinstance(ov, dict):
                raise AssetsError("project.yaml 的 overrides 必须是映射")
            cfg = config_mod.deep_merge(cfg, ov)
        for item in sets or []:
            cfg = config_mod.deep_merge(cfg, config_mod.parse_set(item))
        return _deep_merge(DEFAULTS, cfg), project_doc, project_dir

    # 回退：miniyaml 直读（共享 config 模块缺席的并行构建期）
    gp = REPO_ROOT / "pipeline" / "config.yaml"
    if not gp.is_file():
        raise AssetsError("全局配置缺失: %s（pipeline.config 未落地且该文件不存在）" % gp)
    cfg = miniyaml.load(gp)
    project_dir = spec_dir or _resolve_project_dir(str(project_spec), cfg)
    project_doc = _read_project_doc(project_dir)
    if isinstance(project_doc.get("overrides"), dict):
        cfg = _deep_merge(cfg, project_doc["overrides"])
    for item in sets or []:
        if "=" not in item:
            raise AssetsError("--set 需要 key=value 形式，得到 %r" % item)
        key, _, val = item.partition("=")
        node: Dict[str, Any] = {}
        cur = node
        parts = [p.strip() for p in key.split(".") if p.strip()]
        for p in parts[:-1]:
            cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = miniyaml.parse(val) if val.strip().startswith(("[", "{", '"', "'")) else _guess_scalar(val)
        cfg = _deep_merge(cfg, node)
    return _deep_merge(DEFAULTS, cfg), project_doc, project_dir


def _guess_scalar(raw: str):
    s = raw.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


# --------------------------------------------------------------------------- #
# characters.yaml 解析与全量校验（§6 schema；错误一次性全部列出）
# --------------------------------------------------------------------------- #
def validate_characters(doc: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, str], List[str]]:
    """规范化 + 校验。返回 (chars, props_catalog, errors)。

    chars 条目：id/name/anchor_en/outfit_en/negative_extra/seed/three_view/
    expressions(原始名列表)/props(原始名列表)/expr_files/prop_files(文件名安全形式)。
    """
    errors: List[str] = []
    if not isinstance(doc, dict):
        return [], [], ["characters.yaml 顶层必须是映射（characters + props_catalog）"]
    raw_chars = doc.get("characters")
    if not isinstance(raw_chars, list) or not raw_chars:
        return [], [], ["characters.yaml 缺少非空 characters 列表（设计 §6 模板）"]
    catalog_raw = doc.get("props_catalog") or {}
    if not isinstance(catalog_raw, dict):
        errors.append("props_catalog 必须是映射（道具名 → 英文锚点）")
        catalog_raw = {}
    catalog: Dict[str, str] = {}
    for k, v in catalog_raw.items():
        if not isinstance(v, str) or not v.strip():
            errors.append("props_catalog[%r] 的锚点必须是非空字符串" % k)
        else:
            catalog[str(k)] = v.strip()

    chars: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for i, c in enumerate(raw_chars, 1):
        where = "characters[%d]" % i
        if not isinstance(c, dict):
            errors.append("%s 必须是映射" % where)
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not _ID_RE.match(cid):
            errors.append("%s.id 必须是 ASCII 文件名安全 id（正则 %s），得到 %r"
                          % (where, _ID_RE.pattern, cid))
            continue
        if cid in seen_ids:
            errors.append("%s.id=%r 重复" % (where, cid))
            continue
        seen_ids.add(cid)
        if not isinstance(c.get("anchor_en"), str) or not c["anchor_en"].strip():
            errors.append("%s(%s).anchor_en 必须是非空英文锚点（身份一致性核心）" % (where, cid))
        if not isinstance(c.get("name"), str) or not c.get("name", "").strip():
            errors.append("%s(%s).name 缺失（中文名，仅展示；schema.py 同款要求）" % (where, cid))
        assets = c.get("assets") or {}
        if not isinstance(assets, dict):
            errors.append("%s(%s).assets 必须是映射 {three_view, expressions, props}" % (where, cid))
            assets = {}
        three_view = assets.get("three_view", True)
        if not isinstance(three_view, bool):
            errors.append("%s(%s).assets.three_view 应为布尔，得到 %r" % (where, cid, three_view))
            three_view = bool(three_view)
        exprs_raw = assets.get("expressions") or []
        props_raw = assets.get("props") or []
        if not isinstance(exprs_raw, list):
            errors.append("%s(%s).assets.expressions 必须是字符串列表（英文表情词进入提示词）" % (where, cid))
            exprs_raw = []
        elif not all(isinstance(e, str) for e in exprs_raw):
            errors.append("%s(%s).assets.expressions 必须是字符串列表（英文表情词进入提示词）" % (where, cid))
            exprs_raw = [e for e in exprs_raw if isinstance(e, str)]
        if not isinstance(props_raw, list):
            errors.append("%s(%s).assets.props 必须是字符串列表" % (where, cid))
            props_raw = []
        elif not all(isinstance(p, str) for p in props_raw):
            errors.append("%s(%s).assets.props 必须是字符串列表" % (where, cid))
            props_raw = [p for p in props_raw if isinstance(p, str)]
        expr_files, seen_file = [], set()
        for e in exprs_raw:
            fe = safe_name(e)
            if not fe:
                errors.append("%s(%s) 表情名 %r 安全化后为空" % (where, cid, e))
                continue
            if fe in seen_file:
                errors.append("%s(%s) 表情名安全化后冲突: %r → %r" % (where, cid, e, fe))
                continue
            seen_file.add(fe)
            expr_files.append(fe)
        prop_files = []
        for p in props_raw:
            fp = safe_name(p)
            if not fp:
                errors.append("%s(%s) 道具名 %r 安全化后为空" % (where, cid, p))
                continue
            if fp in seen_file:
                errors.append("%s(%s) 道具名安全化后冲突: %r → %r" % (where, cid, p, fp))
                continue
            seen_file.add(fp)
            prop_files.append(fp)
            if p not in catalog:
                errors.append("%s(%s) 引用的道具 %r 不在 props_catalog（引用完整性，§6）" % (where, cid, p))
        seed = c.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            # bool 是 int 子类，须显式排除（schema.py 对 shots.seed 同款标准）
            errors.append("%s(%s).seed 必须是整数（角色级显式 seed），得到 %r" % (where, cid, seed))
            seed = None
        outfit = c.get("outfit_en")
        if outfit is not None and not isinstance(outfit, str):
            errors.append("%s(%s).outfit_en 必须是字符串（标准着装锚点）" % (where, cid))
            outfit = ""
        neg = c.get("negative_extra")
        if neg is not None and not isinstance(neg, str):
            errors.append("%s(%s).negative_extra 必须是字符串" % (where, cid))
            neg = ""
        # seed 派生空间：资产序（1 起）上限 99（10×99=990 < 1000×角色序块宽，§3 规则 4）
        n_assets = (1 if three_view else 0) + len(expr_files) + len(prop_files)
        if n_assets > 99:
            errors.append("%s(%s) 资产数 %d 超过 seed 派生空间上限 99（10×资产序 与 1000×角色序 块宽冲突）"
                          % (where, cid, n_assets))
        if n_assets == 0:
            errors.append("%s(%s) 未声明任何资产（three_view=false 且表达式/道具为空）" % (where, cid))
        chars.append({
            "id": cid,
            "name": str(c.get("name") or cid),
            "anchor_en": str(c.get("anchor_en") or "").strip(),
            "outfit_en": str(outfit or "").strip(),
            "negative_extra": str(neg or "").strip(),
            "seed": seed,
            "three_view": three_view,
            "expressions": [str(e) for e in exprs_raw],
            "expr_files": expr_files,
            "props": [str(p) for p in props_raw],
            "prop_files": prop_files,
        })
    return chars, catalog, errors


# --------------------------------------------------------------------------- #
# seed 派生（§3 规则 4 —— 唯一实现：pipeline/seeds.py；角色序/资产序/候选序均从 1 起）
# --------------------------------------------------------------------------- #
def unit_seed(seed_base: int, char_index: int, asset_index: int, candidate: int = 1,
              explicit: Optional[int] = None) -> int:
    """单个 seed：优先走 pipeline.seeds.asset_seed（全段统一）；显式 seed 为**角色级基**，
    以该值替代 seed_base+1_000_000+1000×角色序 整段基后同式派生（+10×(资产序-1)+候选偏移）。

    characters.yaml 只有角色级 seed 粒度（设计 §6 模板无 per-asset seed 字段），
    角色级显式基的语义为本模块文档化定义：基 = 显式值，资产序仍按 §3 间隔展开。
    """
    if explicit is not None:
        return int(explicit) + 10 * (int(asset_index) - 1) + (int(candidate) - 1)
    if seeds_mod is not None:
        return seeds_mod.asset_seed(seed_base, char_index, asset_index, candidate)
    # 回退（seeds.py 缺席的并行构建期）：逐字复刻其公式
    return int(seed_base) + 1_000_000 + 1000 * int(char_index) + 10 * int(asset_index) + (int(candidate) - 1)


# --------------------------------------------------------------------------- #
# unit 计划
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Unit:
    key: str                 # <cid>:threeview | <cid>:expr:<名> | <cid>:prop:<名>
    char_id: str
    kind: str                # threeview | expr | prop
    name_orig: Optional[str] # 表情/道具原始名（None=threeview）
    name_file: Optional[str] # 文件名安全形式
    seeds: List[int]         # 候选数 >1 仅 threeview
    dests: List[str]         # 待渲染 png 文件名（候选 c1..cN / 单图）
    final: Optional[str]     # threeview pick 后定名（其余 None）
    char_index: int
    asset_index: int
    char: Dict[str, Any]     # 角色记录（提示词拼装用）

    @property
    def is_threeview(self) -> bool:
        return self.kind == "threeview"


def plan_units(chars: List[Dict[str, Any]], seed_base: int, candidates: int) -> List[Unit]:
    """按 §4.1 对每角色 × {三视图, 表情差分, 道具} 建 unit。

    角色序/资产序均从 1 起（与 pipeline/seeds.py 一致）；
    资产序枚举顺序：threeview=1，其后 expressions 按列出顺序、props 按列出顺序递增。
    """
    units: List[Unit] = []
    for ci, ch in enumerate(chars, 1):
        explicit = ch.get("seed")
        cid = ch["id"]
        ai = 0

        def _seed(asset_index_1: int, candidate: int = 1) -> int:
            return unit_seed(seed_base, ci, asset_index_1, candidate, explicit)

        if ch["three_view"]:
            ai = 1
            seeds = [_seed(ai, k) for k in range(1, candidates + 1)]
            dests = ["%s_threeview_c%d.png" % (cid, k) for k in range(1, candidates + 1)]
            units.append(Unit("%s:threeview" % cid, cid, "threeview", None, None,
                              seeds, dests, "%s_threeview.png" % cid, ci, ai, ch))
        for e_orig, e_file in zip(ch["expressions"], ch["expr_files"]):
            ai += 1
            units.append(Unit("%s:expr:%s" % (cid, e_file), cid, "expr", e_orig, e_file,
                              [_seed(ai)], ["%s_expr_%s.png" % (cid, e_file)],
                              None, ci, ai, ch))
        for p_orig, p_file in zip(ch["props"], ch["prop_files"]):
            ai += 1
            units.append(Unit("%s:prop:%s" % (cid, p_file), cid, "prop", p_orig, p_file,
                              [_seed(ai)], ["%s_prop_%s.png" % (cid, p_file)],
                              None, ci, ai, ch))
    return units


# --------------------------------------------------------------------------- #
# 提示词拼装（§3 规则 5 次序固定；边界措辞/负面见常量）
# --------------------------------------------------------------------------- #
def compose_prompt(u: Unit, style_anchor: str, prop_anchor: str) -> str:
    ch = u.char
    if u.kind == "threeview":
        parts = [PROMPT_THREEVIEW, ch["anchor_en"], ch["outfit_en"], style_anchor]
    elif u.kind == "expr":
        parts = [PROMPT_EXPR.format(expr=u.name_orig), ch["anchor_en"], ch["outfit_en"], style_anchor]
    else:
        parts = [PROMPT_PROP, prop_anchor, style_anchor]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def compose_negative(style: str, cfg: Dict[str, Any], project_neg: str,
                     char_neg: str) -> str:
    """负面 = 通道基础负面 + 项目负面锚点 + 角色附加负面 + 边界负面（永远在末尾）。"""
    parts: List[str] = []
    base = (cfg.get("channels") or {}).get(style, {}).get("negative")
    if base:
        parts.append(str(base))
    if project_neg:
        parts.append(str(project_neg))
    if char_neg:
        parts.append(str(char_neg))
    parts.append(BOUNDARY_NEGATIVE)
    seen: set = set()
    out: List[str] = []
    for p in parts:
        for tok in (t.strip() for t in p.split(",")):
            if tok and tok.lower() not in seen:
                seen.add(tok.lower())
                out.append(tok)
    return ", ".join(out)


# --------------------------------------------------------------------------- #
# meta 伴生文件（每张图：模型/种子/提示词/采样参数，可复现）
# --------------------------------------------------------------------------- #
def _model_info(style: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    ch = (cfg.get("channels") or {}).get(style, {})
    if style == "anime":
        return {"checkpoint": str(ch.get("checkpoint", ""))}
    return {"unet_gguf": str(ch.get("unet_gguf", "")),
            "clip": str(ch.get("clip", "")),
            "vae": str(ch.get("vae", "")),
            "clip_type": str(ch.get("clip_type", ""))}


def _sampler_info(style: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    ch = (cfg.get("channels") or {}).get(style, {})
    if style == "anime":
        return {"steps": ch.get("steps"), "cfg": ch.get("cfg"),
                "sampler": ch.get("sampler"), "scheduler": ch.get("scheduler", "normal"),
                "denoise": 1.0}
    return {"sampler": ch.get("sampler"), "sigmas": str(ch.get("sigmas", "")),
            "cfg": ch.get("cfg"), "resolution": ch.get("resolution"),
            "sigmas_steps": str(ch.get("sigmas", "")).count(",") + 1}


def write_meta(png_path: Path, *, unit_key: str, kind: str, char_id: str, channel: str,
               seed: int, prompt: str, negative: str, width: int, height: int,
               cfg: Dict[str, Any], prompt_id: Optional[str], candidate: Optional[int] = None,
               picked: bool = False, picked_from: Optional[str] = None) -> Path:
    meta = {
        "schema": 1,
        "module": "pipeline.stages.assets",
        "unit": unit_key,
        "kind": kind,
        "char_id": char_id,
        "channel": channel,
        "candidate": candidate,
        "picked": picked,
        "picked_from": picked_from,
        "model": _model_info(channel, cfg),
        "sampler": _sampler_info(channel, cfg),
        "seed": int(seed),
        "width": int(width),
        "height": int(height),
        "prompt": prompt,
        "negative": negative,
        "prompt_id": prompt_id,
        "rendered_at": _now(),
    }
    p = Path(str(png_path) + ".meta.json")
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)
    return p


# --------------------------------------------------------------------------- #
# 选项
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class StageOptions:
    only: List[str] = dataclasses.field(default_factory=list)
    force: bool = False
    dry_run: bool = False
    auto_pick: bool = False
    pick: Optional[str] = None          # "<char_id>:cN"
    keep_stale: bool = False
    abandon_running: bool = False
    sets: List[str] = dataclasses.field(default_factory=list)


def _coerce_args(args) -> StageOptions:
    if args is None:
        return StageOptions()
    if isinstance(args, StageOptions):
        return args
    if isinstance(args, dict):
        return StageOptions(
            only=[s for s in str(args.get("only") or "").split(",") if s.strip()],
            force=bool(args.get("force")),
            dry_run=bool(args.get("dry_run")),
            auto_pick=bool(args.get("auto_pick")),
            pick=args.get("pick") or None,
            keep_stale=bool(args.get("keep_stale")),
            abandon_running=bool(args.get("abandon_running")),
            sets=list(args.get("sets") or []))
    only = getattr(args, "only", "") or ""
    if isinstance(only, str):
        only = [s for s in only.split(",") if s.strip()]
    sets = getattr(args, "sets", None) or []
    if isinstance(sets, str):
        sets = [sets]
    return StageOptions(
        only=list(only), force=bool(getattr(args, "force", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        auto_pick=bool(getattr(args, "auto_pick", False)),
        pick=getattr(args, "pick", None), keep_stale=bool(getattr(args, "keep_stale", False)),
        abandon_running=bool(getattr(args, "abandon_running", False)),
        sets=list(sets))


@dataclasses.dataclass
class RunContext:
    project_dir: Path
    project_name: str
    assets_dir: Path
    cfg: Dict[str, Any]
    project_doc: Dict[str, Any]
    opts: StageOptions
    log: Callable[[str], None]
    style: str = "photo"
    seed_base: int = 0
    style_anchor: str = ""
    negative_anchor: str = ""
    candidates: int = 2
    width: int = 1024
    height: int = 1024
    min_bytes: int = MIN_BYTES_DEFAULT
    attempts: int = 2
    retry_wait_s: float = 20.0
    quiet_first_s: float = 5.0
    timeout_s: float = 900.0
    precheck: bool = True
    stage_cfg_hash: str = ""
    chars: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    catalog: Dict[str, str] = dataclasses.field(default_factory=dict)
    client: Any = None                  # ComfyClient / 测试注入的 Fake
    mf_path: Optional[Path] = None
    force: bool = False


# --------------------------------------------------------------------------- #
# manifest 封装（pipeline.manifest 共享模块；缺席时内嵌最小实现，schema 一致）
# --------------------------------------------------------------------------- #
def _mf_load(path: Path) -> Dict[str, Any]:
    if manifest_mod is not None:
        try:
            return manifest_mod.load(path)
        except manifest_mod.ManifestError as e:
            # §5：损坏 → 可读错误（统一 CLI 只捕 ValueError，AssetsError 必须在段内消化）
            raise AssetsError("assets manifest 损坏/不合法：%s（%s）——请人工检查或删除重建" % (path, e)) from e
    if not path.is_file():
        return {"stage": STAGE, "project": "", "schema": 1, "config_hash": "",
                "updated_at": _now(), "units": {}}
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise AssetsError("assets manifest 损坏: %s（%s）——请人工检查或删除重建" % (path, e))
    if not isinstance(m, dict) or not isinstance(m.get("units"), dict):
        raise AssetsError("assets manifest 结构不合法: %s" % path)
    return m


def _mf_save(path: Path, m: Dict[str, Any]) -> None:
    if manifest_mod is not None:
        manifest_mod.save(path, m)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    m = dict(m)
    m["updated_at"] = _now()
    tmp = path.with_name("%s.tmp-%d" % (path.name, os.getpid()))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _mf_unit(m: Dict[str, Any], key: str) -> Dict[str, Any]:
    if manifest_mod is not None:
        return manifest_mod.unit(m, key)
    u = m["units"].get(key)
    if u is None:
        u = {"status": "pending", "files": {}, "prompt_id": None, "seed": None,
             "attempts": 0, "error": None, "updated_at": _now()}
        m["units"][key] = u
    return u


def _mf_mark(m: Dict[str, Any], key: str, **fields) -> Dict[str, Any]:
    if manifest_mod is not None:
        return manifest_mod.mark_unit(m, key, **fields)
    u = _mf_unit(m, key)
    u.update(fields)
    u["updated_at"] = _now()
    return u


def _hash_fields(d) -> str:
    if manifest_mod is not None:
        return manifest_mod.hash_fields(d)
    blob = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    import hashlib
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _stage_cfg_hash(cfg: Dict[str, Any]) -> str:
    """§5 段配置哈希 = 该段相关配置节（config.STAGE_SECTIONS["assets"]=channels+assets）。"""
    if config_mod is not None:
        try:
            return config_mod.stage_sections_hash(cfg, STAGE)
        except Exception:
            pass
    sub = {"channels": cfg.get("channels"), "assets": cfg.get("assets")}
    return _hash_fields(sub)


# --------------------------------------------------------------------------- #
# 完成判定 / 失效判定
# --------------------------------------------------------------------------- #


def unit_complete(rec: Dict[str, Any], u: Unit, ctx: RunContext) -> bool:
    """三条件完成判定（§5）：done ∧ 文件存在 ∧ ≥assets.min_bytes。

    注意：pick 状态不参与渲染完成判定（候选齐=渲染完成）；下游门禁（keyframes
    check_assets_gate）另查 picked 标志与 final 文件。
    """
    if rec.get("status") != "done":
        return False
    files = rec.get("files") or {}
    if not files:
        return False
    for name in files.values():
        p = ctx.assets_dir / str(name)
        if not p.is_file() or p.stat().st_size < ctx.min_bytes:
            return False
    return True


def unit_inputs_hash(u: Unit, ctx: RunContext) -> str:
    """本 unit 的 yaml 输入行指纹（§5 失效判定之一）。

    覆盖：整条角色记录（身份锚变动 ⇒ 该角色全部资产重渲）+ unit 名 + 派生 seed +
    通道锚点 + 分辨率 + 候选数。display name 不参与（改名不重渲）。
    """
    return _hash_fields({
        "char": {"id": u.char["id"], "anchor_en": u.char["anchor_en"],
                 "outfit_en": u.char["outfit_en"],
                 "negative_extra": u.char["negative_extra"], "seed": u.char["seed"]},
        "unit": {"kind": u.kind, "name": u.name_orig, "seeds": u.seeds},
        "style": ctx.style, "style_anchor": ctx.style_anchor,
        "negative_anchor": ctx.negative_anchor,
        "candidates": ctx.candidates if u.is_threeview else 1,
        "width": ctx.width, "height": ctx.height,
        "prop_anchor": ctx.catalog.get(u.name_orig, "") if u.kind == "prop" else "",
    })


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _ascii_prefix(u: Unit, dest: str) -> str:
    """SaveImage filename_prefix（ComfyUI 服务端 output 子路径，仅审计用途；
    本地落盘走 download_images(dest_name=…) 不依赖它）。

    char_id 已校验纯 ASCII；dest 的 stem 可能含中文表情/道具名（safe_name 保留中文），
    非 ASCII 时以内容哈希替代，保证服务端路径纯 ASCII 且逐槽唯一（c1/c2 各自可审计）。
    """
    stem = Path(dest).stem
    if stem.isascii():
        return "pipeline_assets/%s/%s" % (u.char_id, stem)
    digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
    return "pipeline_assets/%s/u-%s-%s" % (u.char_id, digest, stem.encode("ascii", "ignore").decode("ascii") or "slot")


def _build_workflow(u: Unit, ctx: RunContext, prompt: str, negative: str, seed: int,
                    dest: str) -> dict:
    chan = (ctx.cfg.get("channels") or {}).get(ctx.style)
    if not isinstance(chan, dict) or not chan:
        raise AssetsError("config channels.%s 缺失（assets 段必需）" % ctx.style)
    return build_t2i(ctx.style, prompt=prompt, negative=negative, seed=seed,
                     width=ctx.width, height=ctx.height,
                     filename_prefix=_ascii_prefix(u, dest), params=chan)


def _download(client: Any, entry: Dict[str, Any], ctx: RunContext, dest_name: str) -> Path:
    files = client.download_images(entry, str(ctx.assets_dir), dest_name=dest_name)
    if not files:
        raise AssetsError("ComfyUI 返回了完成状态但没有图片输出（dest=%s）" % dest_name)
    return Path(files[0])


def _render_slot(client: Any, ctx: RunContext, u: Unit, mf: Dict[str, Any],
                 prompt: str, negative: str,
                 seed: int, dest: str, candidate: Optional[int]) -> Path:
    wf = _build_workflow(u, ctx, prompt, negative, seed, dest)
    ctx.log("[%s] 提交 %s seed=%s channel=%s %dx%d"
            % (u.key, dest, seed, ctx.style, ctx.width, ctx.height))
    ctx.log("[%s] prompt: %s" % (u.key, prompt))
    ctx.log("[%s] negative: %s" % (u.key, negative))
    ctx.log("[%s] wf: %s" % (u.key, json.dumps(
        {nid: {"class_type": n["class_type"],
               "inputs": {k: v for k, v in n.get("inputs", {}).items() if not isinstance(v, list)}}
         for nid, n in wf.items()}, ensure_ascii=False)))
    pid = client.queue_prompt(wf)
    # 提交即落槽级 prompt_id（进程若死在此后，孤儿收敛按槽回捞各自的 job，§4.0.2/§5）
    # 槽键与 files/render_unit 一致：threeview=c1..cN；单图=img
    slot = ("c%d" % candidate) if u.is_threeview else "img"
    rec = _mf_unit(mf, u.key)
    pids = dict(rec.get("prompt_ids") or {})
    pids[slot] = pid
    _mf_mark(mf, u.key, prompt_id=pid, prompt_ids=pids)
    _mf_save(ctx.mf_path, mf)
    entry = client.wait(pid, timeout_s=ctx.timeout_s, quiet_first_s=ctx.quiet_first_s)
    out = _download(client, entry, ctx, dest)
    if out.stat().st_size < ctx.min_bytes:
        raise AssetsError("下载图片过小（%d < %d 下限）：%s" % (out.stat().st_size, ctx.min_bytes, out))
    write_meta(out, unit_key=u.key, kind=u.kind, char_id=u.char_id, channel=ctx.style,
               seed=seed, prompt=prompt, negative=negative, width=ctx.width,
               height=ctx.height, cfg=ctx.cfg, prompt_id=pid, candidate=candidate)
    return out


def render_unit(u: Unit, mf: Dict[str, Any], ctx: RunContext) -> bool:
    """渲染一个 unit（总尝试 ctx.attempts 次；失败记录 error，不阻塞其它 unit）。

    slot 级"已在盘且达标则跳过"仅用于**崩溃续跑**：进入前状态非 done **且**
    inputs_hash/stage_cfg_hash 与本次一致（--force 一律关）。stale（原状态 done 但
    输入/配置变了）与"失败后改配置再跑"都必须真重渲——否则盘上旧图会短路失效判定，
    或新旧采样参数的图混进同一候选组。
    """
    prev_status = (_mf_unit(mf, u.key).get("status"))
    # 槽级跳过仅限"崩溃续跑"：状态非 done（本次 unit 未完成）且输入/段配置未变——
    # 失败后改过配置（hash 不一致）必须整 unit 重渲，否则新旧采样参数的图混进同一候选组
    allow_slot_skip = (
        prev_status != "done" and not ctx.force
        and _mf_unit(mf, u.key).get("inputs_hash") == unit_inputs_hash(u, ctx)
        and _mf_unit(mf, u.key).get("stage_cfg_hash") == ctx.stage_cfg_hash
    )
    prompt = compose_prompt(u, ctx.style_anchor, ctx.catalog.get(u.name_orig or "", ""))
    negative = compose_negative(ctx.style, ctx.cfg, ctx.negative_anchor, u.char["negative_extra"])
    plan = {"prompt": prompt, "negative": negative, "seeds": list(u.seeds),
            "dests": list(u.dests), "channel": ctx.style,
            "width": ctx.width, "height": ctx.height, "kind": u.kind}
    for attempt in range(1, ctx.attempts + 1):
        try:
            _mf_mark(mf, u.key, status="running", channel=ctx.style,
                     inputs_hash=unit_inputs_hash(u, ctx),
                     stage_cfg_hash=ctx.stage_cfg_hash, plan=plan,
                     attempts=attempt, seed=u.seeds[0], error=None)
            _mf_save(ctx.mf_path, mf)  # 先落 running+plan 再提交（孤儿收敛依据）
            files: Dict[str, str] = {}
            for i, (seed, dest) in enumerate(zip(u.seeds, u.dests)):
                slot = "c%d" % (i + 1) if u.is_threeview else "img"
                p = ctx.assets_dir / dest
                if allow_slot_skip and p.is_file() and p.stat().st_size >= ctx.min_bytes:
                    files[slot] = dest  # 半途崩溃的断点：已合格的候选不重渲
                    continue
                _render_slot(ctx.client, ctx, u, mf, prompt, negative, seed, dest,
                             i + 1 if u.is_threeview else None)
                files[slot] = dest
            done_fields: Dict[str, Any] = {"status": "done", "files": files, "error": None}
            if u.is_threeview and (_mf_unit(mf, u.key).get("picked")):
                # 候选已重渲 ⇒ 旧 pick 指向被替换的候选，作废并提示重新挑（§4.1 人工关口）
                done_fields.update({"picked": False, "picked_candidate": None})
                ctx.log("[%s] 候选已重渲，旧 pick 作废——请重新 --pick" % u.key)
            _mf_mark(mf, u.key, **done_fields)
            _mf_save(ctx.mf_path, mf)
            ctx.log("[%s] OK（%d 张）" % (u.key, len(files)))
            return True
        except Exception as e:  # noqa: BLE001 - 段级容错：失败记录后继续其它 unit
            last_err = "%s: %s" % (type(e).__name__, e)
            ctx.log("[%s] 第 %d/%d 次尝试失败：%s" % (u.key, attempt, ctx.attempts, last_err))
            _mf_mark(mf, u.key,
                     status="failed" if attempt >= ctx.attempts else "pending",
                     error=last_err[:800])
            _mf_save(ctx.mf_path, mf)
            if attempt < ctx.attempts and ctx.retry_wait_s > 0:
                time.sleep(ctx.retry_wait_s)
    return False


# --------------------------------------------------------------------------- #
# 孤儿收敛（§4.0.2：history 命中→收割；queue 在列→等待收割绝不重提；都查无→pending）
# --------------------------------------------------------------------------- #
def _missing_slots(u: Unit, rec: Dict[str, Any], ctx: RunContext) -> List[Tuple[str, int, str]]:
    """缺的槽：[(slot, seed, dest)]——未记录或文件缺失/过小的候选。"""
    files = rec.get("files") or {}
    out = []
    for i, (seed, dest) in enumerate(zip(u.seeds, u.dests)):
        slot = "c%d" % (i + 1) if u.is_threeview else "img"
        p = ctx.assets_dir / dest
        if slot in files and p.is_file() and p.stat().st_size >= ctx.min_bytes:
            continue
        out.append((slot, seed, dest))
    return out


def _harvest_slot(u: Unit, mf: Dict[str, Any], ctx: RunContext, slot: str, seed: int,
                  dest: str, hist_entry: Dict[str, Any], pid: Optional[str]) -> None:
    plan = (_mf_unit(mf, u.key).get("plan")) or {}
    prompt = plan.get("prompt") or compose_prompt(u, ctx.style_anchor,
                                                  ctx.catalog.get(u.name_orig or "", ""))
    negative = plan.get("negative") or compose_negative(ctx.style, ctx.cfg,
                                                        ctx.negative_anchor, u.char["negative_extra"])
    out = _download(ctx.client, hist_entry, ctx, dest)
    cand = int(slot[1:]) if u.is_threeview else None
    write_meta(out, unit_key=u.key, kind=u.kind, char_id=u.char_id, channel=ctx.style,
               seed=seed, prompt=prompt, negative=negative,
               width=plan.get("width", ctx.width), height=plan.get("height", ctx.height),
               cfg=ctx.cfg, prompt_id=pid, candidate=cand)
    files = _mf_unit(mf, u.key).get("files") or {}
    files[slot] = dest
    _mf_mark(mf, u.key, files=files)
    _mf_save(ctx.mf_path, mf)


def converge_orphans(units: List[Unit], mf: Dict[str, Any], ctx: RunContext) -> set:
    """§4.0.2 孤儿收敛。槽级 pid（unit.prompt_ids）优先，退回 unit.prompt_id。

    判定次序（每 unit）：
      1. 逐缺槽查 history：命中的完成 job 直接收割下载（不重提）；
      2. 仍缺且任一记录 pid 仍在队列 → 等待收割（绝不同时重提新 job；超时→failed+人工处置）；
      3. 都查无（服务器重启过等）→ 置回 pending 重跑。

    返回本轮**封锁**的 unit 键集合（等待超时置 failed 的）：服务器侧 job 可能仍在
    执行，本轮主循环不得重提（双 H3/大模型 job 同跑有 OOM 风险）；人工处置后用
    --abandon-running 重置重提。
    """
    blocked: set = set()
    by_key = {u.key: u for u in units}
    for key, rec in list((mf.get("units") or {}).items()):
        if rec.get("status") != "running":
            continue
        u = by_key.get(key)
        if u is None:
            rec["status"] = "pending"  # 计划外的遗留（chars.yaml 已变）→ 重跑
            continue
        pids: Dict[str, str] = dict(rec.get("prompt_ids") or {})
        fallback_pid = rec.get("prompt_id")
        if fallback_pid and not pids:
            pids = {"img": fallback_pid} if not u.is_threeview else {
                "c%d" % (i + 1): fallback_pid for i in range(len(u.dests))}
        missing = _missing_slots(u, rec, ctx)
        if not missing:
            _mf_mark(mf, key, status="done", error=None)
            _mf_save(ctx.mf_path, mf)
            continue
        harvested = False
        for slot, seed, dest in missing:
            pid = pids.get(slot)
            if not pid:
                continue
            try:
                hist = ctx.client.history(pid)
            except Exception as e:  # noqa: BLE001 - 查询失败按查无处理
                ctx.log("[%s] 孤儿查询 history(%s) 失败（%s）" % (key, pid, e))
                continue
            entry = hist.get(pid)
            status = (entry or {}).get("status") or {}
            if entry is not None and (status.get("completed") is True
                                      or status.get("status_str") == "success"):
                _harvest_slot(u, mf, ctx, slot, seed, dest, entry, pid)
                ctx.log("[%s] 孤儿 job %s 已从 history 收割 → %s（不重提）" % (key, pid, dest))
                harvested = True
        if _missing_slots(u, _mf_unit(mf, key), ctx):
            # 仍缺：查队列（任一记录 pid 在列即等待收割）
            in_queue = [p for p in set(list(pids.values()) + ([fallback_pid] if fallback_pid else []))
                        if p and ctx.client.prompt_in_queue(p)]
            if in_queue:
                pid = in_queue[0]
                ctx.log("[%s] 孤儿 job %s 仍在 ComfyUI 队列/执行中——等待收割，绝不重提（§4.0.2）"
                        % (key, pid))
                try:
                    entry = ctx.client.wait(pid, timeout_s=ctx.timeout_s, quiet_first_s=0)
                    for slot, seed, dest in _missing_slots(u, _mf_unit(mf, key), ctx):
                        _harvest_slot(u, mf, ctx, slot, seed, dest, entry, pid)
                    ctx.log("[%s] 队列等待收割完成 → %s" % (key, pid))
                except Exception as e:  # noqa: BLE001
                    _mf_mark(mf, key, status="failed",
                             error="孤儿等待失败（人工处置：ComfyUI 端取消该 job 或重启服务；"
                                   "确认后 --abandon-running 重提）: %s" % e)
                    _mf_save(ctx.mf_path, mf)
                    blocked.add(key)
                    continue
            else:
                ctx.log("[%s] 孤儿 job history/queue 均查无（服务器重启过？）→ pending 重跑" % key)
        if _missing_slots(u, _mf_unit(mf, key), ctx):
            _mf_unit(mf, key)["status"] = "pending"
            _mf_save(ctx.mf_path, mf)
        else:
            _mf_mark(mf, key, status="done", error=None)
            _mf_save(ctx.mf_path, mf)
    return blocked


# --------------------------------------------------------------------------- #
# pick（§4.1 人工关口）
# --------------------------------------------------------------------------- #
def pick_threeview(u: Unit, mf: Dict[str, Any], ctx: RunContext, cand_no: int) -> bool:
    if not u.is_threeview:
        raise AssetsError("--pick 仅适用于三视图 unit（%s 是 %s）" % (u.key, u.kind))
    if not (1 <= cand_no <= len(u.dests)):
        raise AssetsError("候选号 c%d 越界（%s 共 %d 个候选）" % (cand_no, u.key, len(u.dests)))
    src = ctx.assets_dir / u.dests[cand_no - 1]
    if not src.is_file() or src.stat().st_size < ctx.min_bytes:
        raise AssetsError("候选文件缺失或过小：%s（先完成渲染）" % src)
    dst = ctx.assets_dir / (u.final or "")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    plan = (_mf_unit(mf, u.key).get("plan")) or {}
    write_meta(dst, unit_key=u.key, kind=u.kind, char_id=u.char_id, channel=ctx.style,
               seed=u.seeds[cand_no - 1], prompt=plan.get("prompt", ""),
               negative=plan.get("negative", ""), width=ctx.width, height=ctx.height,
               cfg=ctx.cfg, prompt_id=(_mf_unit(mf, u.key).get("prompt_id")),
               candidate=cand_no, picked=True, picked_from=src.name)
    files = _mf_unit(mf, u.key).get("files") or {}
    files["threeview"] = u.final
    _mf_mark(mf, u.key, files=files, picked=True, picked_candidate=cand_no)
    _mf_save(ctx.mf_path, mf)
    ctx.log("[%s] pick c%d → %s（keyframes 身份锚就绪）" % (u.key, cand_no, u.final))
    return True


def self_heal_picks(units: List[Unit], mf: Dict[str, Any], ctx: RunContext) -> None:
    """picked 但 final 文件丢失（人工误删）→ 从记录的候选自动补拷（不动渲染状态）。"""
    by_key = {u.key: u for u in units}
    for key, rec in (mf.get("units") or {}).items():
        if not rec.get("picked"):
            continue
        u = by_key.get(key)
        if u is None or not u.is_threeview:
            continue
        final = ctx.assets_dir / str(u.final)
        if final.is_file():
            continue
        n = int(rec.get("picked_candidate") or 1)
        src = ctx.assets_dir / u.dests[n - 1]
        if src.is_file():
            shutil.copyfile(src, final)
            ctx.log("[%s] final 丢失，已自动从 c%d 补拷（self-heal）" % (key, n))


# --------------------------------------------------------------------------- #
# 权重就绪检查（缺 → 列出全部缺口，非零退出；§4.0 依赖检查）
# --------------------------------------------------------------------------- #
def missing_weights(ctx: RunContext) -> List[str]:
    models_dir = _resolve_path((ctx.cfg.get("comfy") or {}).get("models_dir", ""))
    chan = (ctx.cfg.get("channels") or {}).get(ctx.style, {})
    keys = ("unet_gguf", "clip", "vae") if ctx.style == "photo" else ("checkpoint",)
    missing: List[str] = []
    for k in keys:
        rel = chan.get(k)
        if not rel:
            missing.append("channels.%s.%s 未配置" % (ctx.style, k))
            continue
        p = Path(str(rel))
        if not p.is_absolute():
            p = models_dir / p
        if not p.is_file() or p.stat().st_size <= 0:
            missing.append("channels.%s.%s 不在盘: %s" % (ctx.style, k, p))
    return missing


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _resolve_project_dir(project: str, cfg: Dict[str, Any]) -> Path:
    raw = Path(str(project))
    if raw.is_dir():
        return raw.resolve()
    root = _resolve_path((cfg.get("paths") or {}).get("projects_root", "projects"))
    cand = root / str(project)
    if cand.is_dir():
        return cand.resolve()
    known = ""
    if root.is_dir():
        names = sorted(d.name for d in root.iterdir() if d.is_dir())[:10]
        known = "；已知项目: %s" % ", ".join(names) if names else ""
    raise AssetsError("找不到项目 %r（尝试过 %s%s）" % (str(project), cand, known))


def _validate_project(ctx: RunContext) -> List[str]:
    """project.yaml + 配置节全量校验（一次性列出）。"""
    errors: List[str] = []
    meta = ctx.project_doc
    style = meta.get("style")
    if style not in CHANNELS:
        errors.append("project.yaml style 必须是 %s 之一，得到 %r（assets 段通道必须显式声明）"
                      % ("/".join(CHANNELS), style))
    seed_base = meta.get("seed_base")
    if not isinstance(seed_base, int) or isinstance(seed_base, bool):
        errors.append("project.yaml seed_base 必须是整数（§3 规则 4 派生基），得到 %r" % (seed_base,))
    anchors = meta.get("style_anchors") or {}
    if not isinstance(anchors, dict) or not str(anchors.get(ctx.style, "")).strip():
        errors.append("project.yaml style_anchors.%s 缺失（assets 提示词需要画风锚点）" % ctx.style)
    a = ctx.cfg.get("assets") or {}
    for k in ("width", "height", "candidates"):
        v = a.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            errors.append("config assets.%s 应为正整数，得到 %r" % (k, v))
    if not isinstance(a.get("min_bytes", MIN_BYTES_DEFAULT), int):
        errors.append("config assets.min_bytes 应为整数")
    return errors


def run_stage(project_dir, config=None, args=None) -> int:
    """assets 段主入口（stages 包约定签名）。

    :param project_dir: 项目目录（或项目名——按 cfg paths.projects_root 解析）
    :param config: 合并后配置 dict / config.MergedConfig；None 时自行加载合并
    :param args: StageOptions / argparse.Namespace / dict（only/force/dry_run/auto_pick/
                 pick/keep_stale/abandon_running/sets）
    :return: 0 成功；1 失败或输入错误；2 ComfyUI 不可达（§4.0.1）
    """
    opts = _coerce_args(args)
    try:
        cfg, project_doc, project_dir = _load_stage_config(
            project_dir, opts.sets, config=getattr(config, "cfg", config))
    except AssetsError as e:
        _safe_print("assets 段中止：%s" % e)
        return 1
    except ValueError as e:  # config_mod.ConfigError / miniyaml.MiniYAMLError 均为 ValueError 子类
        _safe_print("assets 段配置/解析错误：%s" % e)
        return 1
    project_name = project_dir.name
    logs_dir = _resolve_path((cfg.get("paths") or {}).get("logs_dir", "logs"))
    log = StageLog(None if opts.dry_run else logs_dir / ("%s_%s.log" % (project_name, STAGE)))

    # ---- 项目元数据先入 ctx，再全量校验（校验依赖 style 选锚点键） ------- #
    style = project_doc.get("style")
    ctx = RunContext(project_dir=project_dir, project_name=project_name,
                     assets_dir=project_dir / "assets", cfg=cfg, project_doc=project_doc,
                     opts=opts, log=log, style=str(style) if style in CHANNELS else "photo",
                     seed_base=int(project_doc.get("seed_base") or 0) if isinstance(
                         project_doc.get("seed_base"), int) else 0)
    anchors = project_doc.get("style_anchors") or {}
    ctx.style_anchor = str(anchors.get(ctx.style, "") or "").strip()
    neg_anchors = project_doc.get("negative_anchors") or {}
    ctx.negative_anchor = str(neg_anchors.get(ctx.style, "") or "").strip()

    # ---- 输入校验（全量列出，§2.2 精神） -------------------------------- #
    errs = _validate_project(ctx)
    a = cfg.get("assets") or {}
    ctx.width = int(a.get("width", 1024))
    ctx.height = int(a.get("height", 1024))
    ctx.candidates = int(a.get("candidates", 2))
    ctx.min_bytes = int(a.get("min_bytes", MIN_BYTES_DEFAULT))
    run_cfg = cfg.get("run") or {}
    ctx.attempts = max(1, int(run_cfg.get("attempts", 2)))
    ctx.retry_wait_s = float(run_cfg.get("retry_wait_s", 20))
    ctx.quiet_first_s = float(run_cfg.get("quiet_first_s", 5))
    ctx.timeout_s = float((cfg.get("comfy") or {}).get("timeout_img_s", 900))
    ctx.precheck = bool(run_cfg.get("server_precheck", True))
    ctx.stage_cfg_hash = _stage_cfg_hash(cfg)
    ctx.force = bool(opts.force)

    chars_path = project_dir / "characters.yaml"
    if not chars_path.is_file():
        log("assets 段中止：未找到 %s（assets 段必需，设计 §4.1/§6）" % chars_path)
        return 1
    try:
        chars_doc = miniyaml.load(chars_path)
    except miniyaml.MiniYAMLError as e:
        log("assets 段中止：characters.yaml 解析失败：%s" % e)
        return 1
    ctx.chars, ctx.catalog, char_errs = validate_characters(chars_doc)
    errs.extend(char_errs)
    if errs:
        for e in errs:
            log("校验错误：%s" % e)
        log("assets 段中止：共 %d 条输入错误（一次性全部列出，修 yaml 后重跑）" % len(errs))
        return 1

    units = plan_units(ctx.chars, ctx.seed_base, ctx.candidates)
    if opts.only:
        tokens = [t.strip() for t in opts.only if t.strip()]
        units = [u for u in units
                 if any(t == u.char_id or u.key == t or u.key.startswith(t + ":") for t in tokens)]
        if not units:
            log("assets 段中止：--only 过滤后没有匹配的 unit（tokens=%s）" % tokens)
            return 1

    # ---- --pick 独立操作（不渲染） -------------------------------------- #
    ctx.mf_path = ctx.assets_dir / "assets_manifest.json"
    try:
        mf = _mf_load(ctx.mf_path)
    except AssetsError as e:
        log("assets 段中止：%s" % e)
        return 1
    if mf.get("stage") not in ("", None, STAGE, "unknown"):
        log("assets 段中止：manifest 段名不符（%s 声明 %r，期望 %s）——请人工检查 %s"
            % (ctx.mf_path, mf.get("stage"), STAGE, ctx.mf_path))
        return 1
    mf["stage"] = STAGE
    mf["project"] = project_name
    if opts.pick:
        spec = str(opts.pick).strip()
        if ":" not in spec or not spec.rsplit(":", 1)[1].lower().startswith("c"):
            log("--pick 格式应为 <char_id>:cN（如 detective:c2），得到 %r" % spec)
            return 1
        cid, tail = spec.rsplit(":", 1)
        try:
            n = int(tail.lower()[1:])
        except ValueError:
            log("--pick 候选号非法：%r（应为 c1/c2/…）" % tail)
            return 1
        target = [u for u in units if u.char_id == cid and u.is_threeview]
        if not target:
            log("--pick 找不到角色 %r 的三视图 unit（characters.yaml 里有该角色且 three_view: true？）" % cid)
            return 1
        try:
            pick_threeview(target[0], mf, ctx, n)
        except (AssetsError, OSError) as e:
            log("--pick 失败：%s" % e)
            return 1
        return 0

    if opts.dry_run:
        log("== assets 段计划（dry-run：不连服务器、不写盘） ==")
        log("项目 %s | 通道 %s | %dx%d | 三视图候选 %d | unit 数 %d | 段配置哈希 %s"
            % (project_name, ctx.style, ctx.width, ctx.height, ctx.candidates, len(units),
               ctx.stage_cfg_hash[:19]))
        need = 0
        for i, u in enumerate(units, 1):
            rec = mf["units"].get(u.key) or {}
            complete = unit_complete(rec, u, ctx) if rec else False
            if not complete:
                need += 1
            prompt = compose_prompt(u, ctx.style_anchor, ctx.catalog.get(u.name_orig or "", ""))
            log("[%d/%d] %s seeds=%s → %s%s"
                % (i, len(units), u.key, ",".join(str(s) for s in u.seeds),
                   "+".join(u.dests) if not u.is_threeview else ",".join(u.dests) + "（pick→%s）" % u.final,
                   "  [已完成，跳过]" if complete else ""))
            log("        prompt: %s…" % prompt[:96])
        log("汇总：待渲染 %d / 已完成跳过 %d；三视图渲染后需 --pick / --auto-pick（§4.1 人工关口）"
            % (need, len(units) - need))
        mw = missing_weights(ctx)
        if mw:
            log("警告：权重缺口（渲染前须就绪）：%s" % "；".join(mw))
        return 0

    # 定稿自愈（picked 但 final 被误删 → 从记录候选补拷）；置于 dry-run 之后保证零写盘
    self_heal_picks(plan_units(ctx.chars, ctx.seed_base, ctx.candidates), mf, ctx)

    # ---- 权重就绪检查 ---------------------------------------------------- #
    mw = missing_weights(ctx)
    if mw:
        for m in mw:
            log("权重缺失：%s" % m)
        log("assets 段中止：权重不在盘（models_dir=%s）——值班工程师补齐后重跑" % mw[0].split(":")[-1].strip())
        return 1

    # ---- 服务器预检（§4.0.1：不可达 exit 2，不让 unit 空转） ------------- #
    ctx.client = comfy_mod.build_client(cfg)
    if ctx.precheck and not ctx.client.reachable():
        log("ComfyUI 未启动（当前 8188 停机即此情形）：请先运行 run_nvidia_gpu.bat")
        log("assets 段中止（预检失败，exit 2——区别于渲染失败 exit 1）")
        return 2

    # ---- 孤儿收敛 / 强制重置 --------------------------------------------- #
    all_units = plan_units(ctx.chars, ctx.seed_base, ctx.candidates)
    blocked: set = set()
    if opts.abandon_running:
        n = 0
        for rec in (mf.get("units") or {}).values():
            if rec.get("status") == "running":
                rec["status"] = "pending"
                n += 1
        if n:
            log("--abandon-running：把 %d 个 running unit 置回 pending（确认服务器侧已无该 job）" % n)
    else:
        blocked = converge_orphans(all_units, mf, ctx)

    # ---- 渲染循环 -------------------------------------------------------- #
    failures: List[str] = []
    skipped = 0
    stale = 0
    for u in units:
        if u.key in blocked:
            # 孤儿等待超时封锁：服务器侧 job 可能仍在执行，本轮绝不重提（§4.0.2）
            log("[%s] 孤儿等待超时已封锁——人工处置（ComfyUI 端取消该 job 或重启）后 --abandon-running" % u.key)
            failures.append(u.key)
            continue
        rec = _mf_unit(mf, u.key)
        cur_hash = unit_inputs_hash(u, ctx)
        if unit_complete(rec, u, ctx) and not opts.force:
            reason = None
            if manifest_mod is not None:
                reason = manifest_mod.stale_reason(rec, inputs_hash=cur_hash,
                                                   stage_cfg_hash=ctx.stage_cfg_hash)
            else:
                if rec.get("inputs_hash") != cur_hash:
                    reason = "inputs_hash 变化"
                elif rec.get("stage_cfg_hash") != ctx.stage_cfg_hash:
                    reason = "段配置哈希变化"
            if reason:
                if opts.keep_stale:
                    log("[%s] 已 stale（%s）但 --keep-stale：保留旧产物" % (u.key, reason))
                    skipped += 1
                    continue
                log("[%s] stale（%s）→ 重渲（--keep-stale 可保留）" % (u.key, reason))
                stale += 1
            else:
                log("[%s] 已完成，跳过（断点续跑）" % u.key)
                skipped += 1
                continue
        if not render_unit(u, mf, ctx):
            failures.append(u.key)

    # ---- auto-pick（无人值守批跑：全部取第 1 候选） ---------------------- #
    if opts.auto_pick:
        for u in all_units:
            if not u.is_threeview or u.char_id not in {x.char_id for x in units}:
                continue
            rec = mf["units"].get(u.key) or {}
            if rec.get("picked"):
                continue
            if unit_complete(rec, u, ctx):
                try:
                    pick_threeview(u, mf, ctx, 1)
                except (AssetsError, OSError) as e:
                    log("--auto-pick %s 失败：%s" % (u.key, e))
                    failures.append(u.key)

    mf["config_hash"] = ctx.stage_cfg_hash
    _mf_save(ctx.mf_path, mf)

    # ---- 汇总 ------------------------------------------------------------ #
    n_units = len(units)
    unpicked = [u.key for u in units
                if u.is_threeview and not (mf["units"].get(u.key) or {}).get("picked")]
    log("== assets 段汇总：unit %d | 跳过 %d | stale 重渲 %d | 失败 %d ==" %
        (n_units, skipped, stale, len(failures)))
    if unpicked:
        log("待人工 pick 的三视图（keyframes 段门禁，§4.1）：%s；或用 --auto-pick 取第 1 候选"
            % ", ".join(unpicked))
    if failures:
        log("失败 unit 清单：%s" % ", ".join(failures))
        return 1
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def selftest_checks(with_server: bool = False, strict: bool = False) -> list:
    """段自检注册表（根 selftest.py C 节聚合约定；实现见 assets_selftest.selftest_checks）。

    延迟导入避免循环依赖（assets_selftest → assets）。
    """
    from pipeline.stages.assets_selftest import selftest_checks as _sc
    return _sc(with_server=with_server, strict=strict)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.stages.assets",
        description="assets 段：按 characters.yaml 产出角色设定资产（三视图/表情差分/道具，"
                    "内容中性、全身着装的标准设定图——见模块文档内容边界）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m pipeline.stages.assets 我的新剧 --dry-run\n"
            "  python -m pipeline.stages.assets 我的新剧 --only detective\n"
            "  python -m pipeline.stages.assets 我的新剧 --pick detective:c2\n"
            "  python -m pipeline.stages.assets 我的新剧 --auto-pick\n"
            "  python -m pipeline.stages.assets --selftest\n"
        ))
    ap.add_argument("project", nargs="?", help="项目名（projects/<名>）或项目目录路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划（不连服务器、不写盘）")
    ap.add_argument("--only", default="", help="逗号分隔过滤：char_id 或完整 unit 键")
    ap.add_argument("--force", action="store_true", help="忽略完成状态强制重渲")
    ap.add_argument("--pick", default=None, metavar="CHAR:cN", help="人工挑三视图候选并定名")
    ap.add_argument("--auto-pick", action="store_true", help="全部未 pick 三视图取第 1 候选")
    ap.add_argument("--keep-stale", action="store_true", help="stale 时保留旧产物不重渲")
    ap.add_argument("--abandon-running", action="store_true",
                    help="把孤儿 running unit 强制置回 pending 重提（确认服务器侧已无该 job）")
    ap.add_argument("--set", action="append", default=[], dest="sets", metavar="a.b=v",
                    help="覆盖配置（可重复，如 --set assets.candidates=3）")
    ap.add_argument("--selftest", action="store_true", help="干跑自检（不碰 GPU/ComfyUI）")
    args = ap.parse_args(argv)

    try:  # Windows 控制台 UTF-8（中文项目名/提示词预览）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    if args.selftest:
        from pipeline.stages.assets_selftest import run_selftest
        return 0 if run_selftest() else 1
    if not args.project:
        ap.error("缺少 project（或用 --selftest）")  # exit 2
    return run_stage(args.project, None, args)


if __name__ == "__main__":
    sys.exit(main())
