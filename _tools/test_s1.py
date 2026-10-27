# -*- coding: utf-8 -*-
"""Test run: character/scene keyframe S1_first via Sakura turbo."""
import sys, json, time
sys.path.insert(0, r"E:\projects\推理\R18_video\_tools")
import comfy_client as cc

CHAR = ("a 14-year-old Japanese schoolgirl, shoulder-length straight black hair with blunt bangs, "
        "big amber eyes, slightly tall for her age, youthful slender figure, wearing a navy sailor "
        "school uniform blouse with white triple-striped collar and red scarf, light grey pleated skirt, "
        "black over-knee socks, white sneakers, cream-colored shoulder school bag")
STYLE = ("cinematic anime film still, Makoto Shinkai style rain, cel-shaded with soft moody lighting, "
         "wet asphalt reflections, muted dusk palette with warm neon accents, film grain, 16:9 widescreen composition")
NEG = ("photorealistic, 3d render, western cartoon, text, watermark, logo, signature, extra fingers, "
       "deformed hands, extra limbs, bad anatomy, mature woman, adult body type, old person, blurry, lowres, cropped")

prompt = ("inside a rain-soaked bus stop shelter at night, " + CHAR +
          " sitting alone on the bench, head slightly lowered, a transparent vinyl umbrella dripping water "
          "in her hand, school bag beside her, empty street and distant city light bokeh behind the glass, "
          "rain streaks under the shelter light, " + STYLE)

ws = cc.img_wf(prompt, NEG, seed=20261017, width=1344, height=768, filename_prefix="R18/kf_test")
t0 = time.time()
res = cc.queue(ws)
pid = res["prompt_id"]
print("queued", pid, flush=True)
entry = cc.wait(pid, timeout=1500)
print("done in %.1fs" % (time.time() - t0), flush=True)
files = cc.download_images(entry, r"E:\projects\推理\R18_video\output\keyframes", "S1_first_test")
print(json.dumps(files, ensure_ascii=False))
