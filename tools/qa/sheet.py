"""Put each size's QA screenshots side by side, one sheet per size, for a quick look.

    python3 tools/qa/sheet.py <qa out dir>
"""
import glob
import os
import re
import sys

from PIL import Image, ImageDraw

out = sys.argv[1]
groups = {}
for f in glob.glob(os.path.join(out, "*-[0-9]*.png")):
    m = re.match(r"(.+)-(\d+)\.png$", os.path.basename(f))
    if m:
        groups.setdefault(m.group(1), []).append((int(m.group(2)), f))
for size, files in sorted(groups.items()):
    h = 640
    ims = [Image.open(f) for _, f in sorted(files)]
    ims = [i.resize((max(1, int(i.width * h / i.height)), h)) for i in ims]
    sheet = Image.new("RGB", (sum(i.width for i in ims) + 12 * (len(ims) - 1), h + 28), "white")
    ImageDraw.Draw(sheet).text((8, 8), size, fill="black")
    x = 0
    for i in ims:
        sheet.paste(i, (x, 28))
        x += i.width + 12
    sheet.save(os.path.join(out, f"sheet-{size}.png"))
    print("sheet", size, len(ims))
