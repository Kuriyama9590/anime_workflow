# -*- coding: utf-8 -*-
"""Resume: fetch history for an already-queued prompt id and download images."""
import sys, json
sys.path.insert(0, r"E:\projects\推理\R18_video\_tools")
import comfy_client as cc

pid = sys.argv[1]
dest = sys.argv[2]
prefix = sys.argv[3]
entry = cc.wait(pid, timeout=1800)
files = cc.download_images(entry, dest, prefix)
print(json.dumps(files, ensure_ascii=False))
