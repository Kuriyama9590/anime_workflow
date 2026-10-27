# -*- coding: utf-8 -*-
"""工作流构造器统一入口（设计文档 §3 规则 2）。

惰性分发：调用时才 import 具体构造器子模块——六模块并行构建期间，
photo_sakura / anime_sdxl 等文件可能晚于本文件落地，惰性 import 使
本入口对"文件后到"自愈，不需要回头改这里。

stages 层应通过本入口取图（不感知具体节点图）；需要 H3 全参控制时
也可直接 from pipeline.workflows.h3_fl2v import build_h3_fl2v。
"""

from __future__ import annotations

from typing import Any, Dict

_KNOWN_CHANNELS = ("photo", "anime")


def build_h3(**kwargs: Any) -> Dict[str, Dict[str, Any]]:
    """MiniMax H3 首尾帧图生视频（fl2v）—— 参数见 h3_fl2v.build_h3_fl2v。"""
    from .h3_fl2v import build_h3_fl2v
    return build_h3_fl2v(**kwargs)


def build_t2i(channel: str, **kwargs: Any) -> Dict[str, Dict[str, Any]]:
    """按通道分派 t2i 构造器（photo=Sakura / anime=waiIllustrious SDXL）。"""
    if channel == "photo":
        from .photo_sakura import build_t2i as f
        return f(**kwargs)
    if channel == "anime":
        from .anime_sdxl import build_t2i as f
        return f(**kwargs)
    raise ValueError("未知通道 %r（已知：%s）" % (channel, "/".join(_KNOWN_CHANNELS)))


def build_edit(channel: str, **kwargs: Any) -> Dict[str, Dict[str, Any]]:
    """按通道分派保身份改画构造器（photo=edit_wf / anime=img2img）。"""
    if channel == "photo":
        from .photo_sakura import build_edit as f
        return f(**kwargs)
    if channel == "anime":
        from .anime_sdxl import build_edit as f
        return f(**kwargs)
    raise ValueError("未知通道 %r（已知：%s）" % (channel, "/".join(_KNOWN_CHANNELS)))


def validate_api_workflow(wf) -> None:
    """API 格式工作流合法性校验（实现见 h3_fl2v，供各通道复用）。"""
    from .h3_fl2v import validate_api_workflow as _v
    _v(wf)
