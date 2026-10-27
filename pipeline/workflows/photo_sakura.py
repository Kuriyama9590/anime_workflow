# -*- coding: utf-8 -*-
"""photo 通道（Sakura-Image-2.1-Turbo）API 工作流构造器 —— A 级已跑通路径的参数化移植。

来源与证据（_tools/ 只读参考，docs/FRAMEWORK_DESIGN.md §0 映射表）：
- t2i 图 = _tools/comfy_client.py:60-81 ``img_wf`` —— 《雨落无声》20 张关键帧批次全 OK（A 级）。
  注意：img_wf 的 ref_images 分支（comfy_client.py:65-67）三处调用从未传参、零运行证据，
  按设计 §4.2 禁止移植；参考图一律走本模块 ``build_edit`` 的 edit_wf 图形。
- 保身份改画图 = _tools/render_keyframes.py:64-80 ``edit_wf`` —— 同批次全 OK（A 级）。
  latent 取 TextEncodeQwenImage21 输出 slot 2（``["4", 2]``）；ImageScale 宽高 0 = 原样透传。

本模块只做纯函数构造：不读配置、不落盘、不发网络请求。模型名/采样参数由调用方以
``params`` 注入（来自 config ``channels.photo`` 节）；节点入参自动取 basename（设计 §2.1
注：ComfyUI 按自身目录解析，先例 render_videos.py:39 裸名）。
"""
from __future__ import annotations

SIGMAS_TURBO_8STEP = (
    "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0"
)

#: 默认参数 = docs/FRAMEWORK_DESIGN.md §2.1 channels.photo 节的镜像（config 模块落地前的回退值）。
DEFAULT_PARAMS = {
    "unet_gguf": "diffusion_models/Sakura-Image-2.1-Turbo-Q5_K_M-4.60GiB.gguf",
    "clip": "text_encoders/Sakura-TextEncoder-Qwen3VL-8B-Uncensored-bf16.safetensors",
    "vae": "vae/qwen_image_2.1_vae_bf16.safetensors",
    "clip_type": "qwen_image",
    "resolution": 1024,
    "width": 1344,
    "height": 768,
    "sampler": "euler",
    "sigmas": SIGMAS_TURBO_8STEP,
    "cfg": 1.0,
}


def _basename(value) -> str:
    """相对子路径 → ComfyUI 节点入参用的裸文件名（设计 §2.1 注）。"""
    return str(value).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _merged(params) -> dict:
    merged = dict(DEFAULT_PARAMS)
    if params:
        for k, v in params.items():
            if v is not None:
                merged[k] = v
    return merged


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError("photo_sakura: " + msg)


def build_t2i(prompt, negative, seed, width, height, filename_prefix, *,
              params=None, resolution=None):
    """txt2img 工作流（= img_wf，无参考图分支）。返回 API 格式 dict[str, node]。

    参数校验失败抛 ValueError（信息含参数名，便于定位）。
    """
    p = _merged(params)
    _require(isinstance(prompt, str) and prompt.strip(), "prompt 必须是非空字符串")
    _require(isinstance(negative, str), "negative 必须是字符串")
    _require(isinstance(seed, int) and not isinstance(seed, bool), "seed 必须是 int")
    width, height = int(width), int(height)
    _require(width > 0 and height > 0, f"width/height 必须为正数，得到 {width}x{height}")
    _require(isinstance(filename_prefix, str) and filename_prefix.strip(),
             "filename_prefix 必须是非空字符串")
    res = int(resolution if resolution is not None else p["resolution"])
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": _basename(p["unet_gguf"])}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": _basename(p["clip"]), "type": p["clip_type"], "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _basename(p["vae"])}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": prompt, "negative_prompt": negative,
            "resolution": res, "vae": ["3", 0]}},
        "5": {"class_type": "ManualSigmas", "inputs": {"sigmas": str(p["sigmas"])}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": str(p["sampler"])}},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "8": {"class_type": "CFGGuider", "inputs": {
            "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "cfg": float(p["cfg"])}},
        "13": {"class_type": "EmptyQwenImage21Latent", "inputs": {
            "width": width, "height": height, "batch_size": 1}},
        "9": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["7", 0], "guider": ["8", 0], "sampler": ["6", 0],
            "sigmas": ["5", 0], "latent_image": ["13", 0]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {
            "images": ["10", 0], "filename_prefix": filename_prefix}},
    }


def build_edit(ref_image_name, prompt, negative, seed, filename_prefix, *,
               params=None, resolution=None, ref_width=0, ref_height=0):
    """保身份改画工作流（= edit_wf）。

    ``ref_image_name`` 是已复制进两个 input_dirs 的参考图文件名（调用方职责，设计 §4.2：
    先 makedirs 再复制）。输出尺寸跟随参考图与 resolution，故本图无 width/height 入参。
    """
    p = _merged(params)
    _require(isinstance(ref_image_name, str) and ref_image_name.strip(),
             "ref_image_name 必须是非空字符串（LoadImage 需要文件名而非路径）")
    _require(isinstance(prompt, str) and prompt.strip(), "prompt 必须是非空字符串")
    _require(isinstance(negative, str), "negative 必须是字符串")
    _require(isinstance(seed, int) and not isinstance(seed, bool), "seed 必须是 int")
    _require(isinstance(filename_prefix, str) and filename_prefix.strip(),
             "filename_prefix 必须是非空字符串")
    res = int(resolution if resolution is not None else p["resolution"])
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": _basename(p["unet_gguf"])}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": _basename(p["clip"]), "type": p["clip_type"], "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _basename(p["vae"])}},
        "12": {"class_type": "LoadImage", "inputs": {"image": ref_image_name}},
        "14": {"class_type": "ImageScale", "inputs": {
            "image": ["12", 0], "upscale_method": "lanczos",
            "width": int(ref_width), "height": int(ref_height), "crop": "center"}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": prompt, "negative_prompt": negative,
            "resolution": res, "vae": ["3", 0], "images.image_1": ["14", 0]}},
        "5": {"class_type": "ManualSigmas", "inputs": {"sigmas": str(p["sigmas"])}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": str(p["sampler"])}},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "8": {"class_type": "CFGGuider", "inputs": {
            "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "cfg": float(p["cfg"])}},
        "9": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["7", 0], "guider": ["8", 0], "sampler": ["6", 0],
            "sigmas": ["5", 0], "latent_image": ["4", 2]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {
            "images": ["10", 0], "filename_prefix": filename_prefix}},
    }
