# -*- coding: utf-8 -*-
"""anime 通道（waiIllustriousSDXL_v170）API 工作流构造器 —— C 级·设计值，待首跑标定。

证据分级（docs/FRAMEWORK_DESIGN.md §3）：权重在盘但 _tools 从未引用（C 级）。
图为标准 SDXL 结构（CheckpointLoaderSimple + CLIPTextEncode + KSampler）：
- t2i：EmptyLatentImage 初始 latent；
- 保身份改画 = img2img：首帧 VAEEncode 为初始 latent，denoise 取 config
  ``channels.anime.img2img_denoise``（设计初值 0.55，需 3-5 张标定后回写 config）。

节点入参取 checkpoint 的 basename（同 photo_sakura 约定）。本模块只做纯函数构造。
"""
from __future__ import annotations

#: 默认参数 = docs/FRAMEWORK_DESIGN.md §2.1 channels.anime 节的镜像。
DEFAULT_PARAMS = {
    "checkpoint": "checkpoints/waiIllustriousSDXL_v170.safetensors",
    "width": 1344,
    "height": 768,
    "steps": 28,
    "cfg": 5.0,
    "sampler": "euler_ancestral",
    "scheduler": "normal",
    "img2img_denoise": 0.55,
    "negative": "photo, 3d, realistic, text, watermark, bad anatomy, extra fingers, lowres",
}


def _basename(value) -> str:
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
        raise ValueError("anime_sdxl: " + msg)


def _encode_nodes(p, prompt, negative):
    ckpt = _basename(p["checkpoint"])
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": prompt}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": negative}},
    }


def build_t2i(prompt, negative, seed, width, height, filename_prefix, *,
              params=None, resolution=None):
    """SDXL txt2img（设计值）。``resolution`` 为接口同构保留，本通道不使用。"""
    p = _merged(params)
    _require(isinstance(prompt, str) and prompt.strip(), "prompt 必须是非空字符串")
    _require(isinstance(negative, str), "negative 必须是字符串")
    _require(isinstance(seed, int) and not isinstance(seed, bool), "seed 必须是 int")
    width, height = int(width), int(height)
    _require(width > 0 and height > 0, f"width/height 必须为正数，得到 {width}x{height}")
    _require(isinstance(filename_prefix, str) and filename_prefix.strip(),
             "filename_prefix 必须是非空字符串")
    wf = _encode_nodes(p, prompt, negative)
    wf.update({
        "4": {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
            "latent_image": ["4", 0], "seed": int(seed), "steps": int(p["steps"]),
            "cfg": float(p["cfg"]), "sampler_name": str(p["sampler"]),
            "scheduler": str(p["scheduler"]), "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {
            "images": ["6", 0], "filename_prefix": filename_prefix}},
    })
    return wf


def build_edit(ref_image_name, prompt, negative, seed, filename_prefix, *,
               params=None, resolution=None, ref_width=0, ref_height=0):
    """SDXL img2img 保身份改画（设计值，denoise 待标定）。

    首帧图经 LoadImage → VAEEncode 为初始 latent，输出尺寸跟随参考图；
    ``resolution``/``ref_width``/``ref_height`` 为接口同构保留，本通道不使用。
    """
    p = _merged(params)
    _require(isinstance(ref_image_name, str) and ref_image_name.strip(),
             "ref_image_name 必须是非空字符串（LoadImage 需要文件名而非路径）")
    _require(isinstance(prompt, str) and prompt.strip(), "prompt 必须是非空字符串")
    _require(isinstance(negative, str), "negative 必须是字符串")
    _require(isinstance(seed, int) and not isinstance(seed, bool), "seed 必须是 int")
    _require(isinstance(filename_prefix, str) and filename_prefix.strip(),
             "filename_prefix 必须是非空字符串")
    wf = _encode_nodes(p, prompt, negative)
    wf.update({
        "8": {"class_type": "LoadImage", "inputs": {"image": ref_image_name}},
        "9": {"class_type": "VAEEncode", "inputs": {"pixels": ["8", 0], "vae": ["1", 2]}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
            "latent_image": ["9", 0], "seed": int(seed), "steps": int(p["steps"]),
            "cfg": float(p["cfg"]), "sampler_name": str(p["sampler"]),
            "scheduler": str(p["scheduler"]), "denoise": float(p["img2img_denoise"])}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {
            "images": ["6", 0], "filename_prefix": filename_prefix}},
    })
    return wf
