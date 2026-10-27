# -*- coding: utf-8 -*-
"""seed 派生规则（全段统一，docs §3 规则 4——杜绝实现者各自发明）。

一律从 project.yaml 的 seed_base 出发；yaml 里写了显式 seed（>0）的 unit 用显式值。
派生值与实际值都必须写入 manifest（stages 负责记录）。

    段             公式
    assets         seed_base + 1_000_000 + 1000×角色序 + 10×资产序；三视图第 i 候选再 +(i-1)
    keyframes 首帧  seed_base + 10×镜序（镜序从 1 起）
    keyframes 末帧  edit: 首帧 seed +5；fresh: +7（render_keyframes.py:133,140 的 A 级先例）
    video          seed_base + 2_000_000 + 镜序
    camera 模式A   无 seed（Blender 渲染确定性）
"""
from __future__ import annotations

__all__ = ["asset_seed", "kf_first_seed", "kf_last_seed", "video_seed", "resolve_seed"]


def asset_seed(seed_base: int, char_index: int, asset_index: int, candidate: int = 1) -> int:
    """角色序/资产序均从 1 起；candidate 从 1 起（候选即 seed、seed+1…）。"""
    return int(seed_base) + 1_000_000 + 1000 * int(char_index) + 10 * int(asset_index) + (int(candidate) - 1)


def kf_first_seed(seed_base: int, shot_index: int) -> int:
    return int(seed_base) + 10 * int(shot_index)


def kf_last_seed(seed_base: int, shot_index: int, mode: str = "edit") -> int:
    delta = {"edit": 5, "fresh": 7}.get(str(mode).lower(), 5)
    return kf_first_seed(seed_base, shot_index) + delta


def video_seed(seed_base: int, shot_index: int) -> int:
    return int(seed_base) + 2_000_000 + int(shot_index)


def resolve_seed(explicit, derived: int) -> int:
    """显式 seed（>0）优先；0/None/负值 = 按 §3 规则 4 派生。"""
    try:
        e = int(explicit)
    except (TypeError, ValueError):
        return derived
    return e if e > 0 else derived
