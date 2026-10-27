# -*- coding: utf-8 -*-
r"""ffmpeg/ffprobe 发现与封装（docs §2.1 final 节）。

PATH 优先（已核实 ffmpeg 8.1.1 在 PATH），配置可写绝对路径覆盖；
subprocess 一律 encoding="utf-8"；Windows 盘符在 filter 里的转义（`C\:/`、
冒号转义，build_final.py:71 前车之鉴）封装为 filter_path()。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

__all__ = ["FFmpegToolError", "FFmpegTool", "discover", "filter_path", "ffmpeg_version"]


class FFmpegToolError(RuntimeError):
    pass


def _find(configured: str, name: str) -> str:
    """配置值含路径分隔符 → 按绝对路径校验；裸名 → shutil.which。"""
    if not configured:
        configured = name
    if "/" in configured or "\\" in configured:
        return configured  # 绝对/相对路径：交给调用方在使用时发现不存在（doctor 会提示）
    found = shutil.which(configured)
    if not found:
        raise FFmpegToolError("在 PATH 中找不到 %s（配置值 %r）；请在 config.yaml final.%s 写绝对路径" % (name, configured, name))
    return found


def discover(final_cfg: dict):
    """返回 (ffmpeg, ffprobe)。"""
    f = final_cfg or {}
    return _find(str(f.get("ffmpeg", "ffmpeg")), "ffmpeg"), _find(str(f.get("ffprobe", "ffprobe")), "ffprobe")


def filter_path(p) -> str:
    """ffmpeg filter 参数里的路径转义：反斜杠→正斜杠，`:`→`\\:`（Windows 盘符 `C\\:/…`）。"""
    s = str(p).replace("\\", "/")
    return s.replace(":", "\\:")


def ffmpeg_version(ffmpeg: str) -> str:
    """`ffmpeg -version` 首行（如 `ffmpeg version 8.1.1-full_build-www.gyan.dev …`）。"""
    r = subprocess.run([ffmpeg, "-version"], capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise FFmpegToolError("ffmpeg -version 退出码 %s：%s" % (r.returncode, (r.stderr or "")[:300]))
    line = (r.stdout or "").splitlines()[0] if r.stdout else ""
    return line.strip()


class FFmpegTool:
    """final/camera 段共用的薄封装。"""

    def __init__(self, ffmpeg: str, ffprobe: str):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    @classmethod
    def from_config(cls, final_cfg: dict) -> "FFmpegTool":
        ff, fp = discover(final_cfg)
        return cls(ff, fp)

    def run(self, args: list, check: bool = True):
        """执行 ffmpeg；args 为 `-y -i …` 形式（不含程序名）。"""
        cmd = [self.ffmpeg] + [str(a) for a in args]
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            raise FFmpegToolError("ffmpeg 退出码 %s\n命令: %s\nstderr: %s"
                                  % (r.returncode, " ".join(cmd), (r.stderr or "")[-1500:]))
        return r

    def duration_of(self, path) -> float:
        """ffprobe 取时长（秒）。"""
        r = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", str(path)],
                           capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise FFmpegToolError("ffprobe 失败（%s）：%s" % (path, (r.stderr or "")[:300]))
        try:
            return float(json.loads(r.stdout)["format"]["duration"])
        except (ValueError, KeyError) as e:
            raise FFmpegToolError("ffprobe 无时长（%s）：%s" % (path, e)) from e

    def version(self) -> str:
        return ffmpeg_version(self.ffmpeg)

    def version_tuple(self):
        m = re.search(r"ffmpeg version (\d+)\.(\d+)", self.version())
        if not m:
            raise FFmpegToolError("无法解析 ffmpeg 版本号")
        return int(m.group(1)), int(m.group(2))
