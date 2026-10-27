# -*- coding: utf-8 -*-
"""camera 模块（模式A Blender 运镜 / 模式B 运镜提示词词库直通 H3）。

模块组成：
  preset_moves  参数化运镜轨迹（推/拉/摇/移/跟/环绕/2.5D 视差，纯 Python 可离线单测）
  codegen       spec JSON → 可独立运行的 bpy 脚本（细分位移平面/相机动画/EEVEE 双枚举）
  run_camera    CLI（modeA 渲帧→ffmpeg；modeB camdesc；vocab；install）
  vocab         模式B 词库（vocab.yaml）+ 受限 YAML 子集解析 + shots.yaml 批处理
  llm_bpy       可选 OpenAI 兼容 LLM 生成 bpy（未配置/失败回退预设；默认只落盘待审）
"""

from pipeline.camera.codegen import generate_bpy, marker
from pipeline.camera.preset_moves import (
    MOVES,
    DEFAULT_PARAMS,
    EASINGS,
    build_track,
    make_spec,
    plane_half_bounds,
)
from pipeline.camera.vocab import build_camdesc, load_vocab

__all__ = [
    "MOVES", "DEFAULT_PARAMS", "EASINGS",
    "build_track", "make_spec", "plane_half_bounds",
    "generate_bpy", "marker",
    "load_vocab", "build_camdesc",
]
