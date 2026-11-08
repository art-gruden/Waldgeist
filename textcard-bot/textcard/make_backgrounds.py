#!/usr/bin/env python3
"""Генерує фонові зображення для вбудованих шаблонів (без мережі, лише numpy + Pillow).

    python3 textcard/make_backgrounds.py            # усі фони -> templates/<id>/background.jpg
    python3 textcard/make_backgrounds.py pink_glow  # лише один

Фони детерміновані (фіксований seed), тож повторний запуск дає ті самі файли.
Для 10 яскравих шаблонів (lemon_sun … golden_noon) скрипт пише й template.json (див. CONFIGS).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

S = 2048  # квадрат: з нього вирізаються всі формати (1:1, 4:5, 16:9, 9:16)
ROOT = Path(__file__).resolve().parent.parent / "templates"


# ---------- допоміжне ----------

def hex2rgb(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32) / 255.0


def grid():
    y, x = np.mgrid[0:S, 0:S].astype(np.float32) / (S - 1)
    return x, y


def gradient(t: np.ndarray, stops: list[tuple[float, str]]) -> np.ndarray:
    """Кольоровий градієнт за параметром t (0..1) з контрольними точками."""
    t = np.clip(t, 0, 1)
    out = np.zeros(t.shape + (3,), np.float32)
    pos = [p for p, _ in stops]
    cols = [hex2rgb(c) for _, c in stops]
    for c in range(3):
        out[..., c] = np.interp(t, pos, [col[c] for col in cols])
    return out


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def noise(rng, octaves=5, base=4, persistence=0.55) -> np.ndarray:
    """Фрактальний шум 0..1 (сума збільшених випадкових сіток)."""
    acc = np.zeros((S, S), np.float32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        n = base * 2 ** o
        small = Image.fromarray((rng.random((n, n)) * 255).astype(np.uint8))
        big = np.asarray(small.resize((S, S), Image.BICUBIC), np.float32) / 255.0
        acc += big * amp
        total += amp
        amp *= persistence
    acc /= total
    return (acc - acc.min()) / (acc.max() - acc.min() + 1e-6)


def screen(base: np.ndarray, light: np.ndarray, color, amount=1.0) -> np.ndarray:
    """Освітлення режимом «екран»: light — маска 0..1."""
    col = hex2rgb(color) if isinstance(color, str) else color
    add = np.clip(light, 0, 1)[..., None] * col * amount
    return 1 - (1 - base) * (1 - add)


def mix(base, color, alpha):
    col = hex2rgb(color) if isinstance(color, str) else color
    a = np.clip(alpha, 0, 1)[..., None]
    return base * (1 - a) + col * a


def radial(x, y, cx, cy, r) -> np.ndarray:
    d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / r
    return np.exp(-d * d * 2.2)


def mask_layer(draw_fn, blur: float) -> np.ndarray:
    im = Image.new("L", (S, S), 0)
    draw_fn(ImageDraw.Draw(im))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    return np.asarray(im, np.float32) / 255.0


def bokeh(rng, n, rmin, rmax, blur, region=None, alpha=(0.25, 0.7)) -> np.ndarray:
    def fn(d):
        for _ in range(n):
            if region:
                x0, y0, x1, y1 = region
                cx, cy = rng.uniform(x0, x1) * S, rng.uniform(y0, y1) * S
            else:
                cx, cy = rng.uniform(0, S), rng.uniform(0, S)
            r = rng.uniform(rmin, rmax)
            a = int(255 * rng.uniform(*alpha))
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=a)
    return mask_layer(fn, blur)


def sparkles(rng, n, size=(2, 6), region=None) -> np.ndarray:
    """Яскраві точки з хрестоподібними променями."""
    def fn(d):
        for _ in range(n):
            if region:
                x0, y0, x1, y1 = region
                cx, cy = rng.uniform(x0, x1) * S, rng.uniform(y0, y1) * S
            else:
                cx, cy = rng.uniform(0, S), rng.uniform(0, S)
            r = rng.uniform(*size)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
            if r > size[1] * 0.7:
                L = r * 7
                d.line([cx - L, cy, cx + L, cy], fill=150, width=2)
                d.line([cx, cy - L, cx, cy + L], fill=150, width=2)
    core = mask_layer(fn, 1.2)
    halo = mask_layer(fn, 9)
    return np.clip(core + halo * 1.6, 0, 1)


def ribbons(rng, n, amp, width=(3, 10), blur=6, ystart=(0.2, 0.9), tilt=-0.35) -> np.ndarray:
    """Світлові «стрічки» — синусоїди з різною фазою (як світлові завитки)."""
    def fn(d):
        for _ in range(n):
            y0 = rng.uniform(*ystart) * S
            f = rng.uniform(0.6, 1.6)
            ph = rng.uniform(0, 2 * math.pi)
            a = rng.uniform(0.4, 1.0) * amp * S
            w = int(rng.uniform(*width))
            pts = []
            for i in range(0, S + 41, 20):
                xx = i
                yy = y0 + a * math.sin(f * 2 * math.pi * i / S + ph) + tilt * (i - S / 2)
                pts.append((xx, yy))
            d.line(pts, fill=int(rng.uniform(90, 230)), width=w, joint="curve")
    return mask_layer(fn, blur)


def rays(x, y, cx, cy, n, spread, rng, softness=0.5) -> np.ndarray:
    """Промені з точки (cx, cy) донизу/вбік."""
    ang = np.arctan2(y - cy, x - cx)
    acc = np.zeros_like(x)
    for _ in range(n):
        a0 = rng.uniform(*spread)
        w = rng.uniform(0.015, 0.06)
        acc += np.exp(-((ang - a0) / w) ** 2) * rng.uniform(0.3, 1.0)
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    return np.clip(acc, 0, 1) * np.exp(-dist * softness)


def grain(img, rng, amount=0.012):
    return img + (rng.random(img.shape[:2], dtype=np.float32)[..., None] - 0.5) * amount


def save(img: np.ndarray, name: str):
    out = ROOT / name
    out.mkdir(parents=True, exist_ok=True)
    arr = (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8)
    Image.fromarray(arr, "RGB").save(out / "background.jpg", quality=90, optimize=True, progressive=True)
    print("ok", out / "background.jpg")


# ---------- фони ----------

def pink_glow():
    rng = np.random.default_rng(11)
    x, y = grid()
    t = (x * 0.55 + y * 0.45)
    img = gradient(t + (noise(rng, 4, 3) - 0.5) * 0.18, [
        (0.0, "#e25bb0"), (0.3, "#f28ccc"), (0.55, "#f9b9d2"), (0.78, "#fdd9c2"), (1.0, "#fff1b0")])
    img = screen(img, radial(x, y, 0.62, 0.42, 0.55), "#ffffff", 0.55)
    img = screen(img, radial(x, y, 0.95, 0.05, 0.4), "#fff6c8", 0.6)
    img = screen(img, ribbons(rng, 14, 0.12, (2, 9), 5, (0.25, 1.0), -0.45), "#ffffff", 0.55)
    img = screen(img, ribbons(rng, 6, 0.08, (20, 40), 25, (0.2, 0.9), -0.4), "#ffe6f4", 0.35)
    img = screen(img, bokeh(rng, 60, 10, 60, 6), "#ffffff", 0.22)
    img = screen(img, sparkles(rng, 70, (2, 6)), "#ffffff", 0.9)
    save(grain(img, rng), "pink_glow")


def sunset():
    rng = np.random.default_rng(22)
    x, y = grid()
    img = gradient(y + (noise(rng, 4, 3) - 0.5) * 0.06, [
        (0.0, "#1e0c45"), (0.25, "#5a1f7a"), (0.45, "#b8407e"), (0.6, "#f0766a"), (0.7, "#ffb36b"), (0.74, "#ffd98a"), (1.0, "#ffd98a")])
    sun = radial(x, y, 0.5, 0.72, 0.5)
    img = screen(img, sun, "#ffd27a", 0.7)
    img = screen(img, mask_layer(lambda d: d.ellipse([S * 0.43, S * 0.65, S * 0.57, S * 0.79], fill=255), 6), "#fff4cf", 1.0)
    clouds = noise(rng, 6, 2)
    band = smoothstep(0.08, 0.3, y) * (1 - smoothstep(0.5, 0.66, y))
    img = mix(img, "#f7a0a8", smoothstep(0.55, 0.8, clouds) * band * 0.45)
    # силуети пагорбів
    for i, (base, col, ampl) in enumerate([(0.74, "#5a2152", 0.05), (0.8, "#3c1640", 0.06), (0.88, "#230c2b", 0.05)]):
        n = noise(np.random.default_rng(100 + i), 4, 2)
        ridge = base + (n[S // 2, :][None, :] - 0.5) * ampl * 2
        img = mix(img, col, smoothstep(ridge - 0.002, ridge + 0.002, y))
    img = screen(img, sparkles(rng, 40, (1, 3), (0, 0, 1, 0.35)), "#ffffff", 0.7)
    save(grain(img, rng), "sunset")


def ocean():
    rng = np.random.default_rng(33)
    x, y = grid()
    img = gradient(y + (noise(rng, 4, 3) - 0.5) * 0.08, [
        (0.0, "#0a2f4f"), (0.35, "#11618a"), (0.7, "#1f97b4"), (1.0, "#6fd3d8")])
    img = screen(img, rays(x, y, 0.5, -0.12, 22, (math.pi * 0.25, math.pi * 0.75), rng, 0.9), "#bff4ff", 0.45)
    img = screen(img, radial(x, y, 0.5, 0.0, 0.5), "#c9f7ff", 0.45)
    caust = noise(rng, 5, 6)
    img = screen(img, smoothstep(0.55, 0.75, caust) * smoothstep(0.5, 1.0, y), "#aef0f5", 0.25)
    img = screen(img, bokeh(rng, 90, 4, 22, 2, alpha=(0.15, 0.5)), "#e6fbff", 0.35)
    save(grain(img, rng), "ocean")


def lavender():
    rng = np.random.default_rng(44)
    x, y = grid()
    img = gradient(y * 0.8 + x * 0.2 + (noise(rng, 4, 3) - 0.5) * 0.1, [
        (0.0, "#a993e6"), (0.4, "#cdb9f2"), (0.75, "#f1def3"), (1.0, "#fde8ee")])
    clouds = noise(rng, 6, 3)
    img = screen(img, smoothstep(0.5, 0.85, clouds), "#ffffff", 0.45)
    img = screen(img, radial(x, y, 0.3, 0.25, 0.55), "#ffffff", 0.35)
    img = screen(img, bokeh(rng, 40, 20, 90, 18, alpha=(0.15, 0.4)), "#ffffff", 0.3)
    img = screen(img, sparkles(rng, 30, (1.5, 4)), "#ffffff", 0.7)
    save(grain(img, rng), "lavender")


def night_sky():
    rng = np.random.default_rng(55)
    x, y = grid()
    img = gradient(y + (noise(rng, 4, 2) - 0.5) * 0.1, [
        (0.0, "#04060f"), (0.5, "#0d1440"), (1.0, "#2a1d5a")])
    neb = noise(rng, 6, 3)
    neb2 = noise(rng, 6, 3)
    band = np.exp(-((y - 0.45 - (x - 0.5) * 0.5) / 0.22) ** 2)
    img = screen(img, smoothstep(0.45, 0.85, neb) * band, "#7a3fd0", 0.55)
    img = screen(img, smoothstep(0.5, 0.9, neb2) * band, "#2fb6c9", 0.35)
    stars = (rng.random((S, S)) > 0.9985).astype(np.float32) * rng.random((S, S)).astype(np.float32)
    st = Image.fromarray((stars * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.9))
    img = screen(img, np.asarray(st, np.float32) / 255 * 3.0, "#ffffff", 1.0)
    img = screen(img, sparkles(rng, 25, (2, 5)), "#dfe6ff", 0.9)
    save(grain(img, rng, 0.02), "night_sky")


def forest():
    rng = np.random.default_rng(66)
    x, y = grid()
    n = noise(rng, 5, 3)
    img = gradient(y * 0.7 + (1 - x) * 0.0 + (n - 0.5) * 0.25, [
        (0.0, "#3f7a3a"), (0.35, "#245c34"), (0.7, "#163f28"), (1.0, "#0c2a1c")])
    img = screen(img, bokeh(rng, 140, 15, 70, 14, alpha=(0.2, 0.6)), "#7fbf4a", 0.4)
    img = screen(img, bokeh(rng, 60, 8, 30, 5, (0, 0, 0.6, 0.6), alpha=(0.3, 0.8)), "#e9f59a", 0.35)
    img = screen(img, radial(x, y, 0.12, 0.05, 0.6), "#fff0a8", 0.8)
    img = screen(img, rays(x, y, 0.1, 0.0, 16, (math.pi * 0.1, math.pi * 0.45), rng, 0.8), "#fff2b8", 0.5)
    save(grain(img, rng), "forest")


def golden_bokeh():
    rng = np.random.default_rng(77)
    x, y = grid()
    d = np.sqrt((x - 0.5) ** 2 + (y - 0.5) ** 2)
    img = gradient(d * 1.4, [(0.0, "#3d2509"), (0.6, "#1d1104"), (1.0, "#0c0702")])
    img = screen(img, bokeh(rng, 70, 40, 140, 22, alpha=(0.15, 0.45)), "#c98a2a", 0.6)
    img = screen(img, bokeh(rng, 120, 12, 45, 4, alpha=(0.2, 0.7)), "#ffcf6b", 0.45)
    img = screen(img, sparkles(rng, 50, (1.5, 4)), "#ffe9b0", 0.8)
    save(grain(img, rng, 0.02), "golden_bokeh")


def paper():
    rng = np.random.default_rng(88)
    x, y = grid()
    img = gradient(np.full_like(x, 0.5), [(0.0, "#f7f0e1"), (1.0, "#f7f0e1")])
    fib = noise(rng, 7, 8, 0.7)
    img = img * (0.965 + fib[..., None] * 0.05)
    stain = noise(rng, 5, 2)
    img = mix(img, "#e9d6b4", smoothstep(0.6, 0.95, stain) * 0.35)
    img = mix(img, "#efc9b8", smoothstep(0.7, 1.0, noise(rng, 5, 2)) * 0.18)
    d = np.sqrt((x - 0.5) ** 2 + (y - 0.5) ** 2)
    img = img * (1 - smoothstep(0.45, 0.8, d)[..., None] * 0.18)
    save(grain(img, rng, 0.03), "paper")


# ---------- яскраві «позитивні» фони (світлі, теплі, з сонцем і боке) ----------

def petals(rng, n, size=(14, 38), blur=1.0) -> np.ndarray:
    """Пелюстки: повернуті видовжені еліпси різної яскравості."""
    def fn(d):
        for _ in range(n):
            cx, cy = rng.uniform(0, S), rng.uniform(0, S)
            r = rng.uniform(*size)
            a = rng.uniform(0, 2 * math.pi)
            pts = []
            for k in range(28):
                t = 2 * math.pi * k / 28
                px, py = r * math.cos(t), r * 0.55 * math.sin(t) * (1 + 0.25 * math.cos(t))
                pts.append((cx + px * math.cos(a) - py * math.sin(a), cy + px * math.sin(a) + py * math.cos(a)))
            d.polygon(pts, fill=int(rng.uniform(150, 255)))
    return mask_layer(fn, blur)


def lemon_sun():
    rng = np.random.default_rng(111)
    x, y = grid()
    d = np.sqrt((x - 0.5) ** 2 + (y - 0.4) ** 2)
    img = gradient(d * 1.3 + (noise(rng, 4, 3) - 0.5) * 0.1, [
        (0.0, "#fcffd9"), (0.25, "#f4f784"), (0.6, "#e3f550"), (1.0, "#bfe03a")])
    img = screen(img, rays(x, y, 0.5, 0.4, 30, (-math.pi, math.pi), rng, 0.55), "#ffffff", 0.45)
    img = screen(img, radial(x, y, 0.5, 0.4, 0.35), "#ffffff", 0.6)
    img = screen(img, bokeh(rng, 50, 15, 70, 10, alpha=(0.15, 0.4)), "#ffffff", 0.3)
    img = screen(img, sparkles(rng, 45, (2, 5)), "#ffffff", 0.9)
    save(grain(img, rng), "lemon_sun")


def coral_sky():
    rng = np.random.default_rng(222)
    x, y = grid()
    img = gradient(y * 0.85 + x * 0.15 + (noise(rng, 4, 3) - 0.5) * 0.12, [
        (0.0, "#ffe3b8"), (0.4, "#ffaa94"), (0.75, "#ff7f9a"), (1.0, "#ff6f91")])
    clouds = noise(rng, 6, 3)
    img = screen(img, smoothstep(0.5, 0.85, clouds) * (1 - smoothstep(0.6, 1.0, y) * 0.5), "#fff3e0", 0.5)
    img = screen(img, radial(x, y, 0.78, 0.22, 0.5), "#fff6d0", 0.7)
    img = screen(img, rays(x, y, 0.78, 0.22, 18, (math.pi * 0.5, math.pi * 0.95), rng, 0.7), "#fff1c8", 0.4)
    img = screen(img, bokeh(rng, 40, 15, 70, 10, alpha=(0.12, 0.35)), "#ffffff", 0.3)
    img = screen(img, sparkles(rng, 40, (2, 5)), "#ffffff", 0.9)
    save(grain(img, rng), "coral_sky")


def turquoise_lagoon():
    rng = np.random.default_rng(333)
    x, y = grid()
    img = gradient(y + (noise(rng, 4, 3) - 0.5) * 0.1, [
        (0.0, "#d4fff3"), (0.35, "#5fe6d3"), (0.75, "#18b5c9"), (1.0, "#0e8fb3")])
    img = screen(img, rays(x, y, 0.5, -0.1, 20, (math.pi * 0.3, math.pi * 0.7), rng, 0.8), "#ffffff", 0.5)
    caust = noise(rng, 5, 7)
    img = screen(img, smoothstep(0.55, 0.78, caust) * smoothstep(0.2, 0.9, y), "#e6fffb", 0.35)
    img = screen(img, bokeh(rng, 70, 5, 28, 2, alpha=(0.2, 0.55)), "#ffffff", 0.4)
    img = screen(img, sparkles(rng, 30, (1.5, 4)), "#ffffff", 0.8)
    save(grain(img, rng), "turquoise_lagoon")


def spring_green():
    rng = np.random.default_rng(444)
    x, y = grid()
    img = gradient(y * 0.8 + (1 - x) * 0.2 + (noise(rng, 5, 3) - 0.5) * 0.2, [
        (0.0, "#f6ffb0"), (0.45, "#a6ea6a"), (1.0, "#46c26c")])
    img = screen(img, bokeh(rng, 100, 20, 80, 16, alpha=(0.2, 0.5)), "#e8ff8a", 0.45)
    img = mix(img, "#2f9e55", bokeh(rng, 40, 25, 90, 18, alpha=(0.1, 0.3)) * 0.35)
    img = screen(img, radial(x, y, 0.15, 0.05, 0.6), "#ffffe0", 0.8)
    img = screen(img, rays(x, y, 0.12, -0.02, 16, (math.pi * 0.1, math.pi * 0.45), rng, 0.8), "#ffffe0", 0.5)
    img = screen(img, sparkles(rng, 35, (1.5, 4)), "#ffffff", 0.8)
    save(grain(img, rng), "spring_green")


def honey_morning():
    rng = np.random.default_rng(555)
    x, y = grid()
    img = gradient(x * 0.4 + y * 0.6 + (noise(rng, 4, 3) - 0.5) * 0.1, [
        (0.0, "#fff3c0"), (0.35, "#ffd04d"), (0.75, "#f9a825"), (1.0, "#ee8412")])
    img = screen(img, radial(x, y, 0.2, 0.15, 0.55), "#fffbe6", 0.8)
    img = screen(img, ribbons(rng, 10, 0.1, (2, 8), 5, (0.3, 1.0), -0.4), "#fff6d0", 0.5)
    img = screen(img, ribbons(rng, 5, 0.08, (20, 40), 25, (0.2, 0.9), -0.35), "#fff0b0", 0.35)
    img = screen(img, bokeh(rng, 55, 12, 60, 8, alpha=(0.15, 0.4)), "#fff2b8", 0.35)
    img = screen(img, sparkles(rng, 45, (2, 5)), "#ffffff", 0.9)
    save(grain(img, rng), "honey_morning")


def rainbow_bokeh():
    rng = np.random.default_rng(666)
    x, y = grid()
    img = gradient(x * 0.5 + y * 0.5, [(0.0, "#fff6fc"), (0.5, "#fffbe9"), (1.0, "#eef9ff")])
    for c in ["#ff7eb6", "#ffb84d", "#ffe45c", "#6be38a", "#4fd8ff", "#a98bff"]:
        img = mix(img, c, bokeh(rng, 14, 30, 110, 12, alpha=(0.35, 0.8)) * 0.6)
    img = screen(img, radial(x, y, 0.5, 0.5, 0.4), "#ffffff", 0.35)
    img = screen(img, sparkles(rng, 40, (2, 5)), "#ffffff", 0.9)
    save(grain(img, rng), "rainbow_bokeh")


def orange_dawn():
    rng = np.random.default_rng(777)
    x, y = grid()
    img = gradient(y + (noise(rng, 4, 3) - 0.5) * 0.05, [
        (0.0, "#ffe27d"), (0.3, "#ffbb4a"), (0.62, "#ff8a5c"), (0.85, "#ff6b8a"), (1.0, "#ff5c9a")])
    # сонце сходить біля нижнього краю: у горизонтальній листівці видно лише сяйво (не заважає підпису),
    # у пості, квадраті й сторіс — і сам диск
    img = screen(img, radial(x, y, 0.5, 0.86, 0.5), "#fff0b8", 0.8)
    img = screen(img, mask_layer(lambda d: d.ellipse([S * 0.425, S * 0.785, S * 0.575, S * 0.935], fill=255), 5),
                 "#fffbe0", 1.0)
    img = screen(img, rays(x, y, 0.5, 0.86, 22, (-math.pi * 0.95, -math.pi * 0.05), rng, 0.7), "#fff2c0", 0.4)
    band = smoothstep(0.05, 0.3, y) * (1 - smoothstep(0.45, 0.62, y))
    img = mix(img, "#ffd0b0", smoothstep(0.55, 0.8, noise(rng, 6, 2)) * band * 0.4)
    img = screen(img, sparkles(rng, 40, (2, 5)), "#ffffff", 0.85)
    save(grain(img, rng), "orange_dawn")


def cherry_blossom():
    rng = np.random.default_rng(888)
    x, y = grid()
    img = gradient(y * 0.6 + x * 0.4 + (noise(rng, 4, 3) - 0.5) * 0.12, [
        (0.0, "#ffe6f0"), (0.5, "#ffc9dd"), (1.0, "#ffeaf2")])
    img = screen(img, radial(x, y, 0.5, 0.4, 0.6), "#ffffff", 0.5)
    img = mix(img, "#ff8fb8", bokeh(rng, 40, 25, 90, 16, alpha=(0.1, 0.35)) * 0.5)
    img = mix(img, "#ffffff", petals(rng, 120, (14, 40), 1.0) * 0.8)
    img = mix(img, "#ff9fc3", petals(rng, 90, (12, 34), 1.0) * 0.7)
    img = screen(img, sparkles(rng, 35, (1.5, 4)), "#ffffff", 0.8)
    save(grain(img, rng), "cherry_blossom")


def blue_sky():
    rng = np.random.default_rng(999)
    x, y = grid()
    img = gradient(y + (noise(rng, 4, 3) - 0.5) * 0.04, [(0.0, "#3f9bff"), (0.55, "#86cdff"), (1.0, "#d6f0ff")])
    img = screen(img, radial(x, y, 0.82, 0.12, 0.45), "#fff9d6", 0.8)
    clouds = np.clip(smoothstep(0.52, 0.78, noise(rng, 6, 3)) * 0.9 + smoothstep(0.55, 0.8, noise(rng, 6, 4)) * 0.6, 0, 1)
    shade = clouds * smoothstep(0.3, 0.9, noise(rng, 5, 3))
    img = mix(img, "#ffffff", clouds * 0.92)
    img = mix(img, "#b6d6f2", shade * 0.35)
    img = screen(img, sparkles(rng, 20, (1.5, 4)), "#ffffff", 0.7)
    save(grain(img, rng), "blue_sky")


def golden_noon():
    rng = np.random.default_rng(1010)
    x, y = grid()
    d = np.sqrt((x - 0.5) ** 2 + (y - 0.5) ** 2)
    img = gradient(d * 1.5 + (noise(rng, 4, 3) - 0.5) * 0.08, [
        (0.0, "#fffbe3"), (0.3, "#ffeaa0"), (0.65, "#ffd24a"), (1.0, "#f5a812")])
    img = screen(img, rays(x, y, 0.5, 0.5, 34, (-math.pi, math.pi), rng, 0.6), "#ffffff", 0.3)
    img = screen(img, bokeh(rng, 70, 20, 90, 12, alpha=(0.15, 0.4)), "#fff2b0", 0.4)
    img = screen(img, sparkles(rng, 90, (2, 6)), "#ffffff", 0.95)
    save(grain(img, rng), "golden_noon")


ALL = {f.__name__: f for f in [pink_glow, sunset, ocean, lavender, night_sky, forest, golden_bokeh, paper,
                               lemon_sun, coral_sky, turquoise_lagoon, spring_green, honey_morning,
                               rainbow_bokeh, orange_dawn, cherry_blossom, blue_sky, golden_noon]}


# ---------- конфіги шаблонів для яскравих фонів (старі 8 лежать у templates/ готовими) ----------

def _style(font, color, glow, glow_r=0.28, glow_s=1.7, **kw):
    return {"font": font, "color": color, "glow_color": glow, "glow_radius": glow_r, "glow_strength": glow_s, **kw}


def _cfg(name, order, title, body, footer, focus=(0.5, 0.5)):
    return {"name": name, "background": "background.jpg", "default_size": "landscape", "order": order,
            "focus": list(focus), "title": title, "body": body, "footer": footer}


CONFIGS = {
    "lemon_sun": _cfg(
        "Лимонне сонце", 9,
        _style("Oswald-SemiBold", ["#7a3b00", "#b34700"], "#fffbd0", max_size=0.095),
        _style("PTSerif-Bold", "#4a2a00", "#fffbd0", accent_color="#c2410c", max_size=0.07, line_spacing=1.12),
        _style("Oswald-SemiBold", "#7a3b00", "#fffbd0", 0.3, 1.5)),
    "coral_sky": _cfg(
        "Кораловий ранок", 10,
        _style("Lobster-Regular", ["#7a0f3a", "#4d0a26"], "#fff1e6", uppercase=False, max_size=0.115),
        _style("Comfortaa-Bold", "#5a0f2e", "#fff1e6", accent_color="#1d5fa8", max_size=0.064, line_spacing=1.2),
        _style("Comfortaa-Bold", "#5a0f2e", "#fff1e6", 0.3, 1.5)),
    "turquoise_lagoon": _cfg(
        "Бірюзова лагуна", 11,
        _style("RobotoCondensed-Bold", "#05304a", "#e8fffb", max_size=0.093),
        _style("PTSansNarrow-Bold", "#06324a", "#e8fffb", accent_color="#c2185b", max_size=0.078),
        _style("RobotoCondensed-Bold", "#05304a", "#e8fffb", 0.3, 1.5)),
    "spring_green": _cfg(
        "Весняна зелень", 12,
        _style("PlayfairDisplay-Bold", ["#0f4a22", "#08331a"], "#f7ffd0", max_size=0.085),
        _style("PTSerif-Bold", "#0d3b1d", "#f7ffd0", accent_color="#d2411e", max_size=0.07, line_spacing=1.12),
        _style("PTSerif-Bold", "#0d3b1d", "#f7ffd0", 0.3, 1.5)),
    "honey_morning": _cfg(
        "Медовий ранок", 13,
        _style("MarckScript-Regular", "#4a2105", "#fff3c4", uppercase=False, max_size=0.125),
        _style("PTSerif-Bold", "#4a2105", "#fff3c4", accent_color="#a3124f", max_size=0.07, line_spacing=1.12),
        _style("PTSerif-Bold", "#4a2105", "#fff3c4", 0.3, 1.5)),
    "rainbow_bokeh": _cfg(
        "Райдужне боке", 14,
        _style("Lobster-Regular", ["#6d28d9", "#be185d"], "#ffffff", uppercase=False, max_size=0.115),
        _style("Comfortaa-Bold", "#3b1f6e", "#ffffff", accent_color="#d9480f", max_size=0.064, line_spacing=1.2),
        _style("Comfortaa-Bold", "#3b1f6e", "#ffffff", 0.3, 1.5)),
    "orange_dawn": _cfg(
        "Помаранчевий світанок", 15,
        _style("Oswald-SemiBold", "#4d0f2f", "#ffe9b8", max_size=0.095),
        _style("PTSansNarrow-Bold", "#4d0f2f", "#ffe9b8", accent_color="#2b1b8a", max_size=0.078),
        _style("Oswald-SemiBold", "#4d0f2f", "#ffe9b8", 0.3, 1.5)),
    "cherry_blossom": _cfg(
        "Квітуча вишня", 16,
        _style("Lobster-Regular", ["#a3124f", "#6b0f3a"], "#ffffff", uppercase=False, max_size=0.115),
        _style("PlayfairDisplay-Bold", "#5b0f33", "#ffffff", accent_color="#1f7a4d", max_size=0.066, line_spacing=1.18),
        _style("PlayfairDisplay-Bold", "#5b0f33", "#ffffff", 0.3, 1.5)),
    "blue_sky": _cfg(
        "Блакитне небо", 17,
        _style("RobotoCondensed-Bold", "#0b2a5b", "#ffffff", max_size=0.093),
        _style("PTSerif-Bold", "#0b2a5b", "#ffffff", accent_color="#c2410c", max_size=0.07, line_spacing=1.12),
        _style("RobotoCondensed-Bold", "#0b2a5b", "#ffffff", 0.3, 1.5)),
    "golden_noon": _cfg(
        "Золотий полудень", 18,
        _style("PlayfairDisplay-Bold", ["#5a2d00", "#3d1d00"], "#fff7d6", max_size=0.085),
        _style("PlayfairDisplay-Bold", "#4a2a00", "#fff7d6", accent_color="#a3124f", max_size=0.068, line_spacing=1.18),
        _style("PlayfairDisplay-Bold", "#4a2a00", "#fff7d6", 0.3, 1.5)),
}


def write_config(tid: str) -> None:
    out = ROOT / tid / "template.json"
    out.write_text(json.dumps(CONFIGS[tid], ensure_ascii=False, indent=2) + "\n", "utf-8")
    print("ok", out)


if __name__ == "__main__":
    names = sys.argv[1:] or list(ALL)
    for nm in names:
        ALL[nm]()
        if nm in CONFIGS:   # конфіги старих 8 шаблонів не чіпаємо
            write_config(nm)
