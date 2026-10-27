# -*- coding: utf-8 -*-
"""screenplay 段0 公共：时长网格边界、预算可行性、剧本预算行解析、内容边界默认。

时长/预算的权威常数在全局 config ``video:`` 节（fps/length_grid/k_min/k_max/
min_request_s/max_request_s，docs §4.4）；本模块只做只读推导，不复制实现
（映射本身归 pipeline/timing.py，这里取边界与建议值即可）。
"""
from __future__ import annotations

import re

__all__ = ["ASCII_ID_RE", "BudgetError", "CONTENT_BOUNDARY_DEFAULT",
           "content_boundary", "grid_bounds", "feasibility", "recommend_duration",
           "parse_budget_line", "camera_vocab_names", "fmt_secs"]

#: 文件名安全 id（与 schema._ASCII_ID_RE 同口径；breakdown 的 props/expressions 也用它）
ASCII_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

#: 剧本元信息里的机器可读预算行：`- 预算: 4 镜 × 30s`（×/x/* 均接受）
_BUDGET_LINE_RE = re.compile(r"预算[：:]\s*(\d+)\s*镜\s*[×x*]\s*([0-9]+(?:\.[0-9]+)?)\s*s")

CONTENT_BOUNDARY_DEFAULT = (
    "内容边界（框架默认）：模板与生成内容默认中性叙事，全体角色一律为成年人（18+）。"
    "项目尺度由 project.yaml 的 screenplay.content_rating 自行声明；未声明时按本默认执行，"
    "且无论声明为何，角色一律为成年人（18+）。")


class BudgetError(ValueError):
    """预算不可行（消息面向用户与 LLM 反馈均需可读）。"""


def content_boundary(project_meta: dict | None) -> str:
    """项目声明了 screenplay.content_rating 时按声明表述；否则用框架默认。"""
    meta = project_meta if isinstance(project_meta, dict) else {}
    sp = meta.get("screenplay")
    rating = ""
    if isinstance(sp, dict):
        rating = str(sp.get("content_rating") or "").strip()
    if rating:
        return ("内容边界（项目声明）：本项目尺度由 project.yaml 声明为「%s」。"
                "无论声明为何，全体角色一律为成年人（18+）。" % rating)
    return CONTENT_BOUNDARY_DEFAULT


def grid_bounds(video_cfg: dict | None) -> dict:
    """video 节的边界推导（缺节时按 docs §4.4 设计值兜底，便于独立调用）。"""
    v = video_cfg or {}
    lg = v.get("length_grid") or {}
    try:
        fps = float(v.get("fps", 24))
        a = int(lg.get("a", 17))
        b = int(lg.get("b", 5))
        k_min = int(v.get("k_min", 7))
        k_max = int(v.get("k_max", 21))
        lo = float(v.get("min_request_s", 5.0))
        hi = float(v.get("max_request_s", 15.0))
        default_s = float(v.get("default_duration_s", 5.17))
    except (TypeError, ValueError) as e:
        raise BudgetError("video 配置的时长网格参数非法：%s" % e)
    if fps <= 0 or a <= 0 or b < 0 or not (0 < k_min < k_max):
        raise BudgetError("video 配置的时长网格参数越界（fps/a/b/k_min/k_max）")
    return {"fps": fps, "a": a, "b": b, "k_min": k_min, "k_max": k_max,
            "step_s": a / fps,
            "min_eff": (a * k_min + b) / fps, "max_eff": (a * k_max + b) / fps,
            "min_request_s": lo, "max_request_s": hi, "default_s": default_s}


def feasibility(n_shots: int, total_s: float, g: dict) -> None:
    """镜数 × 时长预算 vs 网格可达域（±1 步容差）；不可行抛 BudgetError。"""
    if n_shots <= 0:
        raise BudgetError("镜数须为正整数，得到 %r" % (n_shots,))
    if total_s <= 0:
        raise BudgetError("总时长预算须为正数（秒），得到 %r" % (total_s,))
    slack = g["step_s"]
    if total_s < n_shots * g["min_eff"] - slack:
        raise BudgetError(
            "预算不可行：%d 镜 × 最短 %.2fs ≈ %.2fs 超出预算 %.2fs——"
            "减少镜数（该预算最多 %d 镜）或提高 --total-s"
            % (n_shots, g["min_eff"], n_shots * g["min_eff"], total_s,
               int((total_s + slack) / g["min_eff"])))
    if total_s > n_shots * g["max_eff"] + slack:
        raise BudgetError(
            "预算异常宽松：%d 镜最长 %.2fs ≈ %.2fs，预算 %.2fs 远超上限——"
            "镜数偏少或 --total-s 单位有误（应为秒）"
            % (n_shots, g["max_eff"], n_shots * g["max_eff"], total_s))


def recommend_duration(n_shots: int, total_s: float, g: dict) -> float:
    """均摊预算 → 网格命中后的建议每镜时长（请求域内的可写值，2 位小数）。"""
    per = min(max(total_s / n_shots, g["min_eff"]), g["max_eff"])
    per = min(per, g["max_request_s"])
    k = int(round((per * g["fps"] - g["b"]) / g["a"]))
    k = min(max(k, g["k_min"]), g["k_max"])
    eff = (g["a"] * k + g["b"]) / g["fps"]
    return round(min(eff, g["max_request_s"]), 2)


def parse_budget_line(script_text: str) -> tuple[int, float]:
    """从剧本头部（前 80 行）解析预算行 → (镜数, 总秒)；无则 (0, 0.0)。"""
    if not script_text:
        return (0, 0.0)
    for line in script_text.splitlines()[:80]:
        m = _BUDGET_LINE_RE.search(line)
        if m:
            try:
                return int(m.group(1)), float(m.group(2))
            except ValueError:
                return (0, 0.0)
    return (0, 0.0)


def camera_vocab_names() -> tuple[list[str], list[str]]:
    """运镜词库 (moves, intensities)——单一事实源 pipeline/camera/vocab.yaml；
    词库不可用时按其现行内容兜底（仅影响提示词文案，不放宽校验）。"""
    try:
        from ..camera import vocab
        v = vocab.load_vocab()
        moves = sorted((v.get("moves") or {}).keys())
        intensities = sorted(set(v.get("intensities") or [])
                             | {i for e in (v.get("moves") or {}).values() for i in e})
        if moves and intensities:
            return moves, intensities
    except Exception:  # noqa: BLE001 —— 词库缺失不阻塞 screenplay（提示词用兜底清单）
        pass
    return (["follow", "locked", "orbit", "pan", "parallax", "pull_out",
             "push_in", "truck"], ["fast", "medium", "slow"])


def fmt_secs(v: float) -> str:
    """预算行/日志用的秒数格式（30.0→30，7.29→7.29）。"""
    return ("%g" % float(v))
