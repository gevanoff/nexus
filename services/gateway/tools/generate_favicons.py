#!/usr/bin/env python3
"""Generate favicon PNGs and a multi-size ICO from app/static/ai-infra.png

Writes:
 - app/static/favicon-16.png
 - app/static/favicon-32.png
 - app/static/favicon-48.png
 - app/static/favicon-64.png
 - app/static/favicon-128.png
 - app/static/favicon-180.png (apple-touch-icon)
 - app/static/favicon-192.png
 - app/static/favicon.ico (contains 16/32/48/64)

Requires Pillow: pip install pillow
"""
from pathlib import Path
from PIL import Image

SRC = Path("app/static/ai-infra.png")
OUT = Path("app/static")
SIZES = [16, 32, 48, 64, 128, 180, 192, 512]
ICO_SIZES = [16, 32, 48, 64]

if not SRC.exists():
    raise SystemExit(f"Source icon not found: {SRC}")

im = Image.open(SRC).convert("RGBA")
for s in SIZES:
    outp = OUT / f"favicon-{s}.png"
    im_resized = im.resize((s, s), Image.LANCZOS)
    im_resized.save(outp, format="PNG")
    print("wrote", outp)

# apple-touch-icon (180)
if (OUT / "apple-touch-icon.png").exists():
    pass
else:
    (OUT / "apple-touch-icon.png").write_bytes((OUT / "favicon-180.png").read_bytes())

# favicon-192
# already created above as favicon-192.png

# Create multi-size ICO
ico_path = OUT / "favicon.ico"
# Pillow accepts sizes parameter when saving as ICO
try:
    im.save(ico_path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print("wrote", ico_path)
except Exception as e:
    print("failed to write ico:", e)
    raise

print("Done.")
