"""Generate the Arca app icon (.icns) and the DMG background."""
from __future__ import annotations

import math
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
os.makedirs(ASSETS, exist_ok=True)

S = 1024
SS = 4                      # supersampling factor
W = S * SS


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def diagonal_gradient(size, stops, angle=135):
    """stops: [(pos, rgb), ...] rendered on a small canvas then upscaled."""
    small = 96
    img = Image.new("RGB", (small, small))
    px = img.load()
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    denom = abs(dx) * small + abs(dy) * small
    off = (abs(dx) * 0 + abs(dy) * 0)
    for y in range(small):
        for x in range(small):
            t = ((x * dx + (small - 1 - y) * dy) - off) / denom
            t = min(1.0, max(0.0, t))
            # piecewise
            for i in range(len(stops) - 1):
                p0, c0 = stops[i]
                p1, c1 = stops[i + 1]
                if p0 <= t <= p1:
                    k = (t - p0) / max(1e-6, (p1 - p0))
                    px[x, y] = lerp(c0, c1, k)
                    break
            else:
                px[x, y] = stops[-1][1]
    return img.resize((size, size), Image.BICUBIC)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def build_icon():
    radius = int(0.2237 * W)
    # ---- base gradient
    base = diagonal_gradient(W, [
        (0.0, (112, 110, 252)),
        (0.40, (126, 92, 250)),
        (0.74, (140, 92, 246)),
        (1.0, (58, 190, 248)),
    ], angle=125).convert("RGBA")

    # ---- soft glow in the lower right
    glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([int(W * 0.42), int(W * 0.46), int(W * 1.02), int(W * 1.02)],
               fill=(56, 189, 248, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(int(W * 0.16)))
    base = Image.alpha_composite(base, glow)

    # ---- glass highlight along the top
    hi = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hi)
    hd.ellipse([-int(W * 0.35), -int(W * 0.95), int(W * 1.3), int(W * 0.34)],
               fill=(255, 255, 255, 40))
    hi = hi.filter(ImageFilter.GaussianBlur(int(W * 0.05)))
    base = Image.alpha_composite(base, hi)

    # ---- ground the icon with a soft inner shadow at the bottom (behind glyph)
    shade = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    sd.ellipse([-int(W * 0.2), int(W * 0.66), int(W * 1.2), int(W * 1.35)],
               fill=(40, 24, 96, 54))
    shade = shade.filter(ImageFilter.GaussianBlur(int(W * 0.10)))
    base = Image.alpha_composite(base, shade)

    # ---- glyph: a card with a zipper
    cw, ch = int(W * 0.42), int(W * 0.50)
    cx = W // 2
    top = int(W * 0.245)
    r_card = int(W * 0.062)

    # one card peeking underneath for depth
    bw = int(cw * 0.90)
    by = top + int(W * 0.052)
    under = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(under).rounded_rectangle(
        [cx - bw // 2, by, cx + bw // 2, by + ch], radius=r_card, fill=(255, 255, 255, 105))
    base = Image.alpha_composite(base, under)

    glyph = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    g = ImageDraw.Draw(glyph)
    fx0, fy0 = cx - cw // 2, top
    fx1, fy1 = cx + cw // 2, top + ch
    g.rounded_rectangle([fx0, fy0, fx1, fy1], radius=r_card, fill=(255, 255, 255, 255))

    ink = (99, 84, 244, 255)
    spine_w = max(4, int(W * 0.013))
    tab_w = int(cw * 0.165)
    tab_h = int(ch * 0.042)
    gap = int(ch * 0.018)
    slider_y = fy0 + int(ch * 0.575)
    slider_h = int(ch * 0.105)
    z_top = fy0 + int(ch * 0.115)

    # closed part above the slider: spine + interlocking teeth
    g.rounded_rectangle([cx - spine_w // 2, z_top, cx + spine_w // 2, slider_y + slider_h // 2],
                        radius=spine_w // 2, fill=ink)
    ty = z_top
    i = 0
    while ty + tab_h <= slider_y - int(ch * 0.012):
        if i % 2 == 0:
            box = [cx - tab_w, ty, cx - spine_w // 2 + 3, ty + tab_h]
        else:
            box = [cx + spine_w // 2 - 3, ty, cx + tab_w, ty + tab_h]
        g.rounded_rectangle(box, radius=tab_h // 2, fill=ink)
        ty += tab_h + gap
        i += 1

    # slider body: a flat horizontal pill sitting on the spine
    sw = int(cw * 0.46)
    g.rounded_rectangle([cx - sw // 2, slider_y, cx + sw // 2, slider_y + slider_h],
                        radius=slider_h // 2, fill=ink)

    # open part below the slider: two separated columns of teeth
    oy = slider_y + slider_h + int(ch * 0.026)
    shift = int(cw * 0.040)
    for i in range(3):
        g.rounded_rectangle([cx - tab_w - shift, oy, cx - spine_w // 2 - shift,
                             oy + tab_h], radius=tab_h // 2, fill=ink)
        g.rounded_rectangle([cx + spine_w // 2 + shift, oy, cx + tab_w + shift,
                             oy + tab_h], radius=tab_h // 2, fill=ink)
        oy += tab_h + gap

    base = Image.alpha_composite(base, glyph)

    # ---- rim light
    edge = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ed = ImageDraw.Draw(edge)
    ed.rounded_rectangle([2, 2, W - 3, W - 3], radius=radius,
                         outline=(255, 255, 255, 74), width=int(W * 0.005))
    base = Image.alpha_composite(base, edge)

    # ---- final squircle mask
    mask = rounded_mask(W, radius)
    out = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    return out.resize((S, S), Image.LANCZOS)


def build_dmg_bg():
    w, h = 1200, 760
    img = diagonal_gradient(w, [(0.0, (250, 250, 253)), (1.0, (238, 238, 248))], angle=110)
    img = img.convert("RGBA")
    d = ImageDraw.Draw(img)
    d.ellipse([-200, -260, 520, 260], fill=(120, 108, 250, 22))
    d.ellipse([w - 380, h - 300, w + 160, h + 160], fill=(56, 189, 248, 20))
    img = img.filter(ImageFilter.GaussianBlur(2))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([40, 40, w - 40, h - 40], radius=28, outline=(20, 20, 40, 14), width=2)
    path = os.path.join(ASSETS, "dmg-background.png")
    img.convert("RGB").save(path, "PNG")
    return path


def write_icns(icon, path):
    """Write a modern .icns (PNG payloads) without shelling out to iconutil."""
    import io
    import struct

    specs = [
        (b"icp4", 16), (b"icp5", 32), (b"icp6", 64),
        (b"ic07", 128), (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024),
        (b"ic11", 32), (b"ic12", 64), (b"ic13", 256), (b"ic14", 512),
    ]
    chunks = []
    for code, size in specs:
        buf = io.BytesIO()
        icon.resize((size, size), Image.LANCZOS).save(buf, "PNG", optimize=True)
        data = buf.getvalue()
        chunks.append(code + struct.pack(">I", len(data) + 8) + data)
    body = b"".join(chunks)
    with open(path, "wb") as fh:
        fh.write(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main():
    icon = build_icon()
    png = os.path.join(ASSETS, "Arca-1024.png")
    icon.save(png, "PNG")

    icns = os.path.join(ASSETS, "Arca.icns")
    write_icns(icon, icns)
    bg = build_dmg_bg()
    print("icon:", icns, os.path.getsize(icns))
    print("png :", png)
    print("dmg :", bg)


if __name__ == "__main__":
    main()
