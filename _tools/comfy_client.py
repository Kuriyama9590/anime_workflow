# -*- coding: utf-8 -*-
"""Minimal ComfyUI HTTP API client (stdlib only)."""
import json, time, urllib.request, urllib.parse, os, sys

SERVER = "http://127.0.0.1:8188"

def _get(url):
    with urllib.request.urlopen(url, timeout=300) as r:
        return r.read()

def _post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))

def queue(prompt_ws, client_id="r18-director"):
    return _post(SERVER + "/prompt", {"prompt": prompt_ws, "client_id": client_id})

def history(pid):
    return json.loads(_get(SERVER + "/history/" + pid).decode("utf-8"))

def wait(pid, timeout=1800, quiet_first=5):
    t0 = time.time()
    time.sleep(quiet_first)
    while time.time() - t0 < timeout:
        h = history(pid)
        if pid in h:
            entry = h[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error" or status.get("completed") is False and "messages" in status:
                pass
            if status.get("completed") is True or status.get("status_str") == "success":
                return entry
            if status.get("status_str") == "error":
                raise RuntimeError("execution error: " + json.dumps(status, ensure_ascii=False)[:4000])
        time.sleep(3)
    raise TimeoutError("prompt %s timed out" % pid)

def download_images(history_entry, dest_dir, prefix):
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    for nid, node in history_entry.get("outputs", {}).items():
        for img in node.get("images", []):
            q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img["type"]})
            raw = _get(SERVER + "/view?" + q)
            name = "%s_%02d.%s" % (prefix, len(out) + 1, img["filename"].rsplit(".", 1)[-1])
            p = os.path.join(dest_dir, name)
            with open(p, "wb") as f:
                f.write(raw)
            out.append(p)
    return out

def running():
    try:
        return json.loads(_get(SERVER + "/queue").decode("utf-8"))
    except Exception:
        return None

def img_wf(prompt, negative, seed, width, height, filename_prefix,
           ref_images=None, resolution=1024):
    """Sakura Qwen-Image-2.1 Turbo txt2img (optionally with reference images -> edit mode)."""
    te = {"clip": ["2", 0], "prompt": prompt, "negative_prompt": negative,
          "resolution": resolution, "vae": ["3", 0]}
    if ref_images:
        for i, refnode in enumerate(ref_images, 1):
            te["images.image_%d" % i] = refnode
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "Sakura-Image-2.1-Turbo-Q5_K_M-4.60GiB.gguf"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "Sakura-TextEncoder-Qwen3VL-8B-Uncensored-bf16.safetensors", "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": te},
        "5": {"class_type": "ManualSigmas", "inputs": {"sigmas": "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0"}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "8": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "cfg": 1.0}},
        "13": {"class_type": "EmptyQwenImage21Latent", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "9": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["7", 0], "guider": ["8", 0], "sampler": ["6", 0], "sigmas": ["5", 0], "latent_image": ["13", 0]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": filename_prefix}},
    }

if __name__ == "__main__":
    print(json.dumps({"ok": True, "queue": running()}))
