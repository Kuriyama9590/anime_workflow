# -*- coding: utf-8 -*-
"""MiniMax H3 首尾帧图生视频（fl2v）工作流构造器 —— API 格式（dict，可直接 POST /prompt）。

节点结构照搬 _tools/render_videos.py:37-58 的 video_wf（17 节点）：
int8 unet + turbo LoRA + qwen3vl 编码器(minimax clip) + 双 VAE(video/audio)
+ MiniMaxH3ImageToVideo + 采样链 + VAEDecode/VAEDecodeAudio + CreateVideo + SaveVideo。

证据级别（设计文档 docs/FRAMEWORK_DESIGN.md §0/§4.4）：**B 级**——该图在本机从未成功
执行过一次（output/shots/ 为空，videos.log 的唯一 attempt 无结果行）。此处只做"结构
照抄 + 参数注入"，所有参数（LoRA 档位、steps、length、seed、尺寸、fps）由调用方
（pipeline/stages/video.py）从配置注入，本文件不写死任何档位值。

约定（设计文档 §2.1 video 节注）：模型文件入参一律取 **basename**（ComfyUI 按自身
models 目录解析，先例 render_videos.py:39 裸名）；传入相对子路径亦自动取尾名。
"""

from __future__ import annotations

from typing import Any, Dict

# 与全局配置 video: 节一致的文件名缺省值（设计文档 §2.1；模型值在配置里存相对子路径，
# 节点入参用 basename）
DEFAULT_UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
DEFAULT_TEXT_ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
DEFAULT_CLIP_TYPE = "minimax"
DEFAULT_LORA = "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
DEFAULT_VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
DEFAULT_VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"
DEFAULT_SAMPLER = "res_multistep"
DEFAULT_SCHEDULER = "simple"
DEFAULT_FPS = 24.0
DEFAULT_WIDTH = 1344
DEFAULT_HEIGHT = 768


def _basename(name: str) -> str:
    """取模型文件 basename：容忍传入 'diffusion_models/xxx.safetensors' 相对子路径。"""
    return str(name).replace("\\", "/").rsplit("/", 1)[-1]


def build_h3_fl2v(
    *,
    prompt: str,
    seed: int,
    first_image: str,
    last_image: str,
    length: int,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: float = DEFAULT_FPS,
    unet: str = DEFAULT_UNET,
    text_encoder: str = DEFAULT_TEXT_ENCODER,
    clip_type: str = DEFAULT_CLIP_TYPE,
    lora: str = DEFAULT_LORA,
    lora_strength: float = 1.0,
    vae_video: str = DEFAULT_VAE_VIDEO,
    vae_audio: str = DEFAULT_VAE_AUDIO,
    sampler: str = DEFAULT_SAMPLER,
    scheduler: str = DEFAULT_SCHEDULER,
    steps: int = 4,
    denoise: float = 1.0,
    filename_prefix: str = "video/S",
) -> Dict[str, Dict[str, Any]]:
    """构造 H3 fl2v 的 API 格式工作流。

    参数注入点（其余节点结构与 render_videos.py:37-58 逐节点一致）：
      unet/text_encoder/vae_video/vae_audio/lora —— 文件名（自动取 basename）
      clip_type —— CLIPLoader 的 type（minimax）
      steps/denoise —— BasicScheduler
      sampler/scheduler —— KSamplerSelect / BasicScheduler
      seed —— RandomNoise.noise_seed
      width/height/length —— MiniMaxH3ImageToVideo
      fps —— CreateVideo
      filename_prefix —— SaveVideo
    """
    if length <= 0:
        raise ValueError("length 必须为正整数（帧数），got %r" % (length,))
    if steps <= 0:
        raise ValueError("steps 必须为正整数，got %r" % (steps,))
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": _basename(unet), "weight_dtype": "default"}},
        "2": {"class_type": "LoraLoaderModelOnly",
              "inputs": {"model": ["1", 0], "lora_name": _basename(lora),
                         "strength_model": float(lora_strength)}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": _basename(text_encoder), "type": clip_type,
                         "device": "default"}},
        "4": {"class_type": "VAELoader",
              "inputs": {"vae_name": _basename(vae_video)}},
        "5": {"class_type": "VAELoader",
              "inputs": {"vae_name": _basename(vae_audio)}},
        "6": {"class_type": "LoadImage", "inputs": {"image": first_image}},
        "7": {"class_type": "LoadImage", "inputs": {"image": last_image}},
        "8": {"class_type": "MiniMaxH3ImageToVideo",
              "inputs": {"clip": ["3", 0], "vae": ["4", 0],
                         "first_frame": ["6", 0], "last_frame": ["7", 0],
                         "prompt": prompt,
                         "width": int(width), "height": int(height),
                         "length": int(length)}},
        "9": {"class_type": "BasicGuider",
              "inputs": {"model": ["2", 0], "conditioning": ["8", 0]}},
        "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler}},
        "11": {"class_type": "BasicScheduler",
               "inputs": {"model": ["2", 0], "scheduler": scheduler,
                          "steps": int(steps), "denoise": float(denoise)}},
        "12": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "13": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["12", 0], "guider": ["9", 0], "sampler": ["10", 0],
                          "sigmas": ["11", 0], "latent_image": ["8", 1]}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
        "15": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["13", 0], "vae": ["5", 0]}},
        "16": {"class_type": "CreateVideo",
               "inputs": {"images": ["14", 0], "audio": ["15", 0], "fps": float(fps)}},
        "17": {"class_type": "SaveVideo",
               "inputs": {"video": ["16", 0], "filename_prefix": filename_prefix,
                          "format": "auto", "codec": "auto"}},
    }


# UI 格式（sakura_t2i_ref.json 那种顶层 nodes/links 数组）不可直提 /prompt，
# 此常量供校验器识别 UI 残留字段（设计文档 §0 / §7 检查 7）。
_UI_FORMAT_KEYS = frozenset({"nodes", "links", "groups", "config", "extra", "version"})


def validate_api_workflow(wf: Dict[str, Dict[str, Any]]) -> None:
    """校验 API 格式工作流的合法性（selftest 检查 7 的判定核心）：

    1. 顶层是 dict[str, dict]；
    2. 每个节点 class_type 为非空字符串；
    3. 所有 ``[node_id, slot]`` 引用的节点存在且 slot 为非负 int；
    4. 无 UI 格式残留字段（nodes/links/groups/…）。

    不合法直接 raise ValueError（信息含节点 id，报错可读）。
    """
    if not isinstance(wf, dict) or not wf:
        raise ValueError("工作流须为非空 dict（API 格式），got %s" % type(wf).__name__)
    leaked = _UI_FORMAT_KEYS & set(wf.keys())
    if leaked:
        raise ValueError("检测到 UI 格式残留字段 %s——API 格式顶层键应为节点 id" % sorted(leaked))
    for nid, node in wf.items():
        if not isinstance(node, dict):
            raise ValueError("节点 %s 须为 dict，got %s" % (nid, type(node).__name__))
        ct = node.get("class_type")
        if not isinstance(ct, str) or not ct.strip():
            raise ValueError("节点 %s 缺少非空 class_type" % nid)
        for key, val in node.get("inputs", {}).items():
            if (isinstance(val, list) and len(val) == 2
                    and isinstance(val[0], str) and isinstance(val[1], int)):
                ref, slot = val
                if ref not in wf:
                    raise ValueError("节点 %s 的输入 %s 引用了不存在的节点 %r"
                                     % (nid, key, ref))
                if slot < 0:
                    raise ValueError("节点 %s 的输入 %s slot 为负: %r" % (nid, key, slot))
