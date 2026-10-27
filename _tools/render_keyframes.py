# -*- coding: utf-8 -*-
"""Batch-render all 20 keyframes for 《雨落无声》 with Sakura-Image-2.1-Turbo.
Per shot: first frame = txt2img; last frame = edit of first frame (identity-preserving).
"""
import sys, os, json, time, shutil, traceback
sys.path.insert(0, r"E:\projects\推理\R18_video\_tools")
import comfy_client as cc

KF_DIR   = r"E:\projects\推理\R18_video\output\keyframes"
CUI_INPUT = r"E:\SD-WEBUI-AKI-V5.0\ComfyUI_windows_portable\ComfyUI\user\default\input"
CUI_INPUT2 = r"E:\SD-WEBUI-AKI-V5.0\ComfyUI_windows_portable\ComfyUI\input"
LOG      = os.path.join(os.path.dirname(__file__), "keyframes.log")

CHAR = ("a 14-year-old Japanese schoolgirl, shoulder-length straight black hair with blunt bangs, "
        "big amber eyes, slightly tall for her age, youthful slender figure, wearing a navy sailor "
        "school uniform blouse with white triple-striped collar and red scarf, light grey pleated skirt, "
        "black over-knee socks, white sneakers, cream-colored shoulder school bag")
STYLE = ("cinematic anime film still, Makoto Shinkai style rain, cel-shaded with soft moody lighting, "
         "wet asphalt reflections, muted dusk palette with warm neon accents, film grain, 16:9 widescreen composition")
NEG = ("photorealistic, 3d render, western cartoon, text, watermark, logo, signature, extra fingers, "
       "deformed hands, extra limbs, bad anatomy, mature woman, adult body type, old person, blurry, lowres, cropped")
NEGEDIT = NEG + ", different person, different face, different hair, different clothes, different location, different camera angle"

def A(p):  # txt2img prompt with char+style
    return p + ", " + STYLE
def E(p):  # edit prompt
    return ("Edit <image1>: keep exactly the same girl, face, hairstyle, uniform, camera angle, framing, "
            "lighting and background; change only the following — " + p + ". " + STYLE)

SHOTS = [
 dict(id="S1", seed=20261017, reuse_first="S1_first_test_01.png",
      first="inside a rain-soaked bus stop shelter at night, " + CHAR + " sitting alone on the bench, head slightly lowered, a transparent vinyl umbrella dripping water in her hand, school bag beside her, empty street and distant city light bokeh behind the glass, rain streaks under the shelter light",
      last="she lifts her head, looking up at the rain-streaked bus timetable sign above the bench, faint worried frown, rain droplets running down the shelter glass"),
 dict(id="S2", seed=20261021,
      first="close-up of the rain-covered glass advertising panel of a bus shelter at night, warm panel glow, in the glass reflection " + CHAR + "'s face is softly visible looking down at her phone, and deep in the reflection a lone black umbrella is entering the frame behind her, slightly out of focus",
      last="the black umbrella in the glass reflection now stands still just two steps behind the reflected girl, a tall faceless figure in a long dark coat under it, face hidden in shadow, the reflected girl still unaware"),
 dict(id="S3", seed=20261031,
      first="from inside the bus shelter looking out at the wet road at night, red tail-lights of a departing bus streaking into rain haze, " + CHAR + " standing at the edge of the shelter biting her lip in hesitation, a dark narrow alley mouth lit by a flickering neon sign beside her",
      last="she steps out into the rain under an open transparent vinyl umbrella toward the alley mouth, and at the very edge of the frame a black umbrella has appeared behind her, both reflected in the flooded street"),
 dict(id="S4", seed=20261041,
      first="inside a narrow old-town alley at night, pink and cyan neon sliding over wet brick walls, " + CHAR + " walking alone mid-stride seen from front-side, a large ominous shadow of a man with an umbrella looming over her from the wall, her transparent umbrella dripping",
      last=None,  # last = new composition below
      last_fresh="dramatic close-up: a big black-leather-gloved hand seizing the slender wrist of " + CHAR + ", she flinches and turns her head back in shock, mouth open, her transparent umbrella slipping from her other hand tumbling mid-air, scattered raindrops"),
 dict(id="S5", seed=20261051,
      first="ground-level shot on wet asphalt in the alley: a transparent vinyl umbrella bouncing on the flooded ground, a cream school bag sliding off a girl's shoulder mid-fall, a red hair ribbon slipping from black hair caught mid-air, her grabbed wrist visible, motion blur",
      last="the red hair ribbon has landed in a shallow puddle among scattered school books, raindrops making concentric ripple rings, at the top edge of frame the girl's white sneakers braced against a man's black leather shoe, ground-level macro view"),
 dict(id="S6", seed=20261061,
      first="under the concrete canopy of an old building entrance at night, rain curtain outside lit by distant streetlight, " + CHAR + " pressed back against a mottled peeling wall, one wrist pinned above her head by the dark sleeve of a faceless tall silhouette of a man in a long black coat, wet bangs stuck to her forehead, teary amber eyes glaring defiant, red scarf askew",
      last="the faceless dark silhouette has bent closer over her, her head turned away to the side with eyes squeezed shut, the sailor blouse slipped off one shoulder, the man still a pure backlit silhouette"),
 dict(id="S7", seed=20261071,
      first="a rain-wet brick wall glowing amber in streetlight at night, shadow-puppet scene: the large silhouette shadow of a man in a long coat enveloping the slender shadow of a schoolgirl pinned against the wall, her pleated-skirt shadow flared, thin streams of water running down the wall, only shadows and no bodies visible",
      last="in the shadow-play she arches her shadowed head back with mouth open in a silent cry, the shadow of her red scarf dangling loose from her hair, one wide sheet of rainwater sliding down across both shadows"),
 dict(id="S8", seed=20261081,
      first="the " + CHAR + " crouched in the corner of the building entrance seen from behind-side, her navy sailor blouse slipped down her upper arms over a white cotton camisole undershirt, arms hugging herself, wet black hair hanging limp, shoulder blades trembling, rain mist swirling just outside the canopy, fully covered, modest",
      last="she turns her head back toward the camera, face in three-quarter view, eyelashes wet, an empty hollow stare rather than crying, same crouched pose, same clothes, same corner"),
 dict(id="S9", seed=20261091,
      first="macro shot of a shallow puddle on concrete under dim light, a red hair ribbon lying on the oily iridescent water surface, a girl's blurred tear-streaked reflection broken in soft ripples",
      last="a small pale hand reaching into the frame plucking the red ribbon out of the water, ripples exploding outward erasing the reflection, water dripping from her fingers"),
 dict(id="S10", seed=20261101,
      first="back view of " + CHAR + " standing alone at the alley mouth in drizzling rain, soaked uniform clinging to her thin back, hair tips dripping, far away warm yellow apartment windows glowing through rain haze, she looks small against the night",
      last="three-quarter profile of her face as she half-turns toward the camera, a drop of water rolling down her jawline, amber eyes staring straight into lens with quiet resolve, rain-haze lights behind, darker frame with soft vignette"),
]

def edit_wf(ref_image_name, edit_prompt, seed, filename_prefix):
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "Sakura-Image-2.1-Turbo-Q5_K_M-4.60GiB.gguf"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "Sakura-TextEncoder-Qwen3VL-8B-Uncensored-bf16.safetensors", "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": ref_image_name}},
        "14": {"class_type": "ImageScale", "inputs": {"image": ["12", 0], "upscale_method": "lanczos", "width": 0, "height": 0, "crop": "center"}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "prompt": edit_prompt,
              "negative_prompt": NEGEDIT, "resolution": 1024, "vae": ["3", 0], "images.image_1": ["14", 0]}},
        "5": {"class_type": "ManualSigmas", "inputs": {"sigmas": "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0"}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "8": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "cfg": 1.0}},
        "9": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["7", 0], "guider": ["8", 0], "sampler": ["6", 0], "sigmas": ["5", 0], "latent_image": ["4", 2]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": filename_prefix}},
    }

def log(m):
    line = "%s %s" % (time.strftime("%H:%M:%S"), m)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def run(wf, prefix, dest_name, tries=2):
    dest = os.path.join(KF_DIR, dest_name)
    for t in range(1, tries + 1):
        try:
            r = cc.queue(wf)
            entry = cc.wait(r["prompt_id"], timeout=900)
            files = cc.download_images(entry, KF_DIR, dest_name.rsplit(".", 1)[0] + "_raw")
            if files:
                shutil.copyfile(files[0], dest)
                return dest
            log("  no image output for %s" % prefix)
        except Exception as e:
            log("  attempt %d failed %s: %s" % (t, prefix, e))
            time.sleep(5)
    return None

def main():
    os.makedirs(KF_DIR, exist_ok=True)
    os.makedirs(CUI_INPUT, exist_ok=True)
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    for sh in SHOTS:
        sid = sh["id"]
        if only and sid not in only:
            continue
        first_name = "%s_first.png" % sid
        first_path = os.path.join(KF_DIR, first_name)
        if sh.get("reuse_first") and not os.path.exists(first_path):
            src = os.path.join(KF_DIR, sh["reuse_first"])
            if os.path.exists(src):
                shutil.copyfile(src, first_path)
                log("%s first: reused %s" % (sid, sh["reuse_first"]))
        if not os.path.exists(first_path):
            log("%s first: txt2img ..." % sid)
            wf = cc.img_wf(A(sh["first"]), NEG, sh["seed"], 1344, 768, "R18/kf/%s_first" % sid)
            if not run(wf, sid + "-first", first_name):
                log("%s FIRST FAILED, skipping shot" % sid); continue
        else:
            log("%s first: exists, skip" % sid)
        # last frame
        last_name = "%s_last.png" % sid
        last_path = os.path.join(KF_DIR, last_name)
        if os.path.exists(last_path):
            log("%s last: exists, skip" % sid); continue
        if sh.get("last_fresh"):
            log("%s last: fresh txt2img (new framing) ..." % sid)
            wf = cc.img_wf(A(sh["last_fresh"]), NEG, sh["seed"] + 7, 1344, 768, "R18/kf/%s_last" % sid)
        else:
            log("%s last: edit-mode ..." % sid)
            ref = "%s_ref.png" % sid
            for d in (CUI_INPUT, CUI_INPUT2):
                os.makedirs(d, exist_ok=True)
                shutil.copyfile(first_path, os.path.join(d, ref))
            wf = edit_wf(ref, E(sh["last"]), sh["seed"] + 5, "R18/kf/%s_last" % sid)
        if run(wf, sid + "-last", last_name):
            log("%s last: OK" % sid)
        else:
            log("%s last: FAILED" % sid)
    log("KEYFRAME BATCH FINISHED")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        sys.exit(1)
