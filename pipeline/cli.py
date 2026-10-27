# -*- coding: utf-8 -*-
"""统一 CLI（docs §8）：

    python -m pipeline new <名> [--style photo|anime] [--force] [--set a.b=c]
    python -m pipeline run <名> [--stage assets|keyframes|camera|video|final|all]
                            [--stage screenplay-write|breakdown|screenplay]
                            [--premise "…"] [--style-note "…"] [--shots N] [--total-s S]
                            [--only S1,S3] [--force] [--dry-run] [--set a.b=c]
                            [--keep-stale] [--auto-pick] [--abandon-running]
                            [--trust-llm] [--wait-weights]
    python -m pipeline status <名> [--set a.b=c]
    python -m pipeline doctor [--with-server] [--set a.b=c]
    python -m pipeline recover <名> [--pid <id>] [--set a.b=c]

退出码：0=成功；1=失败/输入错误；2=ComfyUI 不可达（§4.0 预检语义）；
3=所选段执行器未落地（并行构建中，docs §11）。
段间依赖链：assets →(角色 pick) keyframes → camera → video → final；
--stage all 按序执行，每段开跑前走 §4.0 公共前置（依赖缺口 → 列出后非零退出）。

段0 编剧（pipeline/screenplay/，不进 all）：--stage screenplay-write（创意→剧本.md）/
breakdown（剧本.md→三份 yaml，未配置 LLM 时=三份人工输入的校验器）/
screenplay（= write → breakdown 串联）。--premise/--style-note/--shots/--total-s
是该段的输入（题材一句话/风格/镜数/总时长预算秒）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import config as config_mod
from . import manifest as mf
from . import miniyaml, scaffold, schema
from .stages import (MIN_BYTES, STAGE_ORDER, StageNotImplemented, StageOpts,
                     dependency_gaps, invoke_stage)

EXIT_OK, EXIT_FAIL, EXIT_SERVER, EXIT_NOTIMPL = 0, 1, 2, 3

# 段0 编剧（screenplay 包）——懒加载：模块缺失/损坏不影响既有五段命令
try:
    from .screenplay import ALIAS_STAGES, SCREENPLAY_STAGES
except ImportError:  # pragma: no cover —— 并行构建期容错
    ALIAS_STAGES, SCREENPLAY_STAGES = {}, ()


def _setup_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _say(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #
def cmd_new(args) -> int:
    merged = config_mod.load_merged(sets=args.sets)
    errs = config_mod.validate(merged.cfg)
    if errs:
        _say("配置校验失败（%d 项）：" % len(errs))
        for e in errs:
            _say("  - %s" % e)
        return EXIT_FAIL
    root = config_mod.resolved(merged.cfg).projects_root
    try:
        pdir = scaffold.new_project(args.name, args.style, root, force=args.force)
    except (FileExistsError, ValueError, schema.SchemaError) as e:
        _say("创建失败：%s" % e)
        return EXIT_FAIL
    _say("项目就绪：%s" % pdir)
    _say("下一步：编辑 剧本模板.md/characters.yaml/shots.yaml 后 "
         "`python -m pipeline run %s --stage assets --dry-run` 预览计划；"
         "或配置 LLM 后 `run %s --stage screenplay --premise \"…\"` 自动编剧+拆解"
         % (args.name, args.name))
    return EXIT_OK


def cmd_run(args) -> int:
    try:
        merged = config_mod.load_merged(args.name, sets=args.sets)
    except config_mod.ConfigError as e:
        _say("配置/项目错误：%s" % e)
        return EXIT_FAIL
    errs = config_mod.validate(merged.cfg)
    if errs:
        _say("配置校验失败（%d 项）：" % len(errs))
        for e in errs:
            _say("  - %s" % e)
        return EXIT_FAIL
    config_mod.ensure_runtime_dirs(merged.cfg)
    try:
        data = schema.load_project_data(merged.project_dir, merged.cfg)
    except (schema.SchemaError, miniyaml.MiniYAMLError) as e:
        data = None
        # 段0 编剧自行装载/校验（breakdown 校验器模式正是要给出完整问题清单）；
        # 其余段维持"装载失败即退出"的既有语义。
        if args.stage not in ("screenplay-write", "breakdown", "screenplay"):
            _say("项目数据校验失败：\n%s" % e)
            return EXIT_FAIL
        _say("项目数据校验失败（screenplay 段自行处理，校验器模式将给出完整清单）。")

    if args.stage == "all":
        selected = list(STAGE_ORDER)
    else:
        selected = []
        for s in str(args.stage).split(","):
            s = s.strip()
            if s:
                selected.extend(ALIAS_STAGES.get(s, [s]))
    unknown = [s for s in selected
               if s not in STAGE_ORDER and s not in SCREENPLAY_STAGES]
    if unknown:
        _say("未知段：%s（合法：screenplay-write|breakdown|screenplay|%s 或 all）"
             % (", ".join(unknown), "|".join(STAGE_ORDER)))
        return EXIT_FAIL

    opts = StageOpts(only=args.only, force=args.force, dry_run=args.dry_run,
                     keep_stale=args.keep_stale, abandon_running=args.abandon_running,
                     auto_pick=args.auto_pick, trust_llm=args.trust_llm,
                     wait_weights=args.wait_weights)
    _say("== run 项目=%s 段=%s dry_run=%s only=%s force=%s 配置链：%s =="
         % (args.name, "+".join(selected), opts.dry_run,
            ",".join(opts.only) or "-", opts.force, merged.source))

    not_implemented: list[str] = []
    worst_rc = EXIT_OK
    for stage in selected:
        _say("-- 段 %s --" % stage)
        if stage in SCREENPLAY_STAGES:
            # 段0 编剧：不走 §4.0 依赖链（无上游段），输入/校验由 screenplay 包自理
            try:
                from . import screenplay as screenplay_mod
                rc = screenplay_mod.run_stage(stage, merged.project_dir, merged.cfg,
                                              args=args, opts=opts)
            except SystemExit as e:  # argparse 等抛 SystemExit（BaseException）——按段失败计
                _say("  段 %s 内部退出 SystemExit(%s)——按失败计" % (stage, e.code))
                rc = EXIT_FAIL
            except Exception as e:  # noqa: BLE001 —— 段级兜底，与既有段同策略
                _say("  段 %s 异常 %s: %s" % (stage, type(e).__name__, e))
                rc = EXIT_FAIL
            _say("  段 %s 退出码=%s" % (stage, rc))
            if rc != EXIT_OK:
                worst_rc = rc
                if not opts.dry_run:
                    _say("  段失败——串联段（screenplay=write→breakdown）到此停止。")
                    return rc
            continue
        blocking, warns = dependency_gaps(stage, merged.project_dir, data, merged.cfg)
        for w in warns:
            _say("  [依赖提示] %s" % w)
        if blocking:
            for b in blocking:
                _say("  [依赖缺口] %s" % b)
            if opts.dry_run:
                _say("  [DRY-RUN] 依赖缺口：正式运行将拒绝开跑（§4.0.3）；本段跳过预览，退出码按 1 记")
                worst_rc = max(worst_rc, EXIT_FAIL)
                continue
            else:
                _say("  依赖未就绪，不自动越级（§4.0.3）——先补齐上游段。")
                return EXIT_FAIL
        try:
            rc = invoke_stage(stage, merged.project_dir, merged.cfg, sets=args.sets, opts=opts)
        except StageNotImplemented as e:
            _say("  %s" % e)
            not_implemented.append(stage)
            continue
        except SystemExit as e:  # 段内 argparse 等抛 SystemExit（BaseException，普通
            # except 接不住）——按段失败计（exit 1），绝不误诊为"服务器不可达 2"
            _say("  段 %s 内部退出 SystemExit(%s)——按失败计（多为输入参数非法）" % (stage, e.code))
            rc = EXIT_FAIL
        except Exception as e:  # noqa: BLE001 —— 段级兜底：任何异常按失败计，
            # 不让 traceback 裸穿 CLI（逐镜粒度的兜底在各段内部）
            _say("  段 %s 异常 %s: %s" % (stage, type(e).__name__, e))
            rc = EXIT_FAIL
        _say("  段 %s 退出码=%s" % (stage, rc))
        if rc == EXIT_SERVER:
            _say("  ComfyUI 不可达——后续需要服务器的段不再尝试（先运行 run_nvidia_gpu.bat）。")
            return EXIT_SERVER
        if rc == EXIT_NOTIMPL:
            not_implemented.append(stage)
            continue
        if rc != EXIT_OK:
            worst_rc = rc
            if not opts.dry_run:
                _say("  段失败——下游段依赖其产物，停止（重跑本段或 --only 修复后继续）。")
                return rc
    if worst_rc != EXIT_OK:
        return worst_rc
    if not_implemented:
        _say("未落地的段（并行构建中，docs §11）：%s" % ", ".join(not_implemented))
        return EXIT_NOTIMPL
    return EXIT_OK


def cmd_status(args) -> int:
    try:
        merged = config_mod.load_merged(args.name, sets=args.sets)
    except config_mod.ConfigError as e:
        _say("配置/项目错误：%s" % e)
        return EXIT_FAIL
    errs = config_mod.validate(merged.cfg)
    if errs:
        for e in errs:
            _say("配置问题：%s" % e)
        return EXIT_FAIL
    pdir = Path(merged.project_dir)
    _say("项目 %s（%s）配置哈希 %s" % (args.name, pdir, merged.config_hash[:19]))
    overall_bad = False
    for stage in STAGE_ORDER:
        sdir = mf.stage_dir_for(pdir, stage)
        mpath = mf.manifest_path_for(pdir, stage)
        if not mpath.is_file():
            _say("  %-10s 未开始（无 manifest）" % stage)
            continue
        try:
            m = mf.load(mpath)
        except mf.ManifestError as e:
            _say("  %-10s manifest 损坏：%s" % (stage, e))
            overall_bad = True
            continue
        counts = mf.summary(m)
        broken = [uid for uid, u in (m.get("units") or {}).items()
                  if u.get("status") == "done"
                  and not mf.unit_complete(u, sdir, MIN_BYTES[stage])]
        failed = sorted(uid for uid, u in (m.get("units") or {}).items()
                        if u.get("status") == "failed")
        total = len(m.get("units") or {})
        stat = " ".join("%s=%d" % (k, v) for k, v in sorted(counts.items()))
        _say("  %-10s units=%-3d %s  updated=%s" % (stage, total, stat or "空", m.get("updated_at", "")))
        if broken:
            overall_bad = True
            _say("             done 但产物缺失/过小：%s" % ", ".join(sorted(broken)))
        for uid in failed:
            err = str(((m["units"][uid] or {}).get("error")) or "")[:120]
            _say("             failed %s：%s" % (uid, err))
    _say("说明：done=完成（断点续跑跳过） failed=可重入 stale=输入/上游/配置变化将重跑")
    return EXIT_FAIL if overall_bad else EXIT_OK


def cmd_doctor(args) -> int:
    merged = config_mod.load_merged(sets=args.sets)
    errs = config_mod.validate(merged.cfg)
    if errs:
        _say("配置校验失败（%d 项）：" % len(errs))
        for e in errs:
            _say("  - %s" % e)
        return EXIT_FAIL
    cfg = merged.cfg
    _say("== pipeline doctor（环境详查；selftest 的 --with-server 版思路，docs §8）==")
    r = config_mod.resolved(cfg)
    _say("仓库根：%s" % config_mod.repo_root())
    _say("projects_root：%s（%s）" % (r.projects_root, "在盘" if r.projects_root.is_dir() else "待创建"))
    _say("logs_dir：%s（%s）" % (r.logs_dir, "在盘" if r.logs_dir.is_dir() else "待创建"))
    for h in config_mod.path_hints(cfg):
        _say("[提示] %s" % h)
    _say("ComfyUI：%s" % r.server)
    _say("-- 权重（models_dir=%s）--" % r.models_dir)
    for w in config_mod.weight_checks(cfg):
        mark = "OK " if w["ok"] else "问题"
        size = "%.2fGB" % (w["size"] / 1e9) if w["exists"] else "-"
        _say("  %s %-28s %10s（下限 %.2fGB）%s %s"
             % (mark, w["label"], size, w["min_bytes"] / 1e9, w["path"], w["problem"]))
    _say("-- ffmpeg --")
    try:
        from . import ffmpeg_tool
        ff = ffmpeg_tool.FFmpegTool.from_config(cfg.get("final") or {})
        _say("  ffmpeg：%s（%s，≥4 要求满足：%s）"
             % (ff.ffmpeg, ff.version(), ff.version_tuple() >= (4, 0)))
        _say("  ffprobe：%s" % ff.ffprobe)
    except Exception as e:  # noqa: BLE001
        _say("  [提示] ffmpeg/ffprobe 不可用：%s" % e)
    _say("-- Blender（camera 模式A）--")
    try:
        from . import blender
        install_dir = config_mod.resolve_path(
            ((cfg.get("camera") or {}).get("blender") or {}).get("install_dir", ""))
        exe = blender.find_blender(str(install_dir) if str(install_dir) else None)
        _say("  %s（未装时由 camera 模块首次使用时自动安装：zipfile 解压便携包）"
             % (exe or "未安装"))
    except Exception as e:  # noqa: BLE001
        _say("  [提示] %s" % e)
    _say("-- ComfyUI 服务器 --")
    from . import comfy
    client = comfy.build_client(cfg)
    if not client.reachable():
        _say("  不可达（当前 8188 停机即此情形）：需要渲染时先运行 run_nvidia_gpu.bat")
    else:
        stats = client.system_stats()
        ver = (stats.get("system") or {}).get("comfyui_version", "?")
        _say("  可达（ComfyUI %s）" % ver)
        info = client.object_info()
        for node in ("MiniMaxH3ImageToVideo", "TextEncodeQwenImage21", "UnetLoaderGGUF"):
            _say("  节点 %-24s %s" % (node, "已装" if node in info else "未装（video 段被阻塞；H3 节点归值班工程师补装）"))
    _say("doctor 为信息性输出（环境修复项不改变退出码；结构性配置错误才非零）。")
    return EXIT_OK


def cmd_recover(args) -> int:
    """按 manifest 记录的 prompt_id 回捞 history 补下载（resume.py 思路产品化，§5）。"""
    try:
        merged = config_mod.load_merged(args.name, sets=args.sets)
    except config_mod.ConfigError as e:
        _say("配置/项目错误：%s" % e)
        return EXIT_FAIL
    pdir = Path(merged.project_dir)
    candidates = []
    for stage in STAGE_ORDER:
        mpath = mf.manifest_path_for(pdir, stage)
        if not mpath.is_file():
            continue
        try:
            m = mf.load(mpath)
        except mf.ManifestError as e:
            _say("[跳过] %s：%s" % (stage, e))
            continue
        for uid, u in (m.get("units") or {}).items():
            if u.get("prompt_id") and u.get("status") in ("running", "failed"):
                if args.pid and u["prompt_id"] != args.pid:
                    continue
                candidates.append((stage, mpath, m, uid, u))
    if not candidates:
        _say("无可回捞 unit（无 running/failed 且带 prompt_id 的记录%s）"
             % ("匹配 --pid" if args.pid else ""))
        return EXIT_OK
    _say("候选 %d 个 unit：" % len(candidates))
    for stage, _mp, _m, uid, u in candidates:
        _say("  [%s] %s status=%s pid=%s" % (stage, uid, u.get("status"), u.get("prompt_id")))
    from . import comfy
    client = comfy.build_client(merged.cfg)
    if not client.reachable():
        _say("ComfyUI 不可达：请先运行 run_nvidia_gpu.bat 再 recover。")
        return EXIT_SERVER
    recovered, pending, kept = 0, 0, 0
    for stage, mpath, m, uid, u in candidates:
        pid = u["prompt_id"]
        sdir = mf.stage_dir_for(pdir, stage)
        try:
            h = client.history(pid)
        except Exception as e:  # noqa: BLE001
            _say("  %s: history 查询失败（%s）" % (uid, e))
            kept += 1
            continue
        if pid not in h:
            mf.mark_unit(m, uid, status="pending",
                         error="recover：history 查无（服务器或已重启）→ 置回 pending 重跑")
            mf.save(mpath, m)
            _say("  %s: history 查无 → pending（重跑该段即重新提交）" % uid)
            pending += 1
            continue
        entry = h[pid]
        status = (entry.get("status") or {})
        if not (status.get("completed") is True or status.get("status_str") == "success"):
            mf.mark_unit(m, uid, status="failed",
                         error="recover：history 记录非成功（%s）"
                               % str(status.get("status_str"))[:200])
            mf.save(mpath, m)
            _say("  %s: history 为错误记录 → 保持 failed" % uid)
            kept += 1
            continue
        got = None
        for logical, fname in (u.get("files") or {}).items():
            dest = sdir / str(fname)
            if str(fname).lower().endswith(".mp4"):
                got = client.download_media(entry, dest) or got
            else:
                imgs = client.download_images(entry, sdir, dest_name=str(fname))
                got = imgs[0] if imgs else got
        if got and mf.unit_complete({"status": "done", "files": u.get("files") or {}},
                                    sdir, MIN_BYTES[stage]):
            mf.mark_unit(m, uid, status="done", error=None,
                         recovered_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            mf.save(mpath, m)
            _say("  %s: 回捞成功 → %s" % (uid, got))
            recovered += 1
        else:
            mf.mark_unit(m, uid, status="failed", error="recover：history 有记录但产物缺失/过小")
            mf.save(mpath, m)
            _say("  %s: 回捞失败（outputs 无匹配产物或过小）→ failed" % uid)
            kept += 1
    _say("recover 完成：回捞 %d，置回 pending %d，仍 failed %d" % (recovered, pending, kept))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline",
        description="AI 漫剧生产框架（docs/FRAMEWORK_DESIGN.md）：new/run/status/doctor/recover")
    sub = ap.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="新建项目（脚手架 + schema 校验）")
    p_new.add_argument("name", help="项目名（projects/ 下目录名）")
    p_new.add_argument("--style", choices=("photo", "anime"), default="photo",
                       help="项目默认通道（docs §2.2）")
    p_new.add_argument("--force", action="store_true", help="项目已存在时覆盖配置文件")
    p_new.add_argument("--set", dest="sets", action="append", default=[], metavar="a.b=c",
                       help="配置覆盖（最高优先级），可重复")
    p_new.set_defaults(func=cmd_new)

    p_run = sub.add_parser("run", help="按段执行（断点续跑：完成即跳过，失败可重入）")
    p_run.add_argument("name", help="项目名（projects/ 下）")
    p_run.add_argument("--stage", default="all",
                       help="screenplay-write|breakdown|screenplay（段0 编剧）|"
                            "assets|keyframes|camera|video|final|all（默认 all=五段按依赖序，"
                            "不含段0）")
    p_run.add_argument("--only", default="", help="只处理指定镜/unit，如 S1,S3")
    p_run.add_argument("--force", action="store_true", help="完成也重跑")
    p_run.add_argument("--dry-run", action="store_true",
                       help="只打印各段计划与决策，不渲染不触网不写段产物")
    p_run.add_argument("--premise", default="",
                       help="screenplay-write 输入：题材一句话（LLM 模式必填）")
    p_run.add_argument("--style-note", dest="style_note", default="",
                       help="screenplay-write 输入：风格自由文本（如 悬疑·雨夜·治愈；"
                            "不与 photo/anime 通道冲突，通道由 project.yaml 决定）")
    p_run.add_argument("--shots", type=int, default=0,
                       help="screenplay 输入：镜数预算（0=未指定，write 默认 8；"
                            "breakdown 优先取剧本预算行）")
    p_run.add_argument("--total-s", dest="total_s", type=float, default=0.0,
                       help="screenplay 输入：总时长预算（秒，0=未指定；"
                            "breakdown 优先取剧本预算行）")
    p_run.add_argument("--set", dest="sets", action="append", default=[], metavar="a.b=c")
    p_run.add_argument("--keep-stale", action="store_true", help="stale 保留旧产物不重跑（默认 stale 即重跑）")
    p_run.add_argument("--auto-pick", action="store_true",
                       help="assets 三视图自动取第一候选（无人值守；默认人工挑选关口）")
    p_run.add_argument("--abandon-running", action="store_true",
                       help="把遗留 running unit 强制置回 pending 重提（须确认服务器侧已无该 job）")
    p_run.add_argument("--trust-llm", action="store_true", help="camera 模式A：LLM 代码免人工审阅直接执行")
    p_run.add_argument("--wait-weights", action="store_true", help="video 段权重未就绪时轮询（默认立即失败）")
    p_run.set_defaults(func=cmd_run)

    p_st = sub.add_parser("status", help="各段 manifest 汇总表")
    p_st.add_argument("name")
    p_st.add_argument("--set", dest="sets", action="append", default=[], metavar="a.b=c")
    p_st.set_defaults(func=cmd_status)

    p_doc = sub.add_parser("doctor", help="环境详查（路径/权重/ffmpeg/Blender/服务器）")
    p_doc.add_argument("--with-server", action="store_true", help="显式探测服务器与节点清单（默认也探测，不可达仅提示）")
    p_doc.add_argument("--set", dest="sets", action="append", default=[], metavar="a.b=c")
    p_doc.set_defaults(func=cmd_doctor)

    p_rec = sub.add_parser("recover", help="按 prompt_id 回捞 history 补下载（§5）")
    p_rec.add_argument("name")
    p_rec.add_argument("--pid", default=None, help="只处理指定 prompt_id")
    p_rec.add_argument("--set", dest="sets", action="append", default=[], metavar="a.b=c")
    p_rec.set_defaults(func=cmd_recover)
    return ap


def main(argv=None) -> int:
    _setup_stdio()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or EXIT_OK)
    except KeyboardInterrupt:
        _say("中断（manifest 已记录进行中状态；重跑时孤儿收敛接管，§4.0）")
        return 130
    except (config_mod.ConfigError, schema.SchemaError, miniyaml.MiniYAMLError) as e:
        _say("错误：%s" % e)
        return EXIT_FAIL
