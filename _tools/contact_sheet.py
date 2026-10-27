# -*- coding: utf-8 -*-
"""Build a labelled contact sheet of all keyframes for human review."""
import os
from PIL import Image, ImageDraw

KF = r"E:\projects\推理\R18_video\output\keyframes"
OUT = r"E:\projects\推理\R18_video\output\keyframes_contact_sheet.png"
TW, TH = 448, 256  # thumb size ~1344x768 /3

def main():
    rows = []
    for i in range(1, 11):
        sid = "S%d" % i
        pair = []
        for kind in ("first", "last"):
            p = os.path.join(KF, "%s_%s.png" % (sid, kind))
            if os.path.exists(p):
                im = Image.open(p).convert("RGB").resize((TW, TH), Image.LANCZOS)
                pair.append((kind, im))
            else:
                blank = Image.new("RGB", (TW, TH), (24, 24, 30))
                d = ImageDraw.Draw(blank)
                d.text((8, 8), "MISSING %s_%s" % (sid, kind), fill=(255, 80, 80))
                pair.append((kind, blank))
        rows.append((sid, pair))
    pad, label_h = 8, 22
    W = pad + (TW * 2 + pad) + pad
    H = pad + len(rows) * (TH + label_h + pad) + pad
    sheet = Image.new("RGB", (W, H), (12, 12, 16))
    d = ImageDraw.Draw(sheet)
    y = pad
    for sid, pair in rows:
        x = pad
        for kind, im in pair:
            sheet.paste(im, (x, y + label_h))
            d.text((x + 4, y + 4), "%s %s" % (sid, kind), fill=(255, 230, 120))
            x += TW + pad
        y += TH + label_h + pad
    sheet.save(OUT)
    print(OUT)

if __name__ == "__main__":
    main()
