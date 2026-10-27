# -*- coding: utf-8 -*-
"""Render 《雨落无声》 shots with local MiniMax H3 (first+last frame -> video, turbo LoRA).
Waits for weights + keyframes, then renders shots sequentially, resumable.
Usage: python render_videos.py [--force] [S1 S2 ...]
"""
import sys, os, json, time, shutil, traceback
sys.path.insert(0, r"E:\projects\推理\R18_video\_tools")
import comfy_client as cc

ROOT     = r"E:\projects\推理\R18_video"
KF_DIR   = os.path.join(ROOT, "output", "keyframes")
SHOT_DIR = os.path.join(ROOT, "output", "shots")
MODELS   = r"E:\SD-WEBUI-AKI-V5.0\ComfyUI_windows_portable\ComfyUI\models"
CUI_IN   = [r"E:\SD-WEBUI-AKI-V5.0\ComfyUI_windows_portable\ComfyUI\user\default\input",
            r"E:\SD-WEBUI-AKI-V5.0\ComfyUI_windows_portable\ComfyUI\input"]
LOG      = os.path.join(ROOT, "_tools", "videos.log")

UNET  = os.path.join(MODELS, "diffusion_models", "minimax_h3_fl2va_pruned_int8_convrot.safetensors")
TE    = os.path.join(MODELS, "text_encoders", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
LORA4 = os.path.join(MODELS, "loras", "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors")
LORA8 = os.path.join(MODELS, "loras", "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors")

def log(m):
    line = "%s %s" % (time.strftime("%H:%M:%S"), m)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def ready(p, gb=1.0):
    return os.path.exists(p) and os.path.getsize(p) > gb * 1024**3

def pick_lora():
    if ready(LORA4, 1.5):
        return os.path.basename(LORA4), 4
    return os.path.basename(LORA8), 8

def video_wf(first_img, last_img, prompt, seed, lora, steps):
    return {
        "1":  {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2":  {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": lora, "strength_model": 1.0}},
        "3":  {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "4":  {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "5":  {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "6":  {"class_type": "LoadImage", "inputs": {"image": first_img}},
        "7":  {"class_type": "LoadImage", "inputs": {"image": last_img}},
        "8":  {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"clip": ["3", 0], "vae": ["4", 0],
               "first_frame": ["6", 0], "last_frame": ["7", 0], "prompt": prompt,
               "width": 1344, "height": 768, "length": 124}},
        "9":  {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["8", 0]}},
        "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "11": {"class_type": "BasicScheduler", "inputs": {"model": ["2", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}},
        "12": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "13": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["12", 0], "guider": ["9", 0], "sampler": ["10", 0], "sigmas": ["11", 0], "latent_image": ["8", 1]}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
        "15": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["13", 0], "vae": ["5", 0]}},
        "16": {"class_type": "CreateVideo", "inputs": {"images": ["14", 0], "audio": ["15", 0], "fps": 24.0}},
        "17": {"class_type": "SaveVideo", "inputs": {"video": ["16", 0], "filename_prefix": "R18/shots/%s" % first_img.split("_")[0], "format": "auto", "codec": "auto"}},
    }

def download_media(entry, dest):
    for nid, node in entry.get("outputs", {}).items():
        for key in ("videos", "images", "gifs", "audio"):
            for item in node.get(key, []):
                if isinstance(item, dict) and "filename" in item and item["filename"].lower().endswith((".mp4", ".webm", ".mkv", ".mov")):
                    import urllib.parse
                    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")})
                    raw = cc._get(cc.SERVER + "/view?" + q)
                    with open(dest, "wb") as f:
                        f.write(raw)
                    return dest
    return None

def put_input(src, name):
    for d in CUI_IN:
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(src, os.path.join(d, name))

def main():
    force = "--force" in sys.argv
    only = [a for a in sys.argv[1:] if a.upper().startswith("S") and a[1:].isdigit()]
    shots = json.load(open(os.path.join(ROOT, "shots_video.json"), encoding="utf-8"))
    os.makedirs(SHOT_DIR, exist_ok=True)

    # 1) wait for weights
    while not (ready(UNET, 5.0) and ready(TE, 5.0)):
        log("waiting weights... unet=%s te=%s" % (ready(UNET, 5.0), ready(TE, 5.0)))
        time.sleep(30)
    lora, steps = pick_lora()
    log("weights ready. lora=%s steps=%d" % (lora, steps))

    # 2) wait for keyframes
    need = []
    ids = only if only else sorted(shots.keys(), key=lambda s: int(s[1:]))
    for sid in ids:
        for kind in ("first", "last"):
            p = os.path.join(KF_DIR, "%s_%s.png" % (sid, kind))
            need.append(p)
    while True:
        missing = [p for p in need if not os.path.exists(p)]
        if not missing:
            break
        log("waiting keyframes: %d missing" % len(missing))
        time.sleep(30)
    log("keyframes ready")

    # 3) render
    for sid in ids:
        out = os.path.join(SHOT_DIR, "%s.mp4" % sid)
        if os.path.exists(out) and os.path.getsize(out) > 200000 and not force:
            log("%s exists, skip" % sid)
            continue
        s = shots[sid]
        fi, li = "%s_in.png" % sid, "%s_in_last.png" % sid
        put_input(os.path.join(KF_DIR, "%s_first.png" % sid), fi)
        put_input(os.path.join(KF_DIR, "%s_last.png" % sid), li)
        wf = video_wf(fi, li, s["prompt"], s["seed"], lora, steps)
        ok = False
        for t in range(1, 3):
            try:
                log("%s render attempt %d ..." % (sid, t))
                t0 = time.time()
                r = cc.queue(wf)
                entry = cc.wait(r["prompt_id"], timeout=3600, quiet_first=30)
                got = download_media(entry, out)
                if got:
                    log("%s OK in %.1f min -> %s" % (sid, (time.time() - t0) / 60, got))
                    ok = True
                    break
                log("%s no media in outputs: %s" % (sid, json.dumps(entry.get("outputs", {}))[:300]))
            except Exception as e:
                log("%s attempt %d error: %s" % (sid, t, e))
            time.sleep(20)
        if not ok:
            log("%s FAILED, continue" % sid)
    log("VIDEO BATCH FINISHED")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        sys.exit(1)
