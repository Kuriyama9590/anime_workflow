# -*- coding: utf-8 -*-
"""五段执行器包（设计文档 §1/§4）—— 只做编排，不直接拼工作流。

段模块与归属（六模块并行构建，docs §11 落地顺序）：
  - keyframes  段2：photo 通道关键帧（已落地，自包含，见 keyframes.py 头注）
  - video      段4：本地 MiniMax H3 首尾帧图生视频（已落地，自带配置装配与 selftest_checks）
  - camera     段3：适配层（本包），转发 pipeline.camera.run_camera 的公开 CLI
  - final      段5：合成（补轨→concat→SRT/标题卡烧录→contact sheet，已落地，final.py 头注）
  - assets     段1：角色设定资产（photo 模块，已落地，含 pick 门与 selftest_checks）

五段执行器均已落地；段模块缺失/未落地时 load_stage 抛 StageNotImplemented
（cli 映射退出码 3）——该路径留给后续新增段（如 anime 通道独立段）。

段入口约定（本包既有契约）：``run_stage(project_dir, config, args) -> int``（退出码），
供 cli.py（核心模块）按 §4.0 公共前置调度。video 段签名不同（自带 (cfg, doc, shots,
meta) 装配），invoke_stage() 按 hasattr(load_stage_config) 自适应。

退出码约定（docs §4.0/§8）：0=成功或全部跳过；1=存在失败 unit 或输入错误；
2=ComfyUI 不可达（预检，区别于渲染失败）；3=段执行器未落地。
"""
from __future__ import annotations

import importlib
from pathlib import Path

from .. import config as config_mod

__all__ = ["STAGE_ORDER", "NEEDS_COMFY", "MIN_BYTES", "StageNotImplemented",
           "load_stage", "stage_available", "StageOpts", "invoke_stage",
           "dependency_gaps"]

STAGE_ORDER = ("assets", "keyframes", "camera", "video", "final")
NEEDS_COMFY = {"assets", "keyframes", "video"}

# §5 完成判定的段下限：图 100KB / 视频 200KB；camera（camdesc 纯文本）与
# final（srt/对照表大小不一）按存在性判定，细粒度下限由各段自己掌握。
MIN_BYTES = {"assets": 100_000, "keyframes": 100_000, "camera": 0,
             "video": 200_000, "final": 0}

_LANDING = {
    # 五段均已落地（assets/keyframes=photo 模块，camera=适配层，video=video 模块，
    # final=final 模块）；此表留给后续新增段（如 anime 通道独立段）。
}


class StageNotImplemented(RuntimeError):
    """段执行器未落地（并行构建中）。cli 映射为退出码 3，不算渲染失败。"""

    def __init__(self, stage: str):
        self.stage = stage
        super().__init__("%s 段执行器未落地——由 %s 提供；当前仅 plan/依赖检查可用"
                         % (stage, _LANDING.get(stage, "后续模块")))


def load_stage(stage: str):
    """import 段模块；不存在/未落地抛 StageNotImplemented（可读错误，不裸 ImportError）。"""
    if stage not in STAGE_ORDER:
        raise ValueError("未知段 %r（合法：%s）" % (stage, ", ".join(STAGE_ORDER)))
    try:
        return importlib.import_module("pipeline.stages.%s" % stage)
    except ImportError as e:
        raise StageNotImplemented(stage) from e


def stage_available(stage: str) -> bool:
    try:
        load_stage(stage)
        return True
    except StageNotImplemented:
        return False


class StageOpts:
    """cli → 段的统一选项载体（keyframes 段的 _coerce_args 按属性名取，缺省容忍）。"""

    def __init__(self, only=(), force=False, dry_run=False, keep_stale=False,
                 abandon_running=False, auto_pick=False, trust_llm=False,
                 wait_weights=False):
        if isinstance(only, str):
            only = tuple(x.strip() for x in only.split(",") if x.strip())
        self.only = tuple(only)
        self.force = bool(force)
        self.dry_run = bool(dry_run)
        self.keep_stale = bool(keep_stale)
        self.abandon_running = bool(abandon_running)
        self.auto_pick = bool(auto_pick)
        self.trust_llm = bool(trust_llm)
        self.wait_weights = bool(wait_weights)


def invoke_stage(stage: str, project_dir, merged_cfg: dict, sets=(), opts: StageOpts | None = None) -> int:
    """按段适配入口约定并调用 run_stage，返回退出码。

    - video 段：run_stage(project_dir, config=None, args=ns) —— config=None 时段内
      自带装配（全局 config.yaml + 项目 overrides + args.sets），--set 只转发段认识的
      键（comfy./run./video. 前缀）；核心键（paths.* 等）由 cli 消化进 merged_cfg。
    - 其余段：keyframes 约定 run_stage(project_dir, config=merged_cfg, args=opts)。
    """
    opts = opts or StageOpts()
    mod = load_stage(stage)
    if hasattr(mod, "load_stage_config"):  # video 段约定：自带装配（含 sets）
        from types import SimpleNamespace
        cli_sets = [str(item) for item in (sets or ())
                    if str(item).split("=", 1)[0].split(".")[0] in ("comfy", "run", "video")]
        ns = SimpleNamespace(only=opts.only, force=opts.force, dry_run=opts.dry_run,
                             keep_stale=opts.keep_stale, wait_weights=opts.wait_weights,
                             abandon_running=opts.abandon_running, sets=cli_sets)
        return int(mod.run_stage(str(project_dir), config=None, args=ns))
    rc = mod.run_stage(project_dir, config=merged_cfg, args=opts)
    return int(rc if isinstance(rc, int) else 0)


# --------------------------------------------------------------------------- #
# 依赖段完成度检查（§4.0.3：缺上游 → 列缺口，不自动越级）
# --------------------------------------------------------------------------- #
def _file_ok(path, min_bytes: int) -> bool:
    try:
        return Path(path).is_file() and Path(path).stat().st_size >= min_bytes
    except OSError:
        return False


def dependency_gaps(stage: str, project_dir, data: dict, cfg: dict):
    """返回 (blocking, warnings)。

    blocking 非空 → cli 列缺口后非零退出（不自动越级）；warnings 只提示。
    判据用"产物在盘且达标"（A 级存在即跳过的泛化），不单看 manifest 状态——
    keyframes 段的 assets 人工关口（§4.1 pick 门）由该段自己强制，这里只提示。
    """
    project_dir = Path(project_dir)
    shots_doc = data.get("shots") or {}
    shots = shots_doc.get("shots") or []
    blocking, warnings = [], []
    if stage == "keyframes":
        chars = (data.get("characters") or {}).get("characters") or []
        am = project_dir / "assets" / "assets_manifest.json"
        if am.is_file():
            import json
            try:
                units = (json.loads(am.read_text(encoding="utf-8")) or {}).get("units") or {}
            except (OSError, ValueError):
                units = {}
            for ch in chars:
                cid = ch.get("id") if isinstance(ch, dict) else None
                if not cid or not ((ch.get("assets") or {}).get("three_view", True)):
                    continue
                rec = units.get("%s:threeview" % cid) or {}
                if rec.get("status") != "done":
                    warnings.append("角色 %s 三视图未完成（assets 段）；keyframes 将按需拒绝" % cid)
                elif not rec.get("picked"):
                    warnings.append("角色 %s 三视图未挑选（assets 人工关口，§4.1）："
                                    "完成挑选或 --auto-pick，否则 keyframes 拒绝开跑" % cid)
    elif stage == "camera":
        from .. import schema as schema_mod
        for sh in shots:
            sid = sh.get("id")
            if not isinstance(sid, str):
                continue
            cam = schema_mod.eff_camera(sh, shots_doc)
            mode = str(cam.get("mode") or (cfg.get("camera") or {}).get("mode_default", "B")).upper()
            if mode != "A":
                continue
            for kind, name in (("first", "%s_first.png" % sid), ("last", "%s_last.png" % sid)):
                if not _file_ok(project_dir / "keyframes" / name, MIN_BYTES["keyframes"]):
                    blocking.append("镜 %s 模式A 运镜缺 keyframes/%s" % (sid, name))
    elif stage == "video":
        from .. import schema as schema_mod
        for sh in shots:
            sid = sh.get("id")
            if not isinstance(sid, str):
                continue
            cam = schema_mod.eff_camera(sh, shots_doc)
            output = str(cam.get("output") or "endpoints")
            if output == "full_video":
                if not _file_ok(project_dir / "camera" / ("%s.mp4" % sid), MIN_BYTES["video"]):
                    blocking.append("镜 %s 声明 camera full_video 但缺 camera/%s.mp4" % (sid, sid))
                continue
            # 帧源优先级同 video 段：camera endpoints（模式A）→ keyframes
            names = []
            if str(cam.get("mode") or (cfg.get("camera") or {}).get("mode_default", "B")).upper() == "A":
                names = [("camfirst", "camera/%s_camfirst.png" % sid),
                         ("camlast", "camera/%s_camlast.png" % sid)]
            first_ok = any(_file_ok(project_dir / rel, MIN_BYTES["keyframes"]) for _k, rel in names[:1]) \
                or _file_ok(project_dir / "keyframes" / ("%s_first.png" % sid), MIN_BYTES["keyframes"])
            last_ok = any(_file_ok(project_dir / rel, MIN_BYTES["keyframes"]) for _k, rel in names[1:]) \
                or _file_ok(project_dir / "keyframes" / ("%s_last.png" % sid), MIN_BYTES["keyframes"])
            if not first_ok:
                blocking.append("镜 %s 缺首帧（keyframes/%s_first.png 或 camera endpoints）" % (sid, sid))
            if not last_ok:
                blocking.append("镜 %s 缺末帧（keyframes/%s_last.png 或 camera endpoints）" % (sid, sid))
    elif stage == "final":
        have, missing = [], []
        for sh in shots:
            sid = sh.get("id")
            if not isinstance(sid, str):
                continue
            got = _file_ok(project_dir / "video" / ("%s.mp4" % sid), MIN_BYTES["video"]) or \
                _file_ok(project_dir / "camera" / ("%s.mp4" % sid), MIN_BYTES["video"])
            (have if got else missing).append(sid)
        if not have:
            blocking.append("无任何视频产物（video/*.mp4 或 camera/*.mp4 均缺）——先跑 video 段")
        elif missing:
            warnings.append("缺镜（final 将告警剔除后继续，§4.5）：%s" % ", ".join(missing))
    return blocking, warnings
