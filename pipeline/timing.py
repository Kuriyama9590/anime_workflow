# -*- coding: utf-8 -*-
"""H3 帧长网格：duration_s → length 三步命中规则（docs §4.4，网格常数只在 config video 节）。

① 请求校验：duration_s ∈ [min_request_s, max_request_s]=[5.0, 15.0]，越界即配置错误；
② 映射：k = round((d×fps − b)/a)，夹取 [k_min, k_max]=[7, 21]，length = a×k+b；
③ 容差断言：|eff − d| ≤ a/fps/2（half_step ≈ 0.354s），超限显式报错并列出最近两个网格点。

"124 帧 ≈ 5.17s"是设计值不是验证值——本机从未成功执行过任何 H3 渲染；
首跑标定必须包含 5.17s/124 帧本身；网格不符改 config 一处即可，代码不写死。
"""
from __future__ import annotations

__all__ = ["GridError", "map_duration", "sweep_grid"]


class GridError(ValueError):
    pass


def map_duration(duration_s: float, video_cfg: dict) -> dict:
    v = video_cfg or {}
    fps = float(v["fps"])
    a = int(v["length_grid"]["a"])
    b = int(v["length_grid"]["b"])
    k_min, k_max = int(v["k_min"]), int(v["k_max"])
    lo, hi = float(v["min_request_s"]), float(v["max_request_s"])
    tol_mode = v.get("duration_tolerance", "half_step")

    if not (lo <= duration_s <= hi):
        raise GridError("duration_s=%s 超出请求域 [%s, %s]（yaml/defaults 配置错误）"
                        % (duration_s, lo, hi))
    k = round((duration_s * fps - b) / a)
    clamped = k
    if k < k_min:
        clamped = k_min
    elif k > k_max:
        clamped = k_max
    length = a * clamped + b
    eff = length / fps
    tol = (a / fps / 2) if tol_mode == "half_step" else float(tol_mode)
    if abs(eff - duration_s) > tol + 1e-9:
        near = sorted(range(max(k_min, clamped - 1), min(k_max, clamped + 1) + 1),
                      key=lambda kk: abs((a * kk + b) / fps - duration_s))[:2]
        pts = "; ".join("k=%d → length=%d（%.3fs，误差 %.3fs）"
                        % (kk, a * kk + b, (a * kk + b) / fps,
                           abs((a * kk + b) / fps - duration_s)) for kk in near)
        raise GridError("duration_s=%s 夹取后仍超半步容差（|%.3fs| > %.3fs）；最近网格点：%s"
                        % (duration_s, abs(eff - duration_s), tol, pts))
    return {"k": clamped, "length": length, "eff_s": eff, "fps": fps,
            "tolerance_s": tol, "clamped": clamped != k}


def sweep_grid(video_cfg: dict, step: float = 0.5) -> list:
    """min→max 逐 step 扫描（selftest 第 8 项：5.0–15.0 共 21 点须全部命中）。"""
    lo = float(video_cfg["min_request_s"])
    hi = float(video_cfg["max_request_s"])
    out = []
    n = int(round((hi - lo) / step))
    for i in range(n + 1):
        d = round(lo + i * step, 6)
        out.append((d, map_duration(d, video_cfg)))
    return out
