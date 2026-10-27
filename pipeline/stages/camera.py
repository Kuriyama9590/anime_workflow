# -*- coding: utf-8 -*-
"""camera 段（段3）适配层：逐镜调度 pipeline.camera.run_camera 的公开 CLI。

真正的运镜逻辑（模式A Blender codegen/渲染、模式B 词库 camdesc）在
pipeline/camera/run_camera.py（camera 模块，只读复用不重复实现）。本适配层负责：
  - 从 shots.yaml 取每镜 camera 节（单镜 > defaults，整体覆盖不合并，§4.0）；
  - 模式A 时长走 §4.4 三步网格：eff_duration → timing.map_duration 得 length/eff_s，
    以 --duration/--fps/--width/--height 显式传给 run_camera（其默认 5.17s/124 帧
    绝不静默生效），duration 变化计入 inputs_hash（改时长判 stale 重渲）；
  - LLM 桥接：config.yaml camera.llm.* 不被 run_camera 直接消费（其配置源是
    PIPELINE_LLM_CONFIG 指向的 JSON / pipeline/camera/llm_config.json + 环境变量，
    见 camera/llm_bpy.py load_config）——本层把 config 节合成临时 JSON 注入
    PIPELINE_LLM_CONFIG，并据此决定 --llm；--trust-llm 追加免审阅执行；
  - unit=镜，manifest 断点续跑（完成即跳过；inputs_hash/stage_cfg_hash 变化判
    stale 默认重跑，--keep-stale 保留）；
  - SystemExit 兜底：run_camera CLI 对非法参数抛 SystemExit(2)（argparse），
    逐镜捕获记 failed，绝不让 unit 卡 running 或异常穿透调度器。

退出码：0=成功/全部跳过；1=输入错误或存在失败镜。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .. import config as config_mod
from .. import manifest as mf
from .. import miniyaml, schema as schema_mod
from .. import timing
from . import MIN_BYTES, StageOpts

STAGE = "camera"
NEEDS_COMFY = False  # §4.0：模式A 用 Blender、模式B 产物是纯文本 camdesc


def _load(project_dir) -> tuple[Path, dict, dict]:
    pdir = Path(project_dir)
    shots_path = pdir / "shots.yaml"
    if not shots_path.is_file():
        raise FileNotFoundError("未找到 %s（camera 段必需）" % shots_path)
    doc = miniyaml.load(shots_path) or {}
    if not isinstance(doc.get("shots"), list) or not doc["shots"]:
        raise ValueError("shots.yaml 缺少非空 shots 列表")
    return pdir, doc, doc["shots"]


def plan(project_dir, cfg: dict | None = None) -> list[dict]:
    """每镜一个 unit；mode 取 单镜 camera.mode > defaults > config camera.mode_default。

    模式A 时长经 §4.4 三步网格映射（请求域校验 → k=round((d×fps−b)/a) 夹取 → 半步
    容差断言），duration/length/fps/尺寸全部计入 inputs_hash（改时长即 stale 重渲）。
    网格无法命中的镜：unit 带 error 字段，由 run_stage 逐镜记 failed 不阻塞其它镜。
    """
    cfg = cfg or config_mod.load(str(project_dir))
    pdir, doc, shots = _load(project_dir)
    mode_default = str((cfg.get("camera") or {}).get("mode_default", "B")).upper()
    vcfg = cfg.get("video") or {}
    units = []
    for sh in shots:
        sid = sh.get("id")
        if not isinstance(sid, str):
            continue
        cam = schema_mod.eff_camera(sh, doc)
        mode = str(cam.get("mode") or mode_default).upper()
        output = str(cam.get("output") or "endpoints")
        if mode == "B":
            files = {"camdesc": "%s_camdesc.txt" % sid}
            inputs = {"sid": sid, "mode": mode, "move": cam.get("move"),
                      "intensity": cam.get("intensity"), "extra": cam.get("extra"),
                      "output": output}
            units.append({"unit": sid, "sid": sid, "mode": mode,
                          "move": str(cam.get("move") or "push_in"),
                          "intensity": str(cam.get("intensity") or ""),
                          "extra": str(cam.get("extra") or ""),
                          "output": output, "files": files,
                          "inputs_hash": mf.hash_fields(inputs)})
            continue
        # 模式A：时长三步网格（docs §4.4），run_camera 只收秒数 → 传网格有效值
        files = ({"video": "%s.mp4" % sid} if output == "full_video"
                 else {"camfirst": "%s_camfirst.png" % sid,
                       "camlast": "%s_camlast.png" % sid})
        u = {"unit": sid, "sid": sid, "mode": mode,
             "move": str(cam.get("move") or "push_in"),
             "intensity": str(cam.get("intensity") or ""),
             "extra": str(cam.get("extra") or ""), "output": output, "files": files}
        try:
            d = schema_mod.eff_duration(sh, doc, cfg)
            r = timing.map_duration(d, vcfg)
        except timing.GridError as e:
            u["error"] = "时长网格未命中：%s" % e
            # 错误分支也写 inputs_hash（记录 camera 输入行指纹，改动可审计可重试）
            u["inputs_hash"] = mf.hash_fields({
                "sid": sid, "mode": mode, "move": cam.get("move"),
                "intensity": cam.get("intensity"), "extra": cam.get("extra"),
                "output": output, "grid_error": u["error"]})
            units.append(u)
            continue
        u.update(duration_s=round(r["eff_s"], 6), length=r["length"],
                 fps=int(vcfg.get("fps", 24)),
                 width=int(vcfg.get("width", 1344)), height=int(vcfg.get("height", 768)))
        u["inputs_hash"] = mf.hash_fields({
            "sid": sid, "mode": mode, "move": cam.get("move"),
            "intensity": cam.get("intensity"), "extra": cam.get("extra"),
            "output": output, "duration_s": u["duration_s"], "length": u["length"],
            "fps": u["fps"], "width": u["width"], "height": u["height"]})
        units.append(u)
    return units


def _argv_for(pdir: Path, u: dict, opts: StageOpts, llm_active: bool) -> list[str]:
    outdir = str(pdir / "camera")
    if u["mode"] == "B":
        argv = ["modeB", "--shot", u["sid"], "--move", u["move"], "--outdir", outdir]
        if u["intensity"]:
            argv += ["--intensity", u["intensity"]]
        if u["extra"]:
            argv += ["--extra", u["extra"]]
        return argv
    argv = ["modeA", "--shot", u["sid"],
            "--image-first", str(pdir / "keyframes" / ("%s_first.png" % u["sid"])),
            "--move", u["move"], "--outdir", outdir,
            # §4.4 网格：绝不依赖 run_camera 的 5.17s/124 帧默认值
            "--duration", "%.6f" % u["duration_s"], "--fps", str(u["fps"]),
            "--width", str(u["width"]), "--height", str(u["height"])]
    last_png = pdir / "keyframes" / ("%s_last.png" % u["sid"])
    if last_png.is_file():
        argv += ["--image-last", str(last_png)]
    if u["output"] == "full_video":
        argv += ["--output", "animation"]
    if llm_active:
        argv += ["--llm"]  # 启用 LLM 生成（未配置/失败自动回退预设模板）
        if opts.trust_llm:
            argv += ["--trust-llm"]  # 免人工审阅直接执行（默认只落盘，§10.6）
    return argv


class _LLMBridge:
    """把 config.yaml camera.llm.* 桥接为 run_camera 认识的 PIPELINE_LLM_CONFIG。

    camera 模块的配置源是 JSON 文件/环境变量（llm_bpy.load_config），不读 config.yaml；
    不桥接则用户按 docs §2.1 配置会静默走预设模板。桥接只发生在 enabled 且关键项
    齐全时；用法：with _LLMBridge(cfg, log): …（进程内 env 注入，退出恢复）。
    """

    def __init__(self, cfg: dict, log):
        llm_cfg = (cfg.get("camera") or {}).get("llm") or {}
        self.active = False
        self._tmp = None
        self._saved = None
        if not llm_cfg.get("enabled"):
            return
        if not (llm_cfg.get("base_url") and llm_cfg.get("model")):
            log("警告：config.yaml camera.llm.enabled=true 但缺 base_url/model——LLM 不启用，走预设模板")
            return
        payload = {"enabled": True,
                   "base_url": str(llm_cfg["base_url"]),
                   "model": str(llm_cfg["model"]),
                   "api_key_env": str(llm_cfg.get("api_key_env", "LLM_API_KEY")),
                   "timeout_s": llm_cfg.get("timeout_s", 90),
                   "retries": llm_cfg.get("retries", 2),
                   "require_review": bool(llm_cfg.get("require_review", True))}
        fd, path = tempfile.mkstemp(prefix="camera-llm-bridge-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        self._tmp = path
        self._saved = os.environ.get("PIPELINE_LLM_CONFIG")
        os.environ["PIPELINE_LLM_CONFIG"] = path
        self.active = True
        log("camera.llm 已桥接（config.yaml → PIPELINE_LLM_CONFIG 临时文件；"
            "require_review=%s，--trust-llm 才免审阅执行）" % payload["require_review"])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._tmp:
            try:
                os.remove(self._tmp)
            except OSError:
                pass
        if self._saved is None:
            os.environ.pop("PIPELINE_LLM_CONFIG", None)
        else:
            os.environ["PIPELINE_LLM_CONFIG"] = self._saved
        return False


def _log(msg: str) -> None:
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def run_stage(project_dir, config=None, args=None) -> int:
    opts = args if isinstance(args, StageOpts) else StageOpts(
        **({"dry_run": bool(getattr(args, "dry_run", False)),
            "force": bool(getattr(args, "force", False)),
            "only": getattr(args, "only", ()),
            "trust_llm": bool(getattr(args, "trust_llm", False)),
            "keep_stale": bool(getattr(args, "keep_stale", False))} if args is not None else {}))
    cfg = config if isinstance(config, dict) else config_mod.load(str(project_dir))
    pdir, _doc, _shots = _load(project_dir)
    units = plan(project_dir, cfg)
    project_name = pdir.name
    cam_dir = pdir / "camera"
    cam_dir.mkdir(parents=True, exist_ok=True)
    mpath = mf.manifest_path_for(pdir, STAGE)
    stage_cfg_hash = config_mod.stage_sections_hash(cfg, STAGE)  # §5 段配置节哈希
    m = mf.ensure(mpath, STAGE, project_name, config_mod.config_hash(cfg))

    from pipeline.camera import run_camera  # camera 模块（已落地）
    failed, ran, skipped, stale_rerun = [], 0, 0, 0
    with _LLMBridge(cfg, _log) as llm:  # with：退出恢复 env + 清理临时 JSON（类契约）
        try:
            for u in units:
                rec = m["units"].get(u["unit"]) or {}
                if opts.only and u["sid"] not in opts.only:
                    continue
                if u.get("error"):  # plan 阶段就判死（如时长网格未命中）：逐镜失败不阻塞
                    if opts.dry_run:
                        _log("%s [DRY-RUN] 将失败：%s" % (u["unit"], u["error"]))
                        failed.append(u["unit"])
                        continue
                    mf.mark_unit(m, u["unit"], status="failed", files=u["files"],
                                 error=u["error"], inputs_hash=u.get("inputs_hash"))
                    _log("%s: FAILED（%s）" % (u["unit"], u["error"]))
                    failed.append(u["unit"])
                    continue
                # 完成判定 + 失效判定（§5：inputs_hash ∨ 段配置节哈希）
                if not opts.force and mf.unit_complete(rec, cam_dir, MIN_BYTES[STAGE]):
                    reasons = []
                    if rec.get("inputs_hash") != u["inputs_hash"]:
                        reasons.append("shots.yaml 该镜 camera/duration 输入变化")
                    if not rec.get("stage_cfg_hash"):
                        reasons.append("旧记录缺 stage_cfg_hash（补记，一次性重跑对齐）")
                    elif rec.get("stage_cfg_hash") != stage_cfg_hash:
                        reasons.append("camera 段配置节变化")
                    if reasons:
                        if opts.keep_stale:
                            _log("%s: 跳过（已 stale 但 --keep-stale 保留：%s）" % (u["unit"], "；".join(reasons)))
                            skipped += 1
                            continue
                        _log("%s: stale 重跑（%s）" % (u["unit"], "；".join(reasons)))
                        stale_rerun += 1
                    else:
                        _log("%s: 跳过（done 且产物在盘，断点续跑）" % u["unit"])
                        skipped += 1
                        continue
                argv = _argv_for(pdir, u, opts, llm.active)
                if opts.dry_run:
                    _log("%s [DRY-RUN] 将执行: python -m pipeline.camera.run_camera %s"
                         % (u["unit"], " ".join(argv)))
                    continue
                if opts.trust_llm and not llm.active and u["mode"] == "A":
                    _log("%s: --trust-llm 需要 camera.llm 配置启用（base_url/model/密钥），本次仅预设模板" % u["unit"])
                _log("%s: 执行 camera 段（mode %s%s）…" % (
                    u["unit"], u["mode"],
                    (" duration=%ss→%d帧@%dfps" % (u["duration_s"], u["length"], u["fps"]))
                    if u["mode"] == "A" else ""))
                mf.mark_unit(m, u["unit"], status="running", files=u["files"],
                             mode=u["mode"], inputs_hash=u["inputs_hash"],
                             stage_cfg_hash=stage_cfg_hash)
                mf.save(mpath, m)  # running 先落盘（kill-safe）
                try:
                    rc = int(run_camera.main(argv) or 0)
                except (Exception, SystemExit) as e:  # noqa: BLE001 —— SystemExit(2)
                    # 来自 run_camera 的 argparse（非法 move 等人工输入错误），逐镜兜住
                    rc = 1
                    _log("%s: 异常 %s: %s" % (u["unit"], type(e).__name__, e))
                if rc == 0 and mf.unit_complete({"status": "done", "files": u["files"]},
                                                cam_dir, MIN_BYTES[STAGE]):
                    extra = ({"length": u["length"], "duration_s": u["duration_s"],
                              "fps": u["fps"]} if u["mode"] == "A" else {})
                    mf.mark_unit(m, u["unit"], status="done", error=None,
                                 inputs_hash=u["inputs_hash"], stage_cfg_hash=stage_cfg_hash,
                                 **extra)
                    _log("%s: OK" % u["unit"])
                    ran += 1
                else:
                    mf.mark_unit(m, u["unit"], status="failed",
                                 error="run_camera 退出码 %s（产物未齐/过小）" % rc,
                                 stage_cfg_hash=stage_cfg_hash)
                    _log("%s: FAILED（rc=%s）" % (u["unit"], rc))
                    failed.append(u["unit"])
        finally:
            if not opts.dry_run:
                mf.save(mpath, m)
    total = len([u for u in units if not opts.only or u["sid"] in opts.only])
    _log("== camera 段结束：共 %d 镜，OK %d，跳过 %d，stale 重跑 %d，失败 %d%s =="
         % (total, ran, skipped, stale_rerun, len(failed),
            ("（%s）" % ",".join(failed)) if failed else ""))
    return 1 if failed else 0
