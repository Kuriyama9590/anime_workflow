# -*- coding: utf-8 -*-
"""camera 阶段 CLI：模式A（Blender 运镜）/ 模式B（词库直通 H3）。

用法（python -m pipeline.camera.run_camera 或 python pipeline/camera/run_camera.py）：

  # 模式A · 端点图（默认）：运镜起/止两帧 → S#_camfirst/camlast.png（送 H3 作首末帧）
  python -m pipeline.camera.run_camera modeA --shot S1 --image-first S1_first.png \
      [--image-last S1_last.png] --move push_in --outdir proj/camera \
      [--set strength=0.4] [--duration 5.17] [--fps 24] [--width 1344] [--height 768]

  # 模式A · 整段运镜视频：--background 渲 PNG 帧 → ffmpeg 合 S#.mp4（无音轨）
  python -m pipeline.camera.run_camera modeA ... --output animation

  # 模式A · 干跑自检（建场景不渲染）
  python -m pipeline.camera.run_camera modeA ... --verify-only

  # 模式A · LLM 生成 bpy（配置了才启用，失败自动回退预设模板；默认只落盘待审）
  python -m pipeline.camera.run_camera modeA ... --llm [--trust-llm]

  # 模式B · 词库运镜提示词（首尾帧直通 H3，零 Blender 依赖）
  python -m pipeline.camera.run_camera modeB --shot S1 --move push_in \
      --intensity slow [--extra "..."] --outdir proj/camera
  python -m pipeline.camera.run_camera modeB --shots projects/x/shots.yaml --outdir proj/x/camera

  python -m pipeline.camera.run_camera vocab                 # 打印词库
  python -m pipeline.camera.run_camera install [--version-line 4.2]  # 手动装 Blender

依赖：Blender 便携包（缺失自动安装，pipeline/blender.py）；ffmpeg（模式A animation；
PATH 或 --ffmpeg / PIPELINE_FFMPEG 覆盖）。全程仅标准库。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

# 保证 Windows 控制台中文输出不炸
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pipeline import blender                                    # noqa: E402
from pipeline.camera import codegen, llm_bpy, preset_moves, vocab  # noqa: E402


def log(msg: str) -> None:
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 参数解析

def _coerce_value(raw: str):
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    for conv in (int, float):
        try:
            return conv(raw)
        except ValueError:
            continue
    return raw


def _parse_sets(pairs: list[str]) -> dict:
    out = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit("--set 需要 k=v 形式，收到 %r" % item)
        k, v = item.split("=", 1)
        out[k] = _coerce_value(v)
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pipeline.camera.run_camera",
                                 description="camera 阶段：模式A Blender 运镜 / 模式B 词库直通 H3")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("modeA", help="Blender 运镜（codegen→headless 渲帧→ffmpeg）")
    a.add_argument("--shot", required=True, help="镜号（如 S1）")
    a.add_argument("--image-first", required=True, help="首帧关键帧 PNG")
    a.add_argument("--image-last", default=None, help="末帧关键帧 PNG（提供则端点图/整段做 first→last 过渡）")
    a.add_argument("--move", default="push_in", choices=list(preset_moves.MOVES))
    a.add_argument("--set", action="append", default=[], metavar="k=v", help="运镜参数（可重复，如 --set strength=0.4）")
    a.add_argument("--spec", default=None, help="直接加载 spec JSON（跳过预设参数化）")
    a.add_argument("--duration", type=float, default=5.17)
    a.add_argument("--fps", type=int, default=24)
    a.add_argument("--width", type=int, default=1344)
    a.add_argument("--height", type=int, default=768)
    a.add_argument("--samples", type=int, default=16)
    a.add_argument("--easing", default="smooth", choices=list(preset_moves.EASINGS))
    a.add_argument("--overscan", type=float, default=1.08)
    a.add_argument("--subdivisions", type=int, default=96)
    a.add_argument("--displacement", type=float, default=0.012)
    a.add_argument("--outdir", default="camera_out")
    a.add_argument("--output", choices=("endpoints", "animation"), default="endpoints",
                   help="endpoints=端点两帧（默认）；animation=整段 mp4")
    a.add_argument("--llm", action="store_true", help="优先 LLM 生成 bpy（未配置/失败回退预设）")
    a.add_argument("--trust-llm", action="store_true", help="LLM 代码免人工审阅直接执行（默认只落盘）")
    a.add_argument("--verify-only", action="store_true", help="只建场景自检，不渲染（自测用）")
    a.add_argument("--blender", default=None, help="blender.exe 显式路径")
    a.add_argument("--ffmpeg", default=None, help="ffmpeg 显式路径")
    a.add_argument("--timeout", type=int, default=600, help="Blender 单次执行超时秒")
    a.add_argument("--keep-bpy", action="store_true", help="结束后保留 .bpy（默认也保留；此参数占位以便未来清理模式）")

    b = sub.add_parser("modeB", help="词库运镜提示词（camera/S#_camdesc.txt）")
    b.add_argument("--shot", default=None, help="单镜镜号（与 --shots 二选一）")
    b.add_argument("--move", default="push_in", help="词库运镜名（vocab 子命令可查）")
    b.add_argument("--intensity", default=None, help="slow|medium|fast（默认词库 default_intensity）")
    b.add_argument("--extra", default="", help="自由补充短语（追加在短语后）")
    b.add_argument("--shots", default=None, help="shots.yaml 批量模式（单镜参数被忽略）")
    b.add_argument("--outdir", default="camera_out")

    sub.add_parser("vocab", help="打印运镜提示词词库")

    i = sub.add_parser("install", help="安装/校验便携 Blender")
    i.add_argument("--version-line", default=blender.DEFAULT_VERSION_LINE)
    i.add_argument("--install-dir", default=None)
    i.add_argument("--keep-zip", action="store_true")

    return ap


# ---------------------------------------------------------------- 模式A

def _find_ffmpeg(explicit: str | None = None) -> str:
    cand = explicit or os.environ.get("PIPELINE_FFMPEG") or shutil.which("ffmpeg")
    if not cand:
        raise RuntimeError("未找到 ffmpeg：装到 PATH 或用 --ffmpeg / PIPELINE_FFMPEG 指定")
    return cand


def _run_blender_script(bpy_path: str, mode_args: list[str], *, blender_exe: str | None,
                        timeout_s: int) -> tuple[int, str]:
    os.makedirs(os.path.dirname(os.path.abspath(bpy_path)), exist_ok=True)
    proc = blender.run(["--background", "--python", bpy_path, "--"] + mode_args,
                       blender_exe=blender_exe, timeout_s=timeout_s)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # 把结果标记行回显到本进程 stdout（自测/运维直接可见，含引擎枚举等自检信息）
    for line in (proc.stdout or "").splitlines():
        if any(m in line for m in (codegen.marker(), "ENDPOINTS_OK", "ANIMATION_OK")):
            print(line, flush=True)
    tail = "\n".join((proc.stdout or "").splitlines()[-8:])
    if proc.returncode != 0:
        log("blender 退出码 %d，尾部输出：\n%s" % (proc.returncode, tail))
    return proc.returncode, out


def run_mode_a(args: argparse.Namespace) -> int:
    t0 = time.time()
    os.makedirs(args.outdir, exist_ok=True)

    # 1) spec
    if args.spec:
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
        log("spec 载入：%s（move=%s）" % (args.spec, spec.get("move")))
    else:
        spec = preset_moves.make_spec(
            args.shot, args.move, args.image_first, args.image_last,
            params=_parse_sets(args.set), duration_s=args.duration, fps=args.fps,
            width=args.width, height=args.height, samples=args.samples,
            overscan=args.overscan, subdivisions=args.subdivisions,
            displacement_strength=args.displacement, easing=args.easing,
            out_dir=args.outdir)
        log("spec 构建完成：move=%s params=%s frames=%d-%d res=%dx%d@%dfps"
            % (args.move, spec["params"], spec["render"]["frame_start"],
               spec["render"]["frame_end"], args.width, args.height, args.fps))

    # 2) bpy 脚本：预设模板打底，LLM 成功则覆盖（失败/未配置回退预设，永不阻塞）
    bpy_path = os.path.join(args.outdir, "%s.bpy" % args.shot)
    script = codegen.generate_bpy(spec)
    source = "preset:%s" % args.move
    llm_meta = None
    if args.llm:
        try:
            cfg = llm_bpy.load_config()
        except llm_bpy.LLMError as e:
            cfg = None
            log("LLM 配置异常，回退预设：%s" % e)
        if cfg is None:
            log("LLM 未配置（enabled/base_url/model/api key 缺一），走预设模板")
        else:
            try:
                code, llm_meta = llm_bpy.generate_bpy_code(spec, cfg)
                violations = llm_bpy.safety_scan(code)
                if violations:
                    log("LLM 代码安全扫描未过（%s），回退预设" % "; ".join(violations[:3]))
                else:
                    script, source = code, "llm:%s" % cfg["model"]
                    log("LLM 生成成功（sha256=%s…）" % llm_meta["code_sha256"][:12])
            except llm_bpy.LLMError as e:
                log("LLM 生成失败，回退预设：%s" % e)
    with open(bpy_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)
    log("bpy 脚本落盘：%s（来源 %s）" % (bpy_path, source))

    # 3) LLM 代码默认只落盘不执行（require_review）
    if source.startswith("llm:") and llm_meta and llm_meta.get("require_review") \
            and not args.trust_llm:
        log("LLM 代码已落盘待人工审阅；确认后加 --trust-llm 执行（设计文档 §10 风险 6）")
        return 0

    # 4) Blender 场景构建/渲染
    exe = args.blender or blender.find_blender()
    if not exe:
        log("blender.exe 未找到，开始便携安装（免管理员，zipfile 解压）…")
        exe = blender.ensure_installed()
    log("blender：%s（%s）" % (exe, blender.get_version(exe) or "版本未知"))

    if args.verify_only:
        rc, out = _run_blender_script(bpy_path, ["--verify-only"],
                                      blender_exe=exe, timeout_s=args.timeout)
        ok = rc == 0 and codegen.marker() in out
        log("场景构建自检：%s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    if args.output == "endpoints":
        rc, out = _run_blender_script(bpy_path, ["--render=endpoints"],
                                      blender_exe=exe, timeout_s=args.timeout)
        if rc != 0 or "ENDPOINTS_OK" not in out:
            return 1
        bad = [p for p in (os.path.join(args.outdir, n) for n in spec["output"]["endpoints"])
               if not (os.path.isfile(p) and os.path.getsize(p) >= 10_000)]
        if bad:
            log("端点图缺失或过小（<10KB）：%s" % bad)
            return 1
        log("端点图完成：%s（%.1fs）" % (
            [os.path.basename(p) for p in spec["output"]["endpoints"]], time.time() - t0))
        return 0

    # animation：PNG 序列 → ffmpeg → mp4
    rc, out = _run_blender_script(bpy_path, ["--render=animation"],
                                  blender_exe=exe, timeout_s=args.timeout)
    if rc != 0 or "ANIMATION_OK" not in out:
        return 1
    prefix = spec["output"]["frame_prefix"]
    pattern = os.path.join(args.outdir, prefix + "_*.png")
    frames = sorted(glob.glob(pattern))
    if len(frames) < 2:
        log("渲染帧不足：%d 张（%s）" % (len(frames), pattern))
        return 1
    mp4 = os.path.join(args.outdir, "%s.mp4" % args.shot)
    ff = _find_ffmpeg(args.ffmpeg)
    cmd = [ff, "-y", "-framerate", str(spec["render"]["fps"]),
           "-i", os.path.join(args.outdir, prefix + "_%04d.png"),
           "-c:v", "libx264", "-crf", "18", "-preset", "medium",
           "-pix_fmt", "yuv420p", mp4]
    log("ffmpeg 合成：%d 帧 → %s" % (len(frames), mp4))
    proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        log("ffmpeg 失败：\n%s" % (proc.stderr or "")[-1500:])
        return 1
    size = os.path.getsize(mp4) if os.path.isfile(mp4) else 0
    if size < 20_000:
        log("mp4 过小（%d 字节 < 20KB），疑似空渲染" % size)
        return 1
    log("运镜视频完成：%s（%d 字节，%.1fs）" % (mp4, size, time.time() - t0))
    return 0


# ---------------------------------------------------------------- 模式B

def run_mode_b(args: argparse.Namespace) -> int:
    if args.shots:
        results = vocab.camdesc_from_shots(args.shots, args.outdir, log=log)
        log("模式B 批量完成：%d 镜 camdesc" % len(results))
        return 0 if results else 1
    if not args.shot:
        print("modeB 需要 --shot 或 --shots", file=sys.stderr)
        return 2
    v = vocab.load_vocab()
    intensity = args.intensity or v.get("default_intensity", "medium")
    text = vocab.build_camdesc(args.move, intensity, args.extra, v)
    os.makedirs(args.outdir, exist_ok=True)
    path = os.path.join(args.outdir, "%s_camdesc.txt" % args.shot)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    log("%s -> %s" % (path, text))
    return 0


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "modeA":
        return run_mode_a(args)
    if args.cmd == "modeB":
        return run_mode_b(args)
    if args.cmd == "vocab":
        v = vocab.load_vocab()
        for move, entries in sorted(v["moves"].items()):
            for inten, text in sorted(entries.items()):
                print("%-10s %-7s %s" % (move, inten, text))
        return 0
    if args.cmd == "install":
        exe = blender.install(version_line=args.version_line,
                              install_dir=args.install_dir,
                              keep_zip=args.keep_zip, log=log)
        log("blender 就绪：%s（%s）" % (exe, blender.get_version(exe)))
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, ValueError, KeyError, RuntimeError,
            AttributeError, TypeError) as e:
        log("错误：%s" % e)
        sys.exit(1)
