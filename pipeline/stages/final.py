# -*- coding: utf-8 -*-
"""final 段（段5）执行器 —— 把各镜 mp4 合成成片：_tools/build_final.py 的泛化版。

设计契约 docs/FRAMEWORK_DESIGN.md §4.5（B 级：build_final.py 从未在本机跑完，
参数照抄其结构，全部待首跑标定）。次序固定——**补轨必须前置于 concat**（concat
demuxer 面对有声/无声混合输入会失败或时长错乱）：

  1. 逐镜源解析：按 shots.yaml 镜序定位 mp4 —— 模式A full_video 镜（camera.output
     声明或 camera/S#.mp4 在盘）取 camera/S#.mp4，其余取 video/S#.mp4；缺镜/小于
     video.min_bytes 告警剔除后继续（build_final.py:32-35 同策略）。
  2. 逐镜音频规整（采样率/声道统一到"首个有声镜实测规格"基准，复审 #2：混合采样率
     经 concat 会静默产出时长错乱的成片，24k mono+44.1k stereo 各 2s 实测 4s→7.43s）：
     无声镜补 anullsrc（r/cl 按基准）或与 assets/ambience.mp3 床轨"该镜全局偏移段 +
     静音垫底"混音（床轨先 aresample/aformat 到基准再 amix）；有声镜规格不符基准时
     音频重编码规整（-ar/-ac，视频直拷）；全无声镜回退 44.1k/stereo。
  3. concat 转码：x264 crf/preset + aac bitrate 统一参数（build_final.py:42-43）。
  4. ffprobe 逐镜时长 → 字幕全局时间轴偏移（build_final.py:45-48 算法）。
  5. cues → SRT → subtitles 滤镜烧录（force_style 用 config final.sub_style）+
     可选 drawtext 标题卡（alpha 渐入渐出，build_final.py:69-70 模板）+ 片头 fade-in
     （build_final.py:72）。烧录命令以 cwd=<out_dir> 执行、滤镜内只出现 ASCII 相对名
     （.tmp/_burn.srt / .tmp/_title.txt）：自由文本经 drawtext textfile= 传入——
     '…' 引用段内撇号/逗号/冒号无可靠转义（复审 #1 实测 ' 形式直接 Filter not
     found），font/fontsdir 仍为内联绝对路径并守卫撇号（filter_path 只转义盘符冒号，
     build_final.py:71 前车之鉴）。
  6. contact sheet：ffmpeg scale+tile 生成每镜首末帧对照表（替代 PIL 版
     _tools/contact_sheet.py——框架全程仅标准库）。帧来源与成片一致（§4.5）：
     模式B镜=keyframes S#_first/last.png；模式A endpoints=camera S#_camfirst/camlast.png
     （实际送入 H3 的端点）；模式A full_video=运镜视频首/末帧各抽一帧；
     缺帧画格=纯色+drawtext 标 MISSING（contact_sheet.py:20-24 语义）。

cue 双来源（合并、按全局起始排序；t_in/t_out 一律为镜内秒，final 按各镜偏移换算）：
  - shots.yaml 每镜 ``cues: [{t_in, t_out, text}]``（任务书口径；schema 对未知键宽容）；
  - subtitles.yaml ``cues: [{shot, t_in, t_out, text}]``（schema._check_subtitles 规范形）。
片头/片尾卡：subtitles.yaml ``title_card: {text, t_in, t_out, fontsize}``（drawtext）
与 ``end_card: {text, tail_s}``（scaffold 模板形；lead_in=tail_s、lead_out=min(0.6,
tail_s*0.2)，即 build_final.py:62 的 total-4.4..total-0.6 节奏泛化；显式 lead_in/
lead_out 亦可）。CLI --title/--end-card 可覆盖启用。

产物（§4.5）：<项目>/final/<项目名>.mp4、<项目名>.srt、contact_sheet.png +
final_manifest.json（总时长、各镜偏移、命令日志、上游指纹，§5 schema，unit=assemble）。
断点续跑：done ∧ 产物在 ∧ 大小达标 ∧ inputs_hash/upstream/段配置哈希未变 → 跳过；
stale 默认重跑（--keep-stale 保留旧片）；--force 强制。影响产出的段选项
（--title/--end-card/--no-burn/--no-contact-sheet/--no-ambience/--out-dir）一并进
inputs_hash（复审 #3）：任一变化即 stale 重跑，绝不静默沿用旧成片。

入口（stages 包既有约定 ``run_stage(project_dir, config, args) -> int``，
cli/invoke_stage 走此路；本模块另带直连 CLI）：
  python -m pipeline.stages.final --project projects/我的剧 --dry-run
  python -m pipeline.stages.final --project projects/我的剧 [--only S1,S3] [--force]
      [--no-burn] [--no-contact-sheet] [--no-ambience] [--title 文本] [--end-card 文本]
      [--out-dir output/final] [--set final.crf=20]

退出码：0=成功/全部跳过；1=输入错误或合成失败。final 段 needs_comfy=false（§4.0）。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import config as config_mod
from .. import ffmpeg_tool, manifest as manifest_mod, schema as schema_mod
from ..schema import SchemaError

STAGE = "final"
NEEDS_COMFY = False
UNIT = "assemble"                      # 全片合成不可逐镜分割（本包骨架 plan 既有约定）
SHOT_ID_RE = re.compile(r"^S(\d+)$")


class ConfigError(RuntimeError):
    """配置/输入结构错误（shots/subtitles/config）。"""


# --------------------------------------------------------------------------- #
# 段配置：§2.1 final 节键为事实源；扩展键给默认值，可经 project overrides /
# --set final.<key>=<v> 覆盖（深合并原样进入 cfg["final"]）
# --------------------------------------------------------------------------- #
FINAL_DEFAULTS = {
    # §2.1 final 节原样键（pipeline/config.yaml 同值）
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "crf": 18,
    "preset": "medium",
    "audio_codec": "aac",
    "audio_bitrate": "192k",
    "font": "C:/Windows/Fonts/msyh.ttc",
    "sub_style": ("FontName=Microsoft YaHei&FontSize=16&PrimaryColour=&H00F5F5F5"
                  "&OutlineColour=&H80000000&BorderStyle=1&Outline=1&Shadow=0"
                  "&Alignment=2&MarginV=34"),
    # 本段扩展键
    "fade_in_s": 0.4,              # build_final.py:72 fade=t=in:st=0:d=0.4
    "sheet_thumb_width": 448,      # contact_sheet.py:8 的 1344x768/3
    "sheet_thumb_height": 256,
    "sheet_margin": 6,
    "sheet_padding": 4,
    "min_output_bytes": 200_000,   # 成片完成判定大小门（§5 视频下限）
    "charenc": "UTF-8",
    "out_dir": "final",            # 设计 §1/§4.5；要 output/final 布局改此键或 --out-dir
    "video_dir": "video",
    "ambience_file": "assets/ambience.mp3",
    "keyframes_dirs": ["keyframes"],   # contact sheet 帧源目录（项目内相对，按序回退）
}


class FinalOpts:
    """StageOpts（cli 统一载体）+ 本段专有选项的宽容归一。"""

    def __init__(self, only=(), force=False, dry_run=False, keep_stale=False,
                 burn=True, contact_sheet=True, use_ambience=True,
                 title=None, end_card=None, out_dir=None):
        if isinstance(only, str):
            only = tuple(x.strip() for x in only.split(",") if x.strip())
        self.only = tuple(only)
        self.force = bool(force)
        self.dry_run = bool(dry_run)
        self.keep_stale = bool(keep_stale)
        self.burn = bool(burn)
        self.contact_sheet = bool(contact_sheet)
        self.use_ambience = bool(use_ambience)
        self.title = title
        self.end_card = end_card
        self.out_dir = out_dir


def _coerce_args(args) -> FinalOpts:
    """StageOpts / argparse.Namespace / dict / None → FinalOpts（缺省容忍）。"""
    if args is None or isinstance(args, FinalOpts):
        return args if isinstance(args, FinalOpts) else FinalOpts()
    get = args.get if isinstance(args, dict) else lambda k, d=None: getattr(args, k, d)
    kwargs = {k: get(k) for k in
              ("only", "force", "dry_run", "keep_stale", "burn", "contact_sheet",
               "use_ambience", "title", "end_card", "out_dir") if get(k) is not None}
    return FinalOpts(**kwargs)


def _num_of(cfg: dict, key: str, problems: list):
    """数值键宽容转换：非法值记 problems 并回退 None（复审 #5：不得裸抛 ValueError）。"""
    try:
        return float(cfg[key])
    except (TypeError, ValueError):
        problems.append("final.%s 应为数值（得到 %r）" % (key, cfg[key]))
        return None


def _stage_cfg(merged: dict) -> dict:
    """合并配置（§2.1 结构）→ 本段扁平配置（final 节覆盖默认值）+ 数值校验。"""
    cfg = copy.deepcopy(FINAL_DEFAULTS)
    node = merged.get("final")
    if isinstance(node, dict):
        cfg.update(dict(node))
    problems = []
    if not isinstance(cfg["crf"], int) or isinstance(cfg["crf"], bool) \
            or not (0 <= cfg["crf"] <= 51):
        problems.append("final.crf 应为 0-51 的整数（得到 %r）" % cfg["crf"])
    if not str(cfg["preset"]).strip() or not str(cfg["audio_bitrate"]).strip():
        problems.append("final.preset / final.audio_bitrate 不能为空")
    for key in ("fade_in_s", "sheet_thumb_width", "sheet_thumb_height",
                "min_output_bytes", "sheet_margin", "sheet_padding"):
        v = _num_of(cfg, key, problems)
        if v is not None and v < 0:
            problems.append("final.%s 应为非负（得到 %r）" % (key, cfg[key]))
    for key in ("sheet_thumb_width", "sheet_thumb_height"):
        v = _num_of(cfg, key, problems)
        if v is not None and v <= 0:
            problems.append("final.%s 应为正数（得到 %r）" % (key, cfg[key]))
    if not isinstance(cfg["keyframes_dirs"], list) or not cfg["keyframes_dirs"]:
        problems.append("final.keyframes_dirs 应为非空列表")
    if "'" in str(cfg["font"]):
        problems.append("final.font 路径含单引号（ffmpeg 滤镜引用段内无法表达）：%s"
                        % cfg["font"])
    if problems:
        raise ConfigError("final 段配置校验失败：" + "；".join(problems))
    # 数值键统一 int 化（命令行/字符串来源的合法数值在此收敛）
    for key in ("sheet_thumb_width", "sheet_thumb_height", "min_output_bytes",
                "sheet_margin", "sheet_padding"):
        try:
            cfg[key] = int(float(cfg[key]))
        except (TypeError, ValueError):
            pass
    return cfg


def _resolve_project_dir(raw, cfg: dict) -> Path:
    """绝对路径原样；相对名按 paths.projects_root（仓库根锚定）解析；再退 cwd。"""
    p = Path(str(raw))
    if p.is_absolute() and p.is_dir():
        return p
    root = config_mod.resolve_path((cfg.get("paths") or {}).get("projects_root", "projects"))
    for cand in (root / str(raw), Path(os.getcwd()) / str(raw)):
        if cand.is_dir():
            return cand.resolve()
    raise ConfigError("项目目录不存在：%s（尝试 %s 与 cwd 下同名）" % (raw, root / str(raw)))


class StageLog:
    """logs/<项目>_final.log 追加（§5）；dry-run 只打印不落盘；顺带收集命令日志。"""

    def __init__(self, path: Path, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled
        self.commands: list = []

    def __call__(self, msg: str) -> None:
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


# --------------------------------------------------------------------------- #
# cue 收集与 SRT（build_final.py:45-48,50-64 算法泛化）
# --------------------------------------------------------------------------- #
def srt_ts(t: float) -> str:
    """SRT 时间戳。build_final.py:26 用 %06.3f（点分）；这里用 SRT 规范的逗号毫秒
    （libass/ffmpeg 两者都接受，逗号为标准播放器兼容形；ms 取整避免 59.9997→60.000）。"""
    ms = max(0, int(round(float(t) * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms2 = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms2)


def _cue_list(node, where: str) -> list:
    """规范化 cue 列表 → [{shot|None, t_in, t_out, text}]；结构错误给可读 ConfigError。"""
    if not node:
        return []
    if not isinstance(node, list):
        raise ConfigError("%s 须为列表（每项 {t_in, t_out, text[, shot]}）" % where)
    out = []
    for i, c in enumerate(node):
        if not isinstance(c, dict):
            raise ConfigError("%s[%d] 须为映射" % (where, i))
        try:
            t_in, t_out = float(c["t_in"]), float(c["t_out"])
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError("%s[%d] 缺少可数值化的 t_in/t_out：%r（%s）"
                              % (where, i, c, e)) from e
        text = str(c.get("text") or "").strip()
        if not text:
            raise ConfigError("%s[%d] 缺少 text" % (where, i))
        if t_out <= t_in:
            raise ConfigError("%s[%d] t_out（%s）须晚于 t_in（%s）" % (where, i, t_out, t_in))
        shot = c.get("shot")
        if shot is not None and not SHOT_ID_RE.match(str(shot)):
            raise ConfigError("%s[%d].shot 须形如 S1，得到 %r" % (where, i, shot))
        out.append({"shot": str(shot) if shot else None, "t_in": t_in,
                    "t_out": t_out, "text": text})
    return out


def collect_cues(shots: list, subs: dict, offsets: dict, total: float,
                 end_text: str | None, log) -> list:
    """合并 shots.yaml 每镜 cues（镜内秒 + 该镜偏移）与 subtitles.yaml cues（shot 锚定；
    无 shot = 全局绝对时间的宽容形），外加 end_card 片尾 cue；按全局起始排序。"""
    cues = []
    for shot in shots:
        sid = shot.get("id")
        if sid not in offsets:
            continue                     # 缺镜/--only 剔除：cue 一并丢弃
        off = offsets[sid]
        for c in _cue_list(shot.get("cues"), "shots.%s.cues" % sid):
            cues.append({"start": off + c["t_in"], "end": off + c["t_out"],
                         "text": c["text"], "src": "shots.yaml %s" % sid})
    for c in _cue_list(subs.get("cues"), "subtitles.yaml cues"):
        if c["shot"]:
            if c["shot"] not in offsets:
                log("[告警] 丢弃未入片镜的字幕（%s → %s）" % (c["text"][:14], c["shot"]))
                continue
            off = offsets[c["shot"]]
            cues.append({"start": off + c["t_in"], "end": off + c["t_out"],
                         "text": c["text"], "src": "subtitles.yaml %s" % c["shot"]})
        else:
            cues.append({"start": c["t_in"], "end": c["t_out"],
                         "text": c["text"], "src": "subtitles.yaml 全局"})
    card = subs.get("end_card") if isinstance(subs.get("end_card"), dict) else {}
    text = end_text if end_text is not None else str(card.get("text") or "").strip()
    if text:
        # scaffold 形 {text, tail_s}；显式 lead_in/lead_out 优先。参考节奏：
        # build_final.py:62 的 total-4.4..total-0.6（尾端 0.6s 让位黑场/收尾）
        if "lead_in" in card or "lead_out" in card:
            lead_in = float(card.get("lead_in", 4.4))
            lead_out = float(card.get("lead_out", 0.6))
        else:
            lead_in = float(card.get("tail_s", 4.4))
            lead_out = min(0.6, lead_in * 0.2)
        cues.append({"start": total - lead_in, "end": total - lead_out,
                     "text": text, "src": "end_card"})
    cues.sort(key=lambda c: (c["start"], c["end"]))
    return cues


def resolve_title_card(subs: dict, title_text: str | None, total: float) -> dict | None:
    """标题卡（drawtext）：subtitles.yaml title_card 节 + CLI --title 覆盖/启用。
    默认 0.9-3.0s、fontsize 58（build_final.py:56,69）。"""
    card = dict(subs.get("title_card")) if isinstance(subs.get("title_card"), dict) else {}
    if title_text is not None:
        card["text"] = title_text
    text = str(card.get("text") or "").strip()
    if not text:
        return None
    t_in = float(card.get("t_in", 0.9))
    t_out = float(card.get("t_out", 3.0))
    if t_out <= t_in:
        raise ConfigError("title_card.t_out（%s）须晚于 t_in（%s）" % (t_out, t_in))
    try:
        fontsize = int(card.get("fontsize", 58) or 58)
    except (TypeError, ValueError):
        raise ConfigError("title_card.fontsize 应为整数（得到 %r）" % card.get("fontsize"))
    note = "标题卡 t_in=%.2fs 超出成片 %.2fs，将不可见" % (t_in, total) if t_in >= total else ""
    return {"text": text, "t_in": t_in, "t_out": min(t_out, total),
            "fontsize": fontsize, "note": note}


def build_srt(cues: list, total: float, log=None) -> tuple[str, int]:
    """cues → SRT 文本（钳制 [0,total]；越界/零长丢弃并告警；build_final.py:50-65 泛化）。"""
    blocks, skipped = [], []
    for c in cues:
        s, e = max(0.0, c["start"]), min(float(total), c["end"])
        if e - s <= 0.02 or s >= total or e <= 0:
            skipped.append(c["text"][:16])
            continue
        blocks.append((s, e, c["text"]))
    if log:
        for t in skipped:
            log("[告警] 丢弃成片时长外/零长的字幕：%s" % t)
    lines = ["%d\n%s --> %s\n%s" % (i, srt_ts(s), srt_ts(e), text)
             for i, (s, e, text) in enumerate(blocks, 1)]
    return ("\n\n".join(lines) + "\n" if lines else ""), len(blocks)


# --------------------------------------------------------------------------- #
# ffmpeg 命令构造（纯函数，dry-run 与真实运行共用；build_final.py 参数为骨架）
# --------------------------------------------------------------------------- #
def _n(x) -> str:
    """滤镜表达式数字：最多 3 位小数、去尾零（build_final.py:70 字面量风格）。"""
    s = ("%.3f" % float(x)).rstrip("0").rstrip(".")
    return s or "0"


_LABEL_SAFE_RE = re.compile(r"^[A-Za-z0-9 _.\-]*$")


def _label_text(text: str) -> str:
    """滤镜内联文本（drawtext 标签等）只接受无特殊字符的 ASCII——' : , ; % \\ 任一
    出现即 ConfigError（复审实测：'…' 引用段内 \\' 无法表达撇号，内联文本不可靠；
    自由文本一律走 textfile=，见 title_drawtext_filter）。"""
    s = str(text)
    if not _LABEL_SAFE_RE.match(s):
        raise ConfigError("滤镜内联文本含特殊字符，请改走 textfile 通道：%r" % s)
    return s


def _assert_filter_path_safe(path) -> str:
    """滤镜内联路径（字体/字体目录）守卫：含 ' 即拒绝（引用段内无法表达；复审 #1）。
    项目内路径（srt/标题文本）不走内联——用 cwd + ASCII 相对名（.tmp/_burn.srt 等），
    从根上绕开转义问题。"""
    s = str(path)
    if "'" in s:
        raise ConfigError("滤镜内联路径含单引号（ffmpeg 引用段内无法表达）：%s ——"
                          "请改路径名，或对该文件改用 cwd+相对名通道" % s)
    return s


def layout_for(channels: int) -> str:
    return {1: "mono", 2: "stereo"}.get(int(channels), "stereo")


def build_concat_cmd(cfg: dict, list_file, out_file) -> list:
    """concat 转码（build_final.py:42-43：x264 crf + aac bitrate，统一参数避免流不一致）。"""
    return ["-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:v", "libx264", "-crf", str(cfg["crf"]), "-preset", str(cfg["preset"]),
            "-c:a", str(cfg["audio_codec"]), "-b:a", str(cfg["audio_bitrate"]),
            str(out_file)]


def build_silence_cmd(cfg: dict, src, dst, duration: float, rate: int, channels: int) -> list:
    """无声镜补 anullsrc 静音轨（§4.5 步骤 1；视频流直拷不二压）。

    采样率/声道取自首个有声镜的实测规格（复审 #2：硬编码 44.1k/stereo 与 H3 音频
    混流会造成 concat 时长静默错乱——24k 单声道与 44.1k 立体声混合实测 4s→7.43s）。"""
    d = _n(duration)
    lay = layout_for(channels)
    return ["-y", "-i", str(src),
            "-f", "lavfi", "-t", d, "-i", "anullsrc=r=%d:cl=%s" % (int(rate), lay),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", str(cfg["audio_codec"]),
            "-b:a", str(cfg["audio_bitrate"]), "-ar", str(int(rate)),
            "-ac", str(int(channels)), "-shortest", str(dst)]


def build_audio_norm_cmd(cfg: dict, src, dst, rate: int, channels: int) -> list:
    """有声镜规格规整（复审 #2）：采样率/声道 ≠ 全片基准时音频重编码到基准
    （-ar/-ac 由 aac 编码器完成重采样/混音），视频流直拷。"""
    return ["-y", "-i", str(src),
            "-c:v", "copy", "-c:a", str(cfg["audio_codec"]),
            "-b:a", str(cfg["audio_bitrate"]), "-ar", str(int(rate)),
            "-ac", str(int(channels)), str(dst)]


def build_ambience_cmd(cfg: dict, src, amb, dst, duration: float, offset: float,
                       rate: int, channels: int) -> list:
    """无声镜与 ambience 混音：取床轨全局偏移段（-ss offset -t dur，输入选项随其 -i），
    先 aresample/aformat 规整到全片基准（amix 要求各输入同采样率同声道），
    apad 垫底 + anullsrc 兜底，amix 后截齐该镜时长。"""
    d = _n(duration)
    lay = layout_for(channels)
    fc = ("[1:a]aresample={r},aformat=channel_layouts={l},"
          "apad=whole_dur={d},atrim=0:{d},asetpts=N/SR/TB[amb];"
          "[amb][2:a]amix=inputs=2:duration=longest:dropout_transition=0,"
          "atrim=0:{d}[aout]").format(d=d, r=int(rate), l=lay)
    return ["-y", "-i", str(src),
            "-ss", _n(offset), "-t", d, "-i", str(amb),
            "-f", "lavfi", "-t", d, "-i", "anullsrc=r=%d:cl=%s" % (int(rate), lay),
            "-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", str(cfg["audio_codec"]),
            "-b:a", str(cfg["audio_bitrate"]), "-ar", str(int(rate)),
            "-ac", str(int(channels)), str(dst)]


def title_drawtext_filter(cfg: dict, card: dict) -> str:
    """标题卡 drawtext（build_final.py:69-70 alpha 渐入渐出模板的参数化泛化）。

    文本经 textfile= 传入（card['textfile']=相对 out_dir 的 ASCII 路径，ffmpeg 以
    cwd=out_dir 执行）：自由文本（撇号/逗号/冒号/%/反斜杠）在 filtergraph 引用段内
    无可靠转义（复审 #1 实测），textfile 从根上绕开；alpha 表达式内无用户文本。"""
    t0, t1 = float(card["t_in"]), float(card["t_out"])
    dur = max(0.2, t1 - t0)
    fade = min(0.6, dur / 2.0)          # 参考实现 0.6s 渐变；短卡自动收窄防表达式自相矛盾
    f1, f2 = t0 + fade, t1 - fade
    alpha = ("if(lt(t,{t0}),0,if(lt(t,{f1}),(t-{t0})/{fd},"
             "if(lt(t,{f2}),1,if(lt(t,{t1}),({t1}-t)/{fd},0))))"
             ).format(t0=_n(t0), f1=_n(f1), f2=_n(f2), t1=_n(t1), fd=_n(fade))
    return ("drawtext=fontfile='%s':textfile='%s':fontsize=%s:fontcolor=white"
            ":borderw=2:bordercolor=black@0.5:x=(w-text_w)/2:y=(h-text_h)/2"
            ":expansion=none:alpha='%s'"
            % (ffmpeg_tool.filter_path(_assert_filter_path_safe(cfg["font"])),
               ffmpeg_tool.filter_path(card["textfile"]), card["fontsize"], alpha))


def subtitles_filter(cfg: dict, srt_path) -> str:
    """subtitles 烧录滤镜（build_final.py:67-71：force_style；fontsdir 辅助 libass
    定位 YaHei）。srt_path 用相对 out_dir 的 ASCII 名（.tmp/_burn.srt，ffmpeg 以
    cwd=out_dir 执行）——项目路径含 ' 时内联绝对路径无可靠转义（复审 #1 实测）。"""
    parts = ["subtitles=filename='%s'" % ffmpeg_tool.filter_path(str(srt_path))]
    if cfg.get("charenc"):
        parts.append("charenc=%s" % cfg["charenc"])
    font_dir = os.path.dirname(str(cfg["font"]))
    if font_dir:
        parts.append("fontsdir='%s'"
                     % ffmpeg_tool.filter_path(_assert_filter_path_safe(font_dir)))
    parts.append("force_style='%s'" % cfg["sub_style"])
    return ":".join(parts)


def build_vf(cfg: dict, srt_path, card: dict | None) -> str:
    """-vf 链，次序同 build_final.py:72：标题 drawtext（textfile）→ subtitles → 片头 fade。"""
    parts = []
    if card:
        parts.append(title_drawtext_filter(cfg, card))
    if srt_path:
        parts.append(subtitles_filter(cfg, srt_path))
    if parts and float(cfg["fade_in_s"]) > 0:
        parts.append("fade=t=in:st=0:d=%s" % _n(cfg["fade_in_s"]))
    return ",".join(parts)


def build_burn_cmd(cfg: dict, merged, vf: str, out_file) -> list:
    """字幕/标题烧录（build_final.py:73-74：视频重编码、音频直拷）。"""
    return ["-y", "-i", str(merged), "-vf", vf,
            "-c:v", "libx264", "-crf", str(cfg["crf"]), "-preset", str(cfg["preset"]),
            "-c:a", "copy", str(out_file)]


# --------------------------------------------------------------------------- #
# contact sheet（§4.5 步骤 6：ffmpeg scale+tile；帧来源与成片一致）
# --------------------------------------------------------------------------- #
def plan_contact_sheet(cfg: dict, cells: list, tmp_dir) -> dict:
    """cells = [(标签, 类型, 路径|None)]，类型 image|video|missing，每镜 first/last 各一格。
    返回 {extract_cmds, inputs, filter_complex}——纯构造不执行，dry-run 共用。

    每格 scale→drawtext 标签（missing = color 源 + 红字居中，contact_sheet.py:20-24）
    → concat 拼成 2N 帧流 → tile=2xN（margin/padding 留缝）→ 单帧 PNG。
    """
    w, h = int(cfg["sheet_thumb_width"]), int(cfg["sheet_thumb_height"])
    font = ffmpeg_tool.filter_path(cfg["font"])
    label_tpl = ("drawtext=fontfile='%s':fontsize=18:fontcolor=0xFFE678:x=8:y=6"
                 ":box=1:boxcolor=black@0.55:boxborderw=4:expansion=none:text='%s'"
                 % (font, "%s"))
    missing_tpl = ("drawtext=fontfile='%s':fontsize=22:fontcolor=0xFF6060"
                   ":x=(w-text_w)/2:y=(h-text_h)/2:expansion=none:text='%s'" % (font, "%s"))
    extract_cmds, inputs, chains = [], [], []
    n_in = 0                                    # 输入序号（image=2 元素、lavfi=4 元素，勿用 len 推）
    for idx, (label, kind, path) in enumerate(cells):
        cid = "c%d" % idx
        if kind == "video":            # full_video 镜：抽该镜视频首/末帧（§4.5 帧来源）
            frame_png = os.path.join(str(tmp_dir), "%s_frame.png"
                                     % re.sub(r"\W+", "_", label))
            if label.endswith("last"):
                extract_cmds.append(["-y", "-sseof", "-0.1", "-i", str(path),
                                     "-update", "1", "-frames:v", "1", frame_png])
            else:
                extract_cmds.append(["-y", "-i", str(path), "-update", "1",
                                     "-frames:v", "1", frame_png])
            kind, path = "image", frame_png
        if kind == "image":
            inputs.extend(["-i", str(path)])
            chains.append("[%d:v]scale=%d:%d,setsar=1,format=rgb24,%s[%s]"
                          % (n_in, w, h, label_tpl % _label_text(label), cid))
        else:                          # missing：纯色画格 + MISSING 标记
            inputs.extend(["-f", "lavfi", "-i", "color=c=0x18181E:s=%dx%d:d=0.04" % (w, h)])
            chains.append("[%d:v]trim=end_frame=1,setpts=PTS-STARTPTS,setsar=1,"
                          "format=rgb24,%s[%s]"
                          % (n_in, missing_tpl % _label_text("MISSING " + label), cid))
        n_in += 1
    n = len(cells)
    rows = max(1, (n + 1) // 2)
    concat_in = "".join("[c%d]" % i for i in range(n))
    tile = ("tile=2x%d:margin=%d:padding=%d:color=0x0C0C10"
            % (rows, int(cfg["sheet_margin"]), int(cfg["sheet_padding"])))
    fc = "%s;%sconcat=n=%d:v=1:a=0[cells];[cells]%s[sheet]" % (
        ";".join(chains), concat_in, n, tile)
    return {"extract_cmds": extract_cmds, "inputs": inputs, "filter_complex": fc}


def build_sheet_cmd(inputs: list, filter_complex: str, out_file) -> list:
    return ["-y"] + [str(x) for x in inputs] + [
        "-filter_complex", filter_complex, "-map", "[sheet]",
        "-frames:v", "1", str(out_file)]


# --------------------------------------------------------------------------- #
# 源解析（每镜 mp4 与 contact sheet 帧来源；缺镜告警剔除后继续，build_final.py:32-35）
# --------------------------------------------------------------------------- #
def resolve_shot_source(project_dir: Path, cfg: dict, shot: dict, shots_doc: dict) -> dict:
    """返回 {sid, src|None, kind, reason, full, sheet_first, sheet_last}；
    sheet_* = (类型, 路径|None)，类型 image|video|missing。"""
    sid = shot.get("id")
    vmp4 = project_dir / cfg["video_dir"] / ("%s.mp4" % sid)
    cmp4 = project_dir / "camera" / ("%s.mp4" % sid)
    eff = schema_mod.eff_camera(shot, shots_doc)
    full = (str(eff.get("output") or "endpoints") == "full_video") or cmp4.is_file()
    src, kind, reason = None, "", ""
    for k, p in (("camera", cmp4), ("video", vmp4)) if full \
            else (("video", vmp4), ("camera", cmp4)):
        if p.is_file():
            size = p.stat().st_size
            if size >= cfg["min_bytes"]:
                src, kind = p, k
                break
            reason = "%s 过小（%dB < %dB 下限）" % (p, size, cfg["min_bytes"])
    if src is None and not reason:
        reason = "video/%s.mp4 与 camera/%s.mp4 均不存在" % (sid, sid)

    def pick(cands):
        for p in cands:
            if p.is_file():
                return "image", p
        return "missing", None

    if full and src is not None:
        sheet_first = sheet_last = ("video", src)   # 抽运镜视频首/末帧（§4.5）
    else:
        # camfirst/camlast 只在模式A endpoints 镜存在 → 优先序自选择地满足两条规则：
        # 模式A endpoints = camera 端点；模式B = keyframes 首/末帧
        kf_dirs = [project_dir / d for d in cfg["keyframes_dirs"]]
        sheet_first = pick([project_dir / "camera" / ("%s_camfirst.png" % sid)]
                           + [d / ("%s_first.png" % sid) for d in kf_dirs])
        sheet_last = pick([project_dir / "camera" / ("%s_camlast.png" % sid)]
                          + [d / ("%s_last.png" % sid) for d in kf_dirs])
    return {"sid": sid, "src": src, "kind": kind, "reason": reason, "full": full,
            "sheet_first": sheet_first, "sheet_last": sheet_last}


# --------------------------------------------------------------------------- #
# 探测与执行
# --------------------------------------------------------------------------- #
def _probe(fftool: ffmpeg_tool.FFmpegTool, src) -> tuple:
    """(时长秒, 有声轨, 采样率, 声道数)——ffprobe 只读探测（复审 #2：采样率/声道
    决定补轨 anullsrc 规格与全片音频基准）。"""
    dur = fftool.duration_of(str(src))
    r = subprocess.run([fftool.ffprobe, "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=index", "-of", "csv=p=0", str(src)],
                       capture_output=True, encoding="utf-8", errors="replace")
    has_audio = bool((r.stdout or "").strip())
    rate = ch = None
    if has_audio:
        j = subprocess.run([fftool.ffprobe, "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=sample_rate,channels",
                            "-of", "json", str(src)],
                           capture_output=True, encoding="utf-8", errors="replace")
        try:
            st = (json.loads(j.stdout or "{}").get("streams") or [{}])[0]
            rate = int(st.get("sample_rate") or 0) or None
            ch = int(st.get("channels") or 0) or None
        except (ValueError, TypeError, IndexError):
            rate = ch = None
    return dur, has_audio, rate, ch


def _concat_list_text(paths: list) -> str:
    """concat demuxer 清单：正斜杠 + 单引号包裹（' → '\\''，build_final.py:38-40）。"""
    return "".join("file '%s'\n" % str(p).replace("\\", "/").replace("'", "'\\''")
                   for p in paths)


def _run(ff: ffmpeg_tool.FFmpegTool, args: list, what: str, log: StageLog,
         dry_run: bool, cwd=None):
    """执行（或 dry-run 打印）一条 ffmpeg 命令；命令行记入 log.commands（§5 命令日志）。

    cwd：烧录/拼图等滤镜内联相对路径的命令以 cwd=out_dir 执行（复审 #1：项目路径
    含 ' 时滤镜内联绝对路径无可靠转义，改 cwd+ASCII 相对名从根上绕开）。"""
    cmd = [ff.ffmpeg] + [str(a) for a in args]
    if dry_run:
        tail = "" if cwd is None else "   # cwd=%s" % cwd
        log("[dry-run] %s%s" % (" ".join(cmd), tail))
        return None
    log.commands.append(" ".join(cmd) + ("" if cwd is None else "   # cwd=%s" % cwd))
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, cwd=cwd,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("%s 失败（exit=%d）\n命令：%s%s\n%s"
                           % (what, proc.returncode, " ".join(cmd),
                              "" if cwd is None else "（cwd=%s）" % cwd,
                              (proc.stderr or "")[-1500:]))
    log("%s OK（%.1fs）" % (what, time.time() - t0))
    return True


def _ambience_usable(fftool, amb_path, offset: float) -> bool:
    """床轨在 offset 处还有内容可切（否则该镜退回纯静音）。"""
    try:
        return offset + 0.01 < fftool.duration_of(str(amb_path))
    except ffmpeg_tool.FFmpegToolError:
        return False


# --------------------------------------------------------------------------- #
# 段主入口（stages 包约定：run_stage(project_dir, config, args) -> int）
# --------------------------------------------------------------------------- #
def run_stage(project_dir, config=None, args=None, log=None) -> int:
    """执行 final 段。

    :param project_dir: 项目目录（绝对路径，或 paths.projects_root 下的相对名）
    :param config: 合并后配置 dict（§2.1 结构）；None 时自行装载全局配置
    :param args: StageOpts / argparse.Namespace / dict（only/force/dry_run/keep_stale
                 + 本段 burn/contact_sheet/use_ambience/title/end_card/out_dir）
    :param log: StageLog 注入点（selftest 用；None 时默认 logs/<项目>_final.log）
    :return: 0 成功/跳过；1 输入错误或合成失败
    """
    opts = _coerce_args(args)
    raw_cfg = getattr(config, "cfg", config)   # MergedConfig 包装 → 取其 cfg dict
    merged = copy.deepcopy(raw_cfg) if isinstance(raw_cfg, dict) else None
    if merged is None:
        try:
            merged = config_mod.load_global()
        except config_mod.ConfigError as e:
            print("配置错误：%s" % e, flush=True)
            return 1
    try:
        project_dir = _resolve_project_dir(project_dir, merged)
    except ConfigError as e:
        print("配置错误：%s" % e, flush=True)
        return 1

    # -- 项目输入（schema 全量校验，缺文件/结构错一次性列出）------------------
    try:
        data = schema_mod.load_project_data(project_dir, merged)
    except SchemaError as e:
        print("%s" % e, flush=True)
        return 1
    project_doc, shots_doc = data["project"], data["shots"]
    subs = data.get("subtitles") or {}
    name = str(project_doc.get("name") or project_dir.name)
    # 项目 overrides 就地再合并（cli 已合并过则幂等；直连 CLI/绝对路径项目由此补上）
    ov = project_doc.get("overrides")
    if isinstance(ov, dict) and ov:
        merged = config_mod.deep_merge(merged, ov)
    try:
        cfg = _stage_cfg(merged)
    except ConfigError as e:
        print("配置错误：%s" % e, flush=True)
        return 1
    cfg["min_bytes"] = int((merged.get("video") or {}).get("min_bytes", 200_000))
    cfg.setdefault("video_dir", "video")
    logs_dir = config_mod.resolve_path((merged.get("paths") or {}).get("logs_dir", "logs"))
    if log is None:
        log = StageLog(logs_dir / ("%s_final.log" % name), enabled=not opts.dry_run)

    shots = sorted(shots_doc.get("shots") or [], key=lambda s: int(s["id"][1:]))
    if opts.only:
        known = {s["id"] for s in shots}
        unknown = set(opts.only) - known
        if unknown:
            log("配置错误：--only 引用不存在的镜：%s（现有：%s）"
                % (", ".join(sorted(unknown)), ", ".join(sorted(known))))
            return 1
        shots = [s for s in shots if s["id"] in set(opts.only)]

    out_dir = project_dir / (opts.out_dir or cfg["out_dir"])
    tmp_dir = out_dir / ".tmp"
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "final"
    final_mp4, final_srt = out_dir / ("%s.mp4" % safe), out_dir / ("%s.srt" % safe)
    sheet_png = out_dir / "contact_sheet.png"

    # -- 1. 源解析（缺镜告警剔除后继续）--------------------------------------
    infos = [resolve_shot_source(project_dir, cfg, s, shots_doc) for s in shots]
    for it in infos:
        if it["src"] is None:
            log("[告警] 镜 %s 缺可用成片文件：%s（剔除后继续）" % (it["sid"], it["reason"]))
    present = [it for it in infos if it["src"] is not None]
    if not present:
        log("无任何可用镜头（video/*.mp4 / camera/*.mp4），final 段终止。缺口：\n  %s"
            % "\n  ".join("%s：%s" % (i["sid"], i["reason"]) for i in infos))
        return 1
    try:
        ff = ffmpeg_tool.FFmpegTool.from_config(cfg)
    except ffmpeg_tool.FFmpegToolError as e:
        log("ffmpeg/ffprobe 不可用：%s" % e)
        return 1

    # -- 2. 探测时长/声轨/音频规格 → 全局偏移 + 全片音频基准（build_final.py:45-48）--
    offsets, durations, off = {}, {}, 0.0
    for it in present:
        try:
            d, has_audio, rate, ch = _probe(ff, it["src"])
        except (ffmpeg_tool.FFmpegToolError, OSError, ValueError, KeyError) as e:
            if opts.dry_run:
                log("[dry-run] 探测 %s 失败（%s），以 0s 计" % (it["src"], e))
                d, has_audio, rate, ch = 0.0, False, None, None
            else:
                log("ffprobe 失败（%s）：%s" % (it["src"], e))
                return 1
        it["duration"], it["has_audio"], it["rate"], it["ch"] = d, has_audio, rate, ch
        offsets[it["sid"]], durations[it["sid"]] = off, d
        off += d
    total = off
    # 全片音频基准（复审 #2）：取首个有声镜实测采样率/声道，无声/异规格镜一律规整到
    # 该基准——否则 concat demuxer 面对混合采样率会静默产出时长错乱的成片（实测
    # 24k mono + 44.1k stereo 各 2s → 总 7.43s）。全无声镜时回退 44.1k/stereo。
    canon = next(((it["rate"] or 44100, it["ch"] or 2) for it in present if it["has_audio"]),
                 (44100, 2))
    log("入片 %d/%d 镜，总时长 %.2fs；偏移：%s；音频基准 %dHz/%dch"
        % (len(present), len(infos), total,
           " ".join("%s@%.2fs" % (i["sid"], offsets[i["sid"]]) for i in present),
           canon[0], canon[1]))

    # -- 3. 断点续跑判定（§5：done ∧ 产物 ∧ 指纹未变 → 跳过）------------------
    # 影响产出的段选项一并入指纹（复审 #3：--title/--end-card/--no-burn/
    # --no-contact-sheet/--no-ambience/--out-dir 任一变化必须触发 stale 重跑，
    # 不得沿用旧成片静默跳过）
    inputs_hash = manifest_mod.hash_fields(
        {"shots": shots_doc, "subtitles": subs, "only": list(opts.only), "name": name,
         "title": opts.title, "end_card": opts.end_card, "burn": opts.burn,
         "contact_sheet": opts.contact_sheet, "use_ambience": opts.use_ambience,
         "out_dir": out_dir.name})
    upstream = {}
    for it in present:
        upstream["mp4:%s" % it["sid"]] = manifest_mod.fingerprint_file(it["src"])
        for tag in ("sheet_first", "sheet_last"):
            kind, path = it[tag]
            if kind == "image":
                upstream["sheet:%s:%s" % (it["sid"], tag)] = \
                    manifest_mod.fingerprint_file(path)
    amb_path = project_dir / str(cfg["ambience_file"])
    if opts.use_ambience and amb_path.is_file():
        upstream["ambience"] = manifest_mod.fingerprint_file(amb_path)
    config_hash = config_mod.config_hash(merged)
    stage_cfg_hash = config_mod.stage_sections_hash(merged, "final")

    mp = manifest_mod.manifest_path_for(project_dir, "final")
    mf = manifest_mod.load(mp)
    unit = mf["units"].get(UNIT)
    if unit and unit.get("status") == "done" and not opts.force and not opts.dry_run:
        if manifest_mod.unit_complete(unit, out_dir,
                                      {"mp4": int(cfg["min_output_bytes"]),
                                       "srt": 1, "sheet": 1}):
            stale = manifest_mod.stale_reason(unit, inputs_hash=inputs_hash,
                                              upstream=upstream,
                                              stage_cfg_hash=stage_cfg_hash)
            reason = "；".join(x for x in stale if x) if isinstance(stale, list) else str(stale or "")
            if stale and not opts.keep_stale:
                log("final 已 stale，重跑：%s" % reason)
            elif stale:
                log("final 已 stale 但 --keep-stale：保留旧成片（%s）" % reason)
                return 0
            else:
                log("final 已是最新（done ∧ 产物在 ∧ 指纹未变），跳过：%s" % final_mp4)
                return 0
        else:
            log("manifest=done 但产物缺失/过小——按未完成重跑。")

    # -- 4. cue/卡片解析（纯计算，失败早退未动盘）----------------------------
    try:
        cues = collect_cues(shots, subs, offsets, total, opts.end_card, log)
        card = resolve_title_card(subs, opts.title, total)
    except ConfigError as e:
        log("配置错误：%s" % e)
        return 1
    if card and card.get("note"):
        log("[告警] %s" % card["note"])

    if opts.dry_run:
        log("== final 段 [DRY-RUN]：项目=%s out=%s ==" % (name, out_dir))
        amb = amb_path if opts.use_ambience and amb_path.is_file() else None
        _dry_plan(ff, cfg, present, offsets, total, canon, amb, tmp_dir, final_mp4,
                  final_srt, sheet_png, cues, card, opts, log)
        log("[dry-run] 计划完成（%d 条字幕%s）——未写任何文件"
            % (len(cues), "，含标题卡" if card else ""))
        return 0

    # 合成主体：任一 ffmpeg 步骤失败 → 记 failed 单元（§5）后返回 1，不留半截状态无痕
    try:
        # -- 5. 逐镜音频规整（补轨前置于 concat，§4.5 步骤 1）---------------------
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        amb = None
        if opts.use_ambience and amb_path.is_file():
            if _ambience_usable(ff, amb_path, 0.0):
                amb = amb_path
                log("ambience 床轨：%s" % amb_path)
            else:
                log("[告警] ambience 探测失败/为空，无声镜改补纯静音轨：%s" % amb_path)
        concat_inputs = []
        for it in present:
            src = it["src"]
            if not it["has_audio"]:
                dst = tmp_dir / ("%s_audio.mp4" % it["sid"])
                if amb is not None and _ambience_usable(ff, amb, offsets[it["sid"]]):
                    args = build_ambience_cmd(cfg, src, amb, dst, it["duration"],
                                              offsets[it["sid"]], canon[0], canon[1])
                    what = "镜 %s 补轨（ambience 混音 @%.2fs，规整 %dHz/%dch）" \
                           % (it["sid"], offsets[it["sid"]], canon[0], canon[1])
                else:
                    args = build_silence_cmd(cfg, src, dst, it["duration"],
                                             canon[0], canon[1])
                    what = "镜 %s 补静音轨（%.2fs @%dHz/%dch）" \
                           % (it["sid"], it["duration"], canon[0], canon[1])
                _run(ff, args, what, log, opts.dry_run)
                src = dst
            elif (it["rate"], it["ch"]) != canon:
                dst = tmp_dir / ("%s_anorm.mp4" % it["sid"])
                args = build_audio_norm_cmd(cfg, src, dst, canon[0], canon[1])
                _run(ff, args, "镜 %s 音频规整（%sHz/%sch → %dHz/%dch）"
                     % (it["sid"], it["rate"], it["ch"], canon[0], canon[1]),
                     log, opts.dry_run)
                src = dst
            concat_inputs.append(src)

        # -- 6. concat（统一转码）------------------------------------------------
        merged_mp4 = tmp_dir / "_merged.mp4"
        concat_list = tmp_dir / "concat.txt"
        with open(concat_list, "w", encoding="utf-8", newline="\n") as f:
            f.write(_concat_list_text(concat_inputs))
        _run(ff, build_concat_cmd(cfg, concat_list, merged_mp4),
             "concat %d 镜" % len(concat_inputs), log, opts.dry_run)

        # -- 7. SRT + 烧录 --------------------------------------------------------
        srt_text, n_subs = build_srt(cues, total, log)
        wrote_srt = False
        if srt_text:
            with open(final_srt, "w", encoding="utf-8", newline="\n") as f:
                f.write(srt_text)
            # 烧录用 ASCII 相对名副本（.tmp/_burn.srt）：项目名/路径含 ' 时滤镜内联
            # 绝对路径无可靠转义（复审 #1 实测）——ffmpeg 以 cwd=out_dir 执行
            with open(tmp_dir / "_burn.srt", "w", encoding="utf-8", newline="\n") as f:
                f.write(srt_text)
            wrote_srt = True
            log("SRT %d 条 → %s（烧录副本 .tmp/_burn.srt）" % (n_subs, final_srt))
        burn_card = None
        if card and opts.burn:
            with open(tmp_dir / "_title.txt", "w", encoding="utf-8", newline="\n") as f:
                f.write(card["text"] + "\n")     # 自由文本走 textfile（复审 #1）
            burn_card = dict(card, textfile=".tmp/_title.txt")
        vf = build_vf(cfg, ".tmp/_burn.srt" if (wrote_srt and opts.burn) else None,
                      burn_card)
        if vf:
            _run(ff, build_burn_cmd(cfg, merged_mp4, vf, final_mp4),
                 "烧录（字幕 %d 条%s）" % (n_subs, " + 标题卡" if card else ""),
                 log, opts.dry_run, cwd=str(out_dir))
        else:
            if wrote_srt and not opts.burn:
                log("已生成 SRT；--no-burn 跳过烧录")
            shutil.move(str(merged_mp4), str(final_mp4))
            log("无字幕/标题可烧，concat 结果直接定名：%s" % final_mp4)

        # -- 8. contact sheet ----------------------------------------------------
        if opts.contact_sheet:
            cells = []
            for it in infos:                # 含缺镜：MISSING 画格（contact_sheet.py 语义）
                cells.append(("%s first" % it["sid"], *it["sheet_first"]))
                cells.append(("%s last" % it["sid"], *it["sheet_last"]))
            scp = plan_contact_sheet(cfg, cells, tmp_dir)
            for ext in scp["extract_cmds"]:
                _run(ff, ext, "抽帧（contact sheet 源）", log, opts.dry_run)
            _run(ff, build_sheet_cmd(scp["inputs"], scp["filter_complex"], sheet_png),
                 "contact sheet（%d 格）" % len(cells), log, opts.dry_run)

    except (RuntimeError, ffmpeg_tool.FFmpegToolError, OSError) as e:
        log("FINAL 段失败：%s" % e)
        mf.update(stage=STAGE, project=name, schema=manifest_mod.SCHEMA,
                  config_hash=config_hash)
        mf["units"][UNIT] = {"status": "failed", "error": str(e)[:1500],
                               "commands": list(log.commands),
                               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        manifest_mod.save(mp, mf)
        return 1

    # -- 9. manifest（原子写；总时长/偏移/命令日志/上游指纹，§5）--------------
    files = {"mp4": final_mp4.name}
    if wrote_srt:
        files["srt"] = final_srt.name
    if opts.contact_sheet:
        files["sheet"] = sheet_png.name
    mf.update(stage=STAGE, project=name, schema=manifest_mod.SCHEMA,
              config_hash=config_hash)
    mf["units"][UNIT] = {
        "status": "done", "files": files,
        "total_duration_s": round(total, 3),
        "shot_offsets": {sid: round(v, 3) for sid, v in offsets.items()},
        "shot_durations": {sid: round(v, 3) for sid, v in durations.items()},
        "missing_shots": [it["sid"] for it in infos if it["src"] is None],
        "subtitles": n_subs, "title_card": bool(card),
        "ambience": bool(amb), "burn": bool(opts.burn),
        "inputs_hash": inputs_hash, "upstream": upstream,
        "stage_cfg_hash": stage_cfg_hash, "commands": list(log.commands),
        "attempts": 1, "error": None,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    manifest_mod.save(mp, mf)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    log("FINAL 段完成：%s（%.2fs，%d 镜，字幕 %d 条，contact sheet：%s）"
        % (final_mp4, total, len(present), n_subs,
           "是" if opts.contact_sheet else "否"))
    return 0


def _dry_plan(ff, cfg, present, offsets, total, canon, amb, tmp_dir, final_mp4,
              final_srt, sheet_png, cues, card, opts, log) -> None:
    """dry-run：打印将执行的完整命令序列（不执行、不写盘）。"""
    files = []
    for it in present:
        if not it["has_audio"]:
            dst = tmp_dir / ("%s_audio.mp4" % it["sid"])
            if amb is not None and _ambience_usable(ff, amb, offsets[it["sid"]]):
                _print_cmd(log, ff.ffmpeg,
                           build_ambience_cmd(cfg, it["src"], amb, dst,
                                              it["duration"], offsets[it["sid"]],
                                              canon[0], canon[1]))
            else:
                _print_cmd(log, ff.ffmpeg,
                           build_silence_cmd(cfg, it["src"], dst, it["duration"],
                                             canon[0], canon[1]))
            files.append(dst)
        elif (it["rate"], it["ch"]) != canon:
            dst = tmp_dir / ("%s_anorm.mp4" % it["sid"])
            _print_cmd(log, ff.ffmpeg,
                       build_audio_norm_cmd(cfg, it["src"], dst, canon[0], canon[1]))
            files.append(dst)
        else:
            files.append(it["src"])
    concat_list, merged_mp4 = tmp_dir / "concat.txt", tmp_dir / "_merged.mp4"
    log("[dry-run] 写 %s：" % concat_list)
    for line in _concat_list_text(files).splitlines():
        log("    " + line)
    _print_cmd(log, ff.ffmpeg, build_concat_cmd(cfg, concat_list, merged_mp4))
    srt_text, n_subs = build_srt(cues, total)      # 与真实运行同算法（含钳制）
    if srt_text:
        log("[dry-run] 写 %s 与烧录副本 .tmp/_burn.srt（%d/%d 条入片，总时长 %.2fs 钳制）"
            % (final_srt, n_subs, len(cues), total))
    elif cues:
        log("[dry-run] SRT 0/%d 条入片（全部被总时长 %.2fs 钳制丢弃）" % (len(cues), total))
    burn_card = dict(card, textfile=".tmp/_title.txt") if (card and opts.burn) else None
    vf = build_vf(cfg, ".tmp/_burn.srt" if (srt_text and opts.burn) else None, burn_card)
    if vf:
        log("[dry-run] 写 .tmp/_title.txt（标题文本走 textfile，规避滤镜转义）"
            if burn_card else "[dry-run] 无标题卡")
        _print_cmd(log, ff.ffmpeg, build_burn_cmd(cfg, merged_mp4, vf, final_mp4))
        log("[dry-run] 上条以 cwd=%s 执行（滤镜内 .tmp/_burn.srt 等相对名由此解析）"
            % final_mp4.parent)
    else:
        log("[dry-run] %s → %s（无烧录）" % (merged_mp4, final_mp4))
    if opts.contact_sheet:
        log("[dry-run] contact sheet → %s（缺镜画格真实运行时补 MISSING）" % sheet_png)
        for ext in sheet_extract_preview(cfg, present, tmp_dir):
            _print_cmd(log, ff.ffmpeg, ext)


def sheet_extract_preview(cfg: dict, present: list, tmp_dir) -> list:
    """dry-run 用的抽帧命令预览（full_video 镜首/末帧）。"""
    cmds = []
    for it in present:
        for tag in ("sheet_first", "sheet_last"):
            kind, path = it[tag]
            if kind == "video":
                label = "%s %s" % (it["sid"], "first" if tag.endswith("first") else "last")
                cmds.append(plan_contact_sheet(
                    cfg, [(label, "video", path)], tmp_dir)["extract_cmds"][0])
    return cmds


def _print_cmd(log, ffmpeg: str, args: list) -> None:
    log("[dry-run] %s" % " ".join([ffmpeg] + [str(a) for a in args]))


# --------------------------------------------------------------------------- #
# selftest 检查注册表（设计 §7；根 selftest.py 的 C 段聚合调用）
# 全部 dry：临时目录 + 纯计算 + 只读探测；f9（真实合成冒烟）额外要求
# 环境变量 PIPELINE_FINAL_SELFTEST_FULL=1（CPU-only ffmpeg lavfi，不碰 GPU/ComfyUI）
# --------------------------------------------------------------------------- #
def selftest_checks(with_server: bool = False, strict: bool = False) -> list:
    import tempfile

    from .. import scaffold

    checks = []

    def check(cid, title, level="fatal"):
        def deco(fn):
            checks.append({"id": cid, "title": title, "level": level, "run": fn})
            return fn
        return deco

    CFG = None  # 惰性：基础 cfg（final 节默认值）

    def base_cfg():
        return _stage_cfg({})

    # ---- f1 cue 收集与 SRT ----
    @check("f1", "cue 双来源合并/时轴偏移/钳制/排序/SRT 格式（build_final.py:45-64 泛化）")
    def _f1():
        cfg = base_cfg()
        shots = [{"id": "S1", "cues": [{"t_in": 2.6, "t_out": 4.9, "text": "抄近路吧。"}]},
                 {"id": "S2", "cues": [{"t_in": 0.7, "t_out": 4.8, "text": "镜内字幕"}]},
                 {"id": "S9", "cues": [{"t_in": 0.1, "t_out": 0.2, "text": "缺镜cue"}]}]
        subs = {"cues": [{"shot": "S2", "t_in": 0.9, "t_out": 1.4, "text": "subs镜内"},
                         {"shot": "S8", "t_in": 0.0, "t_out": 1.0, "text": "指向未入片镜"},
                         {"t_in": 0.2, "t_out": 0.5, "text": "全局绝对"}],
                "end_card": {"text": "—— 完 ——", "tail_s": 4.0}}
        warns = []
        cues = collect_cues(shots, subs, offsets={"S1": 0.0, "S2": 5.17}, total=8.0,
                            end_text=None, log=warns.append)
        got = [(round(c["start"], 2), round(c["end"], 2), c["text"]) for c in cues]
        assert got == [(0.2, 0.5, "全局绝对"), (2.6, 4.9, "抄近路吧。"),
                       (4.0, 7.4, "—— 完 ——"), (5.87, 9.97, "镜内字幕"),
                       (6.07, 6.57, "subs镜内")], got   # collect 不钳制（钳制在 build_srt）；S2 cue 越片尾
        assert any("S8" in w for w in warns) and any("S9" not in w for w in warns), warns
        srt, n = build_srt(cues + [{"start": 7.99, "end": 12.0, "text": "越界"},
                                   {"start": 3.0, "end": 3.01, "text": "零长"}],
                           total=8.0, log=warns.append)
        assert n == 5 and srt.startswith("1\n00:00:00,200 --> 00:00:00,500\n全局绝对\n\n"
                                         "2\n00:00:02,600 --> 00:00:04,900\n抄近路吧。"), srt[:90]
        assert "\n\n4\n00:00:05,870 --> 00:00:08,000\n镜内字幕\n" in srt \
            and srt.endswith("5\n00:00:06,070 --> 00:00:06,570\nsubs镜内\n"), srt[-80:]
        assert srt_ts(3661.5) == "01:01:01,500" and srt_ts(-1) == "00:00:00,000"
        assert "越界" in "".join(warns) and "零长" in "".join(warns)
        return "5 条入片（排序/钳制正确）；缺镜与越界/零长 cue 告警丢弃；时间戳逗号毫秒"

    # ---- f2 滤镜与转义（复审 #1 修复后口径：自由文本走 textfile，内联仅限安全字符）----
    @check("f2", "滤镜构造：textfile 通道 + 内联文本守卫 + 盘符转义 + vf 次序（复审 #1 回归）")
    def _f2():
        from .. import ffmpeg_tool as ft
        assert ft.filter_path("E:\\a\\b c.srt") == "E\\:/a/b c.srt"
        assert ft.filter_path("C:/Windows/Fonts/msyh.ttc") == "C\\:/Windows/Fonts/msyh.ttc"
        # 内联文本守卫：撇号/冒号/逗号等一律拒绝（'…' 引用段内无可靠转义，复审实锓），
        # 自由文本必须走 textfile
        assert _label_text("S1 first") == "S1 first"
        for bad in ("It's", "a,b", "a:b", "a;b", "a%b", "a\\b", "a[b"):
            try:
                _label_text(bad)
                raise AssertionError("危险内联文本 %r 未被拒绝" % bad)
            except ConfigError:
                pass
        try:
            _assert_filter_path_safe("E:/qui'll's/msyh.ttc")
            raise AssertionError("含撇号路径未被守卫拒绝")
        except ConfigError:
            pass
        cfg = base_cfg()
        sub = subtitles_filter(cfg, ".tmp/_burn.srt")   # 烧录用 ASCII 相对名（cwd=out_dir）
        assert sub.startswith("subtitles=filename='.tmp/_burn.srt'")
        assert "charenc=UTF-8" in sub and "fontsdir='C\\:/Windows/Fonts'" in sub
        assert sub.endswith("force_style='%s'" % cfg["sub_style"]) and "YaHei" in sub
        card = resolve_title_card({}, "It's 雨 落'无 声", total=3.4)
        tf = title_drawtext_filter(cfg, dict(card, textfile=".tmp/_title.txt"))
        assert "fontfile='C\\:/Windows/Fonts/msyh.ttc'" in tf
        assert "textfile='.tmp/_title.txt'" in tf and "text='" not in tf   # 文本不内联
        assert "fontsize=58" in tf and "alpha='if(" in tf
        assert "(t-0.9)/0.6" in tf and "(3-t)/0.6" in tf  # 渐入/渐出表达式（fade=0.6）
        vf = build_vf(cfg, ".tmp/_burn.srt", dict(card, textfile=".tmp/_title.txt"))
        assert vf.index("drawtext=") < vf.index("subtitles=") < vf.index("fade=")
        assert vf.endswith("fade=t=in:st=0:d=0.4")            # build_final.py:72 参数
        assert build_vf(cfg, None, None) == ""
        return "标题走 textfile/内联守卫拒撇号/烧录用相对 ASCII 名/盘符转义/vf 次序"

    # ---- f3 命令构造 ----
    @check("f3", "concat/补轨命令构造：crf18+aac192k、anullsrc、ambience 混音（build_final.py:42-43）")
    def _f3():
        cfg = base_cfg()
        cc = build_concat_cmd(cfg, "l.txt", "o.mp4")
        assert cc[:6] == ["-y", "-f", "concat", "-safe", "0", "-i", "l.txt"][:6] \
            or cc[:5] == ["-y", "-f", "concat", "-safe", "0"], cc[:6]
        assert "-i" in cc and cc[cc.index("-i") + 1] == "l.txt"
        for flag, val in (("-crf", "18"), ("-preset", "medium"),
                          ("-c:a", "aac"), ("-b:a", "192k"), ("-c:v", "libx264")):
            assert cc[cc.index(flag) + 1] == val, (flag, cc)
        # 补轨/规整命令按全片音频基准生成（复审 #2：不得硬编码 44.1k/stereo）
        sc = build_silence_cmd(cfg, "v.mp4", "o.mp4", 5.17, 24000, 1)
        assert sc[sc.index("-f") + 1] == "lavfi" and "anullsrc=r=24000:cl=mono" in sc
        assert sc[sc.index("-t") + 1] == "5.17" and "-shortest" in sc
        assert sc[sc.index("-c:v") + 1] == "copy"           # 视频直拷不二压
        assert sc[sc.index("-ar") + 1] == "24000" and sc[sc.index("-ac") + 1] == "1"
        sc2 = build_silence_cmd(cfg, "v.mp4", "o.mp4", 5.17, 44100, 2)
        assert "anullsrc=r=44100:cl=stereo" in sc2
        nc = build_audio_norm_cmd(cfg, "v.mp4", "o.mp4", 24000, 1)
        assert nc[nc.index("-c:v") + 1] == "copy" and nc[nc.index("-ar") + 1] == "24000" \
            and nc[nc.index("-ac") + 1] == "1"
        ac = build_ambience_cmd(cfg, "v.mp4", "amb.mp3", "o.mp4", 5.17, 10.34, 24000, 1)
        assert ac[ac.index("-ss") + 1] == "10.34" and ac[ac.index("-t") + 1] == "5.17"
        fc = ac[ac.index("-filter_complex") + 1]
        assert "aresample=24000" in fc and "aformat=channel_layouts=mono" in fc  # 床轨先规整再混
        assert "apad=whole_dur=5.17" in fc and "amix=inputs=2" in fc \
            and "atrim=0:5.17" in fc and "[aout]" in fc
        assert ac[ac.index("[aout]") - 1] == "-map" and "0:v" in ac   # filter 输出经 -map 接入
        assert ac[ac.index("-ar") + 1] == "24000" and ac[ac.index("-ac") + 1] == "1"
        return "统一转码参数；anullsrc/norm/ambience 全部跟随基准采样率/声道（复审 #2 回归）"

    # ---- f4 源解析与帧来源 ----
    @check("f4", "源解析：B镜→video、full_video→camera、大小门/回退、sheet 帧来源规则（§4.5）")
    def _f4():
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(_fixture_project(td, scaffold, real_media=False))
            cfg = base_cfg()
            cfg["min_bytes"] = 1000
            doc = schema_mod.load_project_data(pdir, {})["shots"]
            shots = {s["id"]: s for s in doc["shots"]}
            r1 = resolve_shot_source(pdir, cfg, shots["S1"], doc)
            assert r1["src"] == pdir / "video" / "S1.mp4" and r1["kind"] == "video"
            assert r1["sheet_first"][1] == pdir / "keyframes" / "S1_first.png"
            r2 = resolve_shot_source(pdir, cfg, shots["S2"], doc)   # keyframes 缺 S2_last
            assert r2["sheet_last"] == ("missing", None)
            r3 = resolve_shot_source(pdir, cfg, shots["S3"], doc)   # full_video → camera
            assert r3["src"] == pdir / "camera" / "S3.mp4" and r3["full"]
            assert r3["sheet_first"] == ("video", r3["src"])
            cfg_big = dict(cfg)
            cfg_big["min_bytes"] = 10_000_000                      # 大小门 → 全剔除
            rg = resolve_shot_source(pdir, cfg_big, shots["S1"], doc)
            assert rg["src"] is None and "过小" in rg["reason"]
            os.remove(pdir / "video" / "S2.mp4")                   # 回退 camera
            os.makedirs(pdir / "camera", exist_ok=True)
            shutil.copyfile(pdir / "camera" / "S3.mp4", pdir / "camera" / "S2.mp4")
            rb = resolve_shot_source(pdir, cfg, shots["S2"], doc)
            assert rb["src"] == pdir / "camera" / "S2.mp4"
        return "优先级/回退/门剔除/帧来源（keyframes、camfirst 优先、full_video 抽帧）全对"

    # ---- f5 contact sheet 计划 ----
    @check("f5", "contact sheet 计划：scale+tile、MISSING 画格、输入序号、full_video 抽帧（-sseof）")
    def _f5():
        cfg = base_cfg()
        cells = [("S1 first", "image", "a.png"), ("S1 last", "missing", None),
                 ("S2 first", "video", "v.mp4"), ("S2 last", "missing", None)]
        with tempfile.TemporaryDirectory() as td:
            p = plan_contact_sheet(cfg, cells, td)
            fc = p["filter_complex"]
            assert len(p["extract_cmds"]) == 1 and "-sseof" not in p["extract_cmds"][0]
            assert "scale=448:256" in fc and "tile=2x2" in fc \
                and "concat=n=4:v=1:a=0" in fc
            assert fc.count("MISSING S1 last") == 1 and fc.count("MISSING S2 last") == 1
            assert "trim=end_frame=1" in fc                       # color 源取单帧
            ins = p["inputs"]
            assert ins.count("-i") == 4 and ins.count("lavfi") == 2  # 2 图 + 1 抽帧 + 2 color
            # 输入序号必须连续 0..3（曾因 lavfi 4 元素计数出错：Invalid file index）
            import re as _re
            idx = sorted(int(m) for m in _re.findall(r"\[(\d+):v\]", fc))
            assert idx == [0, 1, 2, 3], idx
            last_cells = [("S9 first", "video", "v.mp4"), ("S9 last", "video", "v.mp4")]
            p2 = plan_contact_sheet(cfg, last_cells, td)
            assert any("-sseof" in c for c in p2["extract_cmds"])   # last 帧从尾部抽
        return "tile=2x2/MISSING/序号连续/last 用 -sseof；曾修的输入序号 bug 有回归断言"

    # ---- f6 断点续跑判定（共享 manifest 模块语义 + 选项归一）----
    @check("f6", "断点续跑：unit_complete 大小门 + stale_reason 三类失效 + _coerce_args 归一")
    def _f6():
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "剧.mp4").write_bytes(b"\0" * 2000)
            (out / "剧.srt").write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nx\n")
            unit = {"status": "done", "files": {"mp4": "剧.mp4", "srt": "剧.srt"},
                    "inputs_hash": "h1", "upstream": {"mp4:S1": "S1.mp4|1|2"},
                    "stage_cfg_hash": "c1"}
            gates = {"mp4": 1000, "srt": 1, "sheet": 1}
            assert manifest_mod.unit_complete(unit, out, gates)
            assert not manifest_mod.unit_complete(unit, out, {"mp4": 5000, "srt": 1, "sheet": 1})
            assert not manifest_mod.unit_complete(dict(unit, status="running"), out, gates)
            r = manifest_mod.stale_reason(unit, inputs_hash="h2")
            assert r and "inputs_hash" in r
            r = manifest_mod.stale_reason(unit, upstream={"mp4:S1": "S1.mp4|1|3"})
            assert r and "指纹" in r
            r = manifest_mod.stale_reason(unit, stage_cfg_hash="c2")
            assert r and "配置" in r
            assert manifest_mod.stale_reason(unit, inputs_hash="h1",
                                             upstream={"mp4:S1": "S1.mp4|1|2"},
                                             stage_cfg_hash="c1") is None
        o = _coerce_args({"dry_run": True, "only": "S1, S3"})
        assert o.dry_run and o.only == ("S1", "S3") and o.burn and o.out_dir is None
        o2 = _coerce_args(None)
        assert not o2.force and o2.contact_sheet
        # 复审 #5 回归：非法数值/撇号字体路径 → ConfigError（可读错误，非裸 ValueError）
        for bad in ("abc", None):
            try:
                _stage_cfg({"final": {"fade_in_s": bad}})
                raise AssertionError("fade_in_s=%r 未报 ConfigError" % bad)
            except ConfigError:
                pass
        try:
            _stage_cfg({"final": {"font": "E:/x'/msyh.ttc"}})
            raise AssertionError("撇号字体路径未被拒绝")
        except ConfigError:
            pass
        assert _stage_cfg({})["crf"] == 18            # 合法配置不受影响
        # 复审 #4 回归：双引号串内 \" 不闭引号，串内 # 不算注释
        from .. import miniyaml as _my
        assert _my.parse('text: "a \\"b # x\\""') == {"text": 'a "b # x"'}
        return ("done 门/stale 三类失效/未变不失效/选项宽容归一；"
                "非法数值与撇号字体路径报 ConfigError；miniyaml 双引号# 边角")

    # ---- f7 标题卡/片尾卡语义 ----
    @check("f7", "title_card/end_card 语义：默认时间与 fontsize、CLI 覆盖、tail_s/lead_in、t_out<=t_in 报错")
    def _f7():
        assert resolve_title_card({}, None, 10.0) is None
        c = resolve_title_card({"title_card": {"text": "A"}}, None, 10.0)
        assert (c["t_in"], c["t_out"], c["fontsize"]) == (0.9, 3.0, 58)
        c2 = resolve_title_card({}, "片名", 10.0)
        assert c2["text"] == "片名" and "t_in" in c2
        try:
            resolve_title_card({"title_card": {"text": "x", "t_in": 3, "t_out": 3}}, None, 10.0)
            raise AssertionError("t_out<=t_in 未报错")
        except ConfigError:
            pass
        warns = []
        cue, = collect_cues([], {"end_card": {"text": "完", "tail_s": 4.4}}, offsets={},
                            total=10.0, end_text=None, log=warns.append)
        assert (cue["start"], cue["end"]) == (5.6, 9.4)          # 尾端 0.6s 让位
        cue2, = collect_cues([], {"end_card": {"text": "完",
                                               "lead_in": 4.4, "lead_out": 0.6}},
                             offsets={}, total=10.0, end_text=None, log=warns.append)
        assert (cue2["start"], cue2["end"]) == (5.6, 9.4)
        cue3, = collect_cues([], {"end_card": {"text": "旧"}}, offsets={}, total=10.0,
                             end_text="CLI 覆盖", log=warns.append)
        assert cue3["text"] == "CLI 覆盖"
        return "scaffold tail_s 形与显式 lead_in/lead_out 等价；--end-card 覆盖生效"

    # ---- f8 CLI dry-run 端到端（不执行 ffmpeg 编码、不写盘）----
    @check("f8", "CLI dry-run 端到端：exit 0、打印完整命令序列、不写 final/ 与日志")
    def _f8():
        with tempfile.TemporaryDirectory() as td:
            pdir = _fixture_project(td, scaffold, real_media=False)
            r = subprocess.run(
                [sys.executable, "-m", "pipeline.stages.final", "--project", pdir,
                 "--dry-run", "--set", "video.min_bytes=1000",
                 "--set", "final.min_output_bytes=1000"],
                cwd=str(config_mod.repo_root()), capture_output=True,
                encoding="utf-8", errors="replace", timeout=120)
            out = (r.stdout or "") + (r.stderr or "")
            assert r.returncode == 0, "exit=%d\n%s" % (r.returncode, out[-1500:])
            assert "[dry-run]" in out and "-f concat -safe 0" in out
            # 伪字节素材探测失败按 0s 计 → 字幕全被钳制丢弃属预期；标题卡恒在（scaffold 模板）
            assert "drawtext=fontfile=" in out and "fade=t=in:st=0:d=0.4" in out
            assert "anullsrc=r=44100:cl=stereo" in out and "-frames:v 1" in out
            # scaffold 骨架自带空的 final/final_manifest.json；dry-run 不得新增任何产物
            left = sorted(os.listdir(os.path.join(pdir, "final")))
            assert left == ["final_manifest.json"], "dry-run 落盘了：%s" % left
            assert not (config_mod.repo_root() / "logs" / "自检合成剧_final.log").exists()
        return "计划命令齐全（concat/烧录/补轨/拼图）；零落盘"

    # ---- f9 真实合成冒烟（CPU-only lavfi；需显式开启）----
    @check("f9", "真实合成冒烟（混合音频规格：24k/mono + 44.1k/stereo + 静音床轨混音；"
                 "复审 #2 时长回归 + 复审 #3 选项 stale 回归）——默认 SKIP，设 "
                 "PIPELINE_FINAL_SELFTEST_FULL=1 启用（CPU-only，不碰 GPU/ComfyUI）",
           level="optional")
    def _f9():
        if os.environ.get("PIPELINE_FINAL_SELFTEST_FULL") != "1":
            return "SKIP: 未设 PIPELINE_FINAL_SELFTEST_FULL=1（保持 selftest 全 dry）"
        ff = shutil.which("ffmpeg")
        if not ff:
            return "SKIP: ffmpeg 不在 PATH"
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(_fixture_project(td, scaffold, real_media=True, ffmpeg=ff))
            merged = config_mod.load_merged(
                None, sets=["video.min_bytes=1000", "final.min_output_bytes=1000"]).cfg
            quiet = StageLog(Path(td) / "quiet.log", enabled=False)
            rc = run_stage(str(pdir), config=merged, args=FinalOpts(), log=quiet)
            assert rc == 0, "首跑 rc=%d（详见上方日志）" % rc
            out = pdir / "final"
            mp4, srt = out / "自检合成剧.mp4", out / "自检合成剧.srt"
            sheet = out / "contact_sheet.png"
            assert mp4.is_file() and mp4.stat().st_size > 5000
            assert sheet.is_file() and sheet.stat().st_size > 3000
            text = srt.read_text(encoding="utf-8")
            assert "示例字幕" in text and "镜内字幕" in text and "—— 完 ——" in text
            # 复审 #2 回归：混合采样率 concat 旧实现会静默拉长时长（4s→7.43s 实测）
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration:stream=codec_type,sample_rate,channels",
                 "-of", "json", str(mp4)], capture_output=True,
                encoding="utf-8", errors="replace")
            info = json.loads(probe.stdout)
            dur = float(info["format"]["duration"])
            audio = [s for s in info["streams"] if s.get("codec_type") == "audio"]
            assert 3.3 < dur < 4.1, "成片时长 %.3fs：混合采样率规整失效（旧 bug ≈7.4s）" % dur
            assert audio and audio[0].get("sample_rate") == "24000" \
                and audio[0].get("channels") == 1, audio   # 基准=首个有声镜 S1（24k/mono）
            mf = json.loads((out / "final_manifest.json").read_text(encoding="utf-8"))
            unit = mf["units"]["assemble"]
            assert unit["status"] == "done" and unit["missing_shots"] == []
            assert unit["shot_offsets"]["S2"] > 0 and len(unit["commands"]) >= 5
            assert abs(unit["total_duration_s"] - 3.6) < 0.5
            mf_path = out / "final_manifest.json"
            snap = os.stat(mf_path).st_mtime_ns
            rc2 = run_stage(str(pdir), config=merged, args=FinalOpts(), log=quiet)
            assert rc2 == 0 and os.stat(mf_path).st_mtime_ns == snap, "二跑应跳过（不改 manifest）"
            # 复审 #3 回归：--title 改变必须触发 stale 重跑（不得静默沿用旧成片）
            mp4_snap = os.stat(mp4).st_mtime_ns
            rc3 = run_stage(str(pdir), config=merged,
                            args=FinalOpts(title="全 新 标 题"), log=quiet)
            assert rc3 == 0 and os.stat(mp4).st_mtime_ns != mp4_snap, \
                "改 --title 未重跑（复审 #3：影响产出的选项须入指纹）"
            snap2 = os.stat(mf_path).st_mtime_ns
            rc4 = run_stage(str(pdir), config=merged,
                            args=FinalOpts(title="全 新 标 题"), log=quiet)
            assert rc4 == 0 and os.stat(mf_path).st_mtime_ns == snap2, "同选项二跑应跳过"
            os.utime(pdir / "video" / "S1.mp4")        # 上游指纹变化 → stale 重跑
            rc5 = run_stage(str(pdir), config=merged,
                            args=FinalOpts(title="全 新 标 题"), log=quiet)
            assert rc5 == 0 and os.stat(mf_path).st_mtime_ns != snap2, "stale 后应重跑"
        return ("时长 %.2fs=规整正确（旧 bug≈7.4s）、输出 24k/mono=跟基准；"
                "--title 变化触发重跑、同选项跳过、touch 上游 stale 重跑" % dur)

    # ---- f10 撇号端到端（复审 #1 回归；需显式开启）----
    @check("f10", "撇号端到端：项目名/标题含 ' 的真实烧录（textfile + 相对名通道）——"
                  "默认 SKIP，设 PIPELINE_FINAL_SELFTEST_FULL=1 启用（CPU-only）",
           level="optional")
    def _f10():
        if os.environ.get("PIPELINE_FINAL_SELFTEST_FULL") != "1":
            return "SKIP: 未设 PIPELINE_FINAL_SELFTEST_FULL=1（保持 selftest 全 dry）"
        ff = shutil.which("ffmpeg")
        if not ff:
            return "SKIP: ffmpeg 不在 PATH"
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(_fixture_project(td, scaffold, real_media=True, ffmpeg=ff,
                                         name="quill's 自检剧"))
            merged = config_mod.load_merged(
                None, sets=["video.min_bytes=1000", "final.min_output_bytes=1000"]).cfg
            quiet = StageLog(Path(td) / "quiet.log", enabled=False)
            rc = run_stage(str(pdir), config=merged,
                           args=FinalOpts(title="It's 撇号'标题"), log=quiet)
            assert rc == 0, "撇号项目 rc=%d（复审 #1：滤镜内联撇号应已走 textfile/相对名）" % rc
            mp4 = pdir / "final" / "quill's 自检剧.mp4"     # 项目名撇号进产物文件名
            srt = pdir / "final" / "quill's 自检剧.srt"
            assert mp4.is_file() and mp4.stat().st_size > 5000
            assert srt.is_file() and "示例字幕" in srt.read_text(encoding="utf-8")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(mp4)], capture_output=True,
                encoding="utf-8", errors="replace")
            dur = float(probe.stdout.strip())
            assert 3.3 < dur < 4.1, "撇号项目成片时长异常：%.3fs" % dur
        return "撇号项目名+撇号标题烧录 rc=0、成片 %.2fs、srt/产物文件名含 ' 正常" % dur

    return checks


def _fixture_project(td, scaffold, real_media: bool, ffmpeg: str | None = None,
                     name: str = "自检合成剧") -> str:
    """scaffold 生成合法项目 → 追加 S2（镜内 cues）/S3（full_video）→ 放媒体文件。

    real_media=False：伪字节 mp4/png（只测存在性与大小门，探测按失败容忍）。
    real_media=True：ffmpeg lavfi 生成真实 3 镜——S1 有声 24kHz/mono（全片基准）、
    S2 无声（ambience 混音，床轨 44.1k/stereo 需规整）、S3 camera full_video 有声
    44.1k/stereo（非基准 → 音频规整），+ ambience.mp3 + keyframes PNG
    （S2_last 故意缺失 → MISSING 画格）。name 可带撇号（复审 #1 端到端）。"""
    pdir = str(scaffold.new_project(name, projects_root=td, force=True,
                                    log=lambda m: None))
    sp = os.path.join(pdir, "shots.yaml")
    with open(sp, "a", encoding="utf-8", newline="\n") as f:
        f.write("""  - id: S2
    title: 示例镜·巷口
    characters: [xiaoyu]
    first: "a narrow neon-lit alley mouth at night in the rain"
    last: "she steps into the alley under her transparent umbrella"
    motion: |
      [0s-2s] rain intensifies
      [2s-5s] she walks into the alley
    cues:
      - {t_in: 0.7, t_out: 4.8, text: "镜内字幕"}
  - id: S3
    title: 示例镜·运镜条
    characters: [xiaoyu]
    first: "a slow parallax shot of the bus shelter at night"
    last: "the parallax settles on the timetable sign"
    camera: {mode: A, move: parallax, output: full_video}
    motion: |
      [0s-2s] smooth parallax drift
""")
    os.makedirs(os.path.join(pdir, "assets"), exist_ok=True)
    if not real_media:
        for rel in ("video/S1.mp4", "video/S2.mp4", "camera/S3.mp4",
                    "assets/ambience.mp3"):
            p = os.path.join(pdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"\0" * 2000)
        for sid in ("S1", "S2"):
            for kind in ("first", "last"):
                if sid == "S2" and kind == "last":
                    continue
                with open(os.path.join(pdir, "keyframes", "%s_%s.png" % (sid, kind)),
                          "wb") as f:
                    f.write(b"\x89PNG" + b"\0" * 500)
        return pdir

    def run(args):
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error"] + args,
                           capture_output=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr[-400:]

    run(["-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24:duration=1.2",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000:duration=1.2",
         "-ac", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", os.path.join(pdir, "video", "S1.mp4")])      # 24kHz/mono（复审 #2）
    run(["-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24:duration=1.2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         os.path.join(pdir, "video", "S2.mp4")])
    run(["-y", "-f", "lavfi", "-i", "color=c=0x2255aa:size=160x120:rate=24:duration=1.2",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=1.2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         os.path.join(pdir, "camera", "S3.mp4")])                  # 44.1k/stereo ≠ 基准 → 规整
    run(["-y", "-f", "lavfi", "-i", "sine=frequency=110:duration=3",
         "-af", "volume=0.4", "-c:a", "libmp3lame", "-b:a", "64k",
         os.path.join(pdir, "assets", "ambience.mp3")])
    for sid, colors in (("S1", ("0x994433", "0x334499")), ("S2", ("0x668844", None))):
        for kind, c in zip(("first", "last"), colors):
            if c is None:
                continue
            run(["-y", "-f", "lavfi", "-i", "color=c=%s:s=320x180:d=1" % c,
                 "-frames:v", "1", os.path.join(pdir, "keyframes", "%s_%s.png" % (sid, kind))])
    return pdir


# --------------------------------------------------------------------------- #
# 直连 CLI（cli.py 之外的本段完整入口；含段专有选项）
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.stages.final",
        description="final 段：按 shots.yaml 顺序 concat 各镜 mp4 → 成片（字幕/标题卡烧录 + contact sheet）",
        epilog="示例：python -m pipeline.stages.final --project projects/我的剧 --dry-run")
    ap.add_argument("--project", required=True, help="项目目录（绝对路径，或 projects/ 下相对名）")
    ap.add_argument("--only", default=None, help="只合成指定镜（预览），如 S1,S3")
    ap.add_argument("--force", action="store_true", help="忽略完成判定强制重跑")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的 ffmpeg 命令，不写任何文件")
    ap.add_argument("--set", dest="sets", action="append", default=[], metavar="a.b=v",
                    help="配置覆盖（最高优先级），如 --set final.crf=20")
    ap.add_argument("--no-burn", action="store_true", help="不烧录字幕/标题（仍生成 SRT 文件）")
    ap.add_argument("--no-contact-sheet", action="store_true", help="跳过 contact sheet")
    ap.add_argument("--no-ambience", action="store_true", help="无声镜补纯静音轨（不用 ambience 床轨）")
    ap.add_argument("--title", default=None, help="标题卡文本（覆盖/启用 subtitles.yaml title_card）")
    ap.add_argument("--end-card", default=None, help="片尾卡文本（覆盖/启用 end_card，挂片尾 SRT cue）")
    ap.add_argument("--out-dir", default=None,
                    help="输出目录覆盖（项目目录相对；默认 final，如 output/final）")
    ap.add_argument("--keep-stale", action="store_true", help="stale 时保留旧成片不重跑")
    args = ap.parse_args(argv)

    try:
        # 全局配置 + CLI --set（项目层 overrides 由 run_stage 按项目目录就地合并）
        merged = config_mod.load_merged(None, sets=args.sets)
    except config_mod.ConfigError as e:
        print("配置错误：%s" % e, flush=True)
        return 1
    opts = FinalOpts(only=args.only or (), force=args.force, dry_run=args.dry_run,
                     keep_stale=args.keep_stale, burn=not args.no_burn,
                     contact_sheet=not args.no_contact_sheet,
                     use_ambience=not args.no_ambience, title=args.title,
                     end_card=args.end_card, out_dir=args.out_dir)
    return run_stage(args.project, config=merged, args=opts)


if __name__ == "__main__":
    sys.exit(main())
