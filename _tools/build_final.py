# -*- coding: utf-8 -*-
"""Assemble 《雨落无声》 final video: concat rendered shots, generate SRT, burn CN subtitles, title + end card."""
import os, json, subprocess, sys

ROOT = r"E:\projects\推理\R18_video"
SHOTS = os.path.join(ROOT, "output", "shots")
OUT = os.path.join(ROOT, "output")
FF = r"C:\Users\23817\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
FFPROBE = os.path.join(os.path.dirname(FF), "ffprobe.exe")
FONT = "C\\:/Windows/Fonts/msyh.ttc"

# (shot, in-shot start, in-shot end, text)
CUES = [
    ("S3", 2.6, 4.9, "抄近路吧。"),
    ("S4", 0.7, 4.8, "手腕被攥住的那一秒,雨声忽然变得很响。"),
    ("S6", 0.9, 4.6, "“放开……不要……”"),
    ("S7", 0.7, 4.8, "那五分钟,整条巷子都在替我出声。"),
]

def dur(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                       capture_output=True, text=True)
    return float(json.loads(r.stdout)["format"]["duration"])

def srt_time(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return "%02d:%02d:%06.3f" % (h, m, s)

def main():
    order = ["S%d" % i for i in range(1, 11)]
    files = [(sid, os.path.join(SHOTS, sid + ".mp4")) for sid in order]
    files = [(sid, p) for sid, p in files if os.path.exists(p) and os.path.getsize(p) > 200000]
    missing = [sid for sid, _ in order if sid not in dict(files)]
    if missing:
        print("WARN missing shots:", missing)
    # concat list
    lst = os.path.join(OUT, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for sid, p in files:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    merged = os.path.join(OUT, "_merged.mp4")
    subprocess.run([FF, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264", "-crf", "18",
                    "-preset", "medium", "-c:a", "aac", "-b:a", "192k", merged], check=True)
    # per-shot offsets
    off, offs = 0.0, {}
    for sid, p in files:
        offs[sid] = off
        off += dur(p)
    total = off
    lines = []
    n = 1
    def cue(t0, t1, text):
        nonlocal n
        lines.append("%d\n%s --> %s\n%s\n" % (n, srt_time(max(0, t0)), srt_time(min(total, t1)), text))
        n += 1
    cue(0.9, 3.0, "六月十七日,梅雨。晚自习放学。")
    for sid, a, b, text in CUES:
        if sid in offs:
            cue(offs[sid] + a, offs[sid] + b, text)
    if files:
        last = offs[files[-1][0]]
        cue(total - 4.4, total - 0.6, "—— 她回家吃了饭,谁也没告诉。")
    srt = os.path.join(OUT, "subtitles.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    final = os.path.join(OUT, "ep01_rainfall_silent.mp4")
    style = ("FontName=Microsoft YaHei&FontSize=16&PrimaryColour=&H00F5F5F5&OutlineColour=&H80000000"
             "&BorderStyle=1&Outline=1&Shadow=0&Alignment=2&MarginV=34")
    title = ("drawtext=fontfile='%s':text='雨 落 无 声':fontsize=58:fontcolor=white:borderw=2:bordercolor=black@0.5"
             ":x=(w-text_w)/2:y=(h-text_h)/2:alpha='if(lt(t,0.5),0,if(lt(t,1.1),(t-0.5)/0.6,if(lt(t,3.4),1,if(lt(t,4.0),(4.0-t)/0.6,0))))'" % FONT)
    sub = "subtitles=filename='%s':force_style='%s'" % (srt.replace("\\", "/").replace(":", "\\:"), style)
    vf = "%s,%s,fade=t=in:st=0:d=0.4" % (title, sub)
    subprocess.run([FF, "-y", "-i", merged, "-vf", vf, "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                    "-c:a", "copy", final], check=True)
    print("FINAL:", final, "total %.2fs" % total)

if __name__ == "__main__":
    main()
