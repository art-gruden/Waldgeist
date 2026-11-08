"""Рендер листівки: фон шаблону + заголовок + текст + підпис.

Лише Pillow, без мережі. Основні кроки:
1. фон масштабується «cover» під потрібний розмір (з точкою фокусу);
2. текст проходить типографіку (апостроф, лапки, тире) і розбивається на «неподільні»
   одиниці (короткі прийменники/сполучники прилипають до наступного слова);
3. перебором розміру шрифту (бінарний пошук) знаходиться найбільший, за якого текст
   влазить у рамку; рядки балансуються динамічним програмуванням (рівні за довжиною);
4. кожен блок малюється в окремий шар: тінь → світіння → обведення → заливка
   (колір, вертикальний градієнт або акцентний колір для *виділених* слів).
"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

Image.MAX_IMAGE_PIXELS = 60_000_000  # захист від «бомб» розпакування

FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"

SIZES: dict[str, tuple[int, int]] = {
    "landscape": (1280, 720),
    "wide": (1600, 900),
    "square": (1080, 1080),
    "portrait": (1080, 1350),
    "story": (1080, 1920),
}
MAX_SIDE = 4096


class RenderError(ValueError):
    """Помилка, яку можна показати користувачеві (текст задовгий, поганий колір тощо)."""


# ---------------------------------------------------------------- шрифти, кольори

def list_fonts() -> list[str]:
    return sorted(p.stem for p in FONTS_DIR.glob("*.ttf"))


@lru_cache(maxsize=512)
def get_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONTS_DIR / f"{name}.ttf"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not path.is_file():
        raise RenderError(f"Невідомий шрифт: {name}")
    return ImageFont.truetype(str(path), max(4, int(size)))


@lru_cache(maxsize=4096)
def has_glyph(font_name: str, ch: str) -> bool:
    if ch.isspace():
        return True
    m = _bitmap(font_name, ch)
    return any(m) and m != _bitmap(font_name, "\U0010fffd")  # не збігається з .notdef


@lru_cache(maxsize=4096)
def _bitmap(font_name: str, ch: str) -> bytes:
    im = Image.new("L", (120, 120), 0)
    ImageDraw.Draw(im).text((20, 20), ch, font=get_font(font_name, 48), fill=255)
    return im.tobytes()


_HEX = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


def parse_color(value: Any) -> tuple[int, int, int, int]:
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        vals = [int(v) for v in value] + [255] * (4 - len(value))
        return tuple(max(0, min(255, v)) for v in vals)  # type: ignore[return-value]
    if not isinstance(value, str) or not _HEX.fullmatch(value.strip()):
        raise RenderError(f"Колір має бути у форматі #RRGGBB або #RRGGBBAA: {value!r}")
    h = value.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 6:
        h += "ff"
    return tuple(int(h[i:i + 2], 16) for i in range(0, 8, 2))  # type: ignore[return-value]


def parse_size(size: str | None, default: str) -> tuple[int, int]:
    s = (size or default or "landscape").strip().lower()
    if s in SIZES:
        return SIZES[s]
    m = re.fullmatch(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})", s)
    if not m:
        raise RenderError(f"Розмір: одна з назв {', '.join(SIZES)} або ШИРИНАxВИСОТА, а не {size!r}")
    w, h = int(m.group(1)), int(m.group(2))
    if not (200 <= w <= MAX_SIDE and 200 <= h <= MAX_SIDE):
        raise RenderError(f"Розмір кожної сторони має бути 200…{MAX_SIDE} px")
    return w, h


# ---------------------------------------------------------------- типографіка

GLUE_NEXT = {
    # короткі слова, які не повинні залишатися в кінці рядка
    "а", "в", "у", "і", "й", "з", "із", "зі", "о", "об", "до", "на", "по", "за", "та", "не", "ні",
    "що", "як", "від", "під", "над", "без", "для", "про", "при", "чи", "ж", "же", "бо", "аж", "ще",
    "the", "a", "an", "to", "of", "in", "on",
}

FALLBACKS = {"’": "'", "ʼ": "'", "«": '"', "»": '"', "—": "-", "–": "-", "…": "...", "„": '"', "“": '"', "”": '"'}


def typography(text: str) -> str:
    s = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = re.sub(r"\n{2,}", "\n", s).strip()
    s = re.sub(r"(?<=\w)['`ʼ´‘](?=\w)", "’", s)          # апостроф: зв'язок -> зв’язок
    s = re.sub(r'"([^"\n]+)"', r"«\1»", s)               # "лапки" -> «лапки»
    s = re.sub(r"(?<=\s)(--?|–)(?=\s)", "—", s)          # тире
    s = re.sub(r"(?m)^(--?|–)(?=\s)", "—", s)
    s = s.replace("...", "…")
    return s


@dataclass
class Unit:
    """Неподільна одиниця переносу: одне або кілька «склеєних» слів."""
    text: str
    accent: list[bool]  # для кожного символу text


@dataclass
class Paragraph:
    units: list[Unit]


def parse_markup(text: str) -> list[Paragraph]:
    """*виділення* -> акцентний колір. Порожні рядки вже прибрані, \\n — примусовий перенос."""
    paragraphs: list[Paragraph] = []
    accent = False
    for raw_line in text.split("\n"):
        chars: list[tuple[str, bool]] = []
        i = 0
        while i < len(raw_line):
            c = raw_line[i]
            if c == "\\" and i + 1 < len(raw_line) and raw_line[i + 1] == "*":
                chars.append(("*", accent))
                i += 2
                continue
            if c == "*":
                accent = not accent
            else:
                chars.append((c, accent))
            i += 1
        words: list[Unit] = []
        cur_t, cur_a = [], []
        for c, a in chars + [(" ", False)]:
            if c == " ":
                if cur_t:
                    words.append(Unit("".join(cur_t), cur_a))
                    cur_t, cur_a = [], []
            else:
                cur_t.append(c)
                cur_a.append(a)
        if not words:
            continue
        # склеювання: короткі слова з наступним; тире — з попереднім
        units: list[Unit] = []
        pending: Unit | None = None
        for w in words:
            if pending is not None:
                w = Unit(pending.text + " " + w.text, pending.accent + [pending.accent[-1] and w.accent[0]] + w.accent)
                pending = None
            core = w.text.split(" ")[-1].strip("«»\"'(").lower()
            if w.text in ("—", "-", "–") and units:
                prev = units.pop()
                w = Unit(prev.text + " " + w.text, prev.accent + [False] + w.accent)
                units.append(w)
                continue
            if core in GLUE_NEXT or len(core) == 1:
                pending = w
                continue
            units.append(w)
        if pending is not None:
            if units:
                prev = units.pop()
                units.append(Unit(prev.text + " " + pending.text, prev.accent + [False] + pending.accent))
            else:
                units.append(pending)
        paragraphs.append(Paragraph(units))
    return paragraphs


def count_words(text: str) -> int:
    return len(re.findall(r"[^\W\d_]+(?:[’'ʼ-][^\W\d_]+)*", text))


def sanitize_for_font(paragraphs: list[Paragraph], font: str) -> set[str]:
    """Замінює символи, яких немає у шрифті; повертає множину відкинутих символів."""
    dropped: set[str] = set()
    for p in paragraphs:
        for u in p.units:
            out_t, out_a = [], []
            for ch, a in zip(u.text, u.accent):
                if has_glyph(font, ch):
                    out_t.append(ch); out_a.append(a)
                elif ch in FALLBACKS and all(has_glyph(font, c) for c in FALLBACKS[ch]):
                    out_t.extend(FALLBACKS[ch]); out_a.extend([a] * len(FALLBACKS[ch]))
                else:
                    dropped.add(ch)
            u.text, u.accent = "".join(out_t), out_a
        p.units = [u for u in p.units if u.text.strip()]
    return dropped


# ---------------------------------------------------------------- розбиття на рядки

@dataclass
class Line:
    text: str
    accent: list[bool]
    width: float


def _join(units: list[Unit]) -> tuple[str, list[bool]]:
    t, a = [], []
    for i, u in enumerate(units):
        if i:
            t.append(" "); a.append(False)
        t.append(u.text); a.extend(u.accent)
    return "".join(t), a


def wrap_balanced(units: list[Unit], font: ImageFont.FreeTypeFont, max_w: float) -> list[Line] | None:
    """Мінімізує нерівність рядків (сума квадратів «недобору»); None — якщо слово ширше рядка."""
    n = len(units)
    if n == 0:
        return []
    width = {}

    def w(i, j):
        if (i, j) not in width:
            width[(i, j)] = font.getlength(_join(units[i:j])[0])
        return width[(i, j)]

    INF = float("inf")
    best = [INF] * (n + 1)
    prev = [0] * (n + 1)
    best[0] = 0.0
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            lw = w(i, j)
            if lw > max_w:
                break
            slack = (max_w - lw) / max_w
            cost = best[i] + slack * slack + 0.08  # +штраф за кожен рядок
            if j == n and j - i == 1 and n > 2:
                cost += 0.35                      # одне слово в останньому рядку
            if cost < best[j]:
                best[j], prev[j] = cost, i
        if best[j] == INF:
            return None
    lines: list[Line] = []
    j = n
    while j > 0:
        i = prev[j]
        t, a = _join(units[i:j])
        lines.append(Line(t, a, w(i, j)))
        j = i
    lines.reverse()
    # другий прохід: вирівнюємо під ширину найдовшого рядка (рівніші рядки, та сама кількість)
    widest = max(l.width for l in lines)
    if len(lines) > 1 and widest < max_w * 0.98:
        again = wrap_balanced_fixed(units, font, widest, len(lines), w)
        if again:
            return again
    return lines


def wrap_balanced_fixed(units, font, max_w, nlines, w) -> list[Line] | None:
    """Те саме DP, але з фіксованою кількістю рядків і мінімізацією найдовшого/розкиду."""
    n = len(units)
    INF = float("inf")
    best = [[INF] * (n + 1) for _ in range(nlines + 1)]
    prev = [[0] * (n + 1) for _ in range(nlines + 1)]
    best[0][0] = 0.0
    for k in range(1, nlines + 1):
        for j in range(1, n + 1):
            for i in range(j - 1, -1, -1):
                lw = w(i, j)
                if lw > max_w * 1.0001:
                    break
                if best[k - 1][i] == INF:
                    continue
                slack = (max_w - lw) / max_w
                cost = best[k - 1][i] + slack * slack
                if k == nlines and j - i == 1 and n > 2:
                    cost += 0.35
                if cost < best[k][j]:
                    best[k][j], prev[k][j] = cost, i
    if best[nlines][n] == INF:
        return None
    lines = []
    j, k = n, nlines
    while k > 0:
        i = prev[k][j]
        t, a = _join(units[i:j])
        lines.append(Line(t, a, w(i, j)))
        j, k = i, k - 1
    lines.reverse()
    return lines


def wrap_paragraphs(pars: list[Paragraph], font, max_w) -> list[Line] | None:
    out: list[Line] = []
    for p in pars:
        ls = wrap_balanced(p.units, font, max_w)
        if ls is None:
            return None
        out.extend(ls)
    return out


# ---------------------------------------------------------------- стиль і шари

DEFAULT_STYLE: dict[str, Any] = {
    "font": "PTSerif-Bold",
    "color": "#ffffff",
    "accent_color": None,
    "uppercase": False,
    "max_size": 0.08,       # частка від sqrt(ширина*висота)
    "min_size": 0.025,
    "line_spacing": 1.12,
    "condense": 1.0,        # <1 — горизонтальне стиснення (вужчий шрифт)
    "align": "center",
    "stroke_width": 0.0,    # частка від кегля
    "stroke_color": "#000000",
    "glow_color": None,
    "glow_radius": 0.22,    # частка від кегля
    "glow_strength": 1.6,
    "shadow_color": None,
    "shadow_offset": [0.03, 0.05],
    "shadow_blur": 0.06,
    "opacity": 1.0,
}


def merge_style(*parts: dict | None) -> dict[str, Any]:
    st = dict(DEFAULT_STYLE)
    for p in parts:
        if p:
            st.update({k: v for k, v in p.items() if v is not None or k in ("glow_color", "shadow_color", "accent_color")})
    return st


def _fill_layer(mask: Image.Image, color: Any) -> Image.Image:
    """RGBA-шар кольору (або вертикального градієнта [верх, низ]) з альфою mask."""
    w, h = mask.size
    if isinstance(color, (list, tuple)) and len(color) == 2 and not isinstance(color[0], int):
        c0, c1 = parse_color(color[0]), parse_color(color[1])
        grad = Image.linear_gradient("L").resize((w, h))
        layer = Image.composite(Image.new("RGBA", (w, h), c1), Image.new("RGBA", (w, h), c0), grad)
    else:
        layer = Image.new("RGBA", (w, h), parse_color(color))
    a = ImageChops.multiply(layer.getchannel("A"), mask)
    layer.putalpha(a)
    return layer


def _amplify(mask: Image.Image, k: float) -> Image.Image:
    return mask.point(lambda v: min(255, int(v * k)))


@dataclass
class Block:
    lines: list[Line]
    font_name: str
    size: int
    style: dict

    @property
    def font(self):
        return get_font(self.font_name, self.size)

    def metrics(self):
        asc, desc = self.font.getmetrics()
        lh = self.size * float(self.style["line_spacing"])
        return asc, desc, lh

    def text_height(self) -> float:
        asc, desc, lh = self.metrics()
        return asc + desc + (len(self.lines) - 1) * lh

    def text_width(self) -> float:
        return max((l.width for l in self.lines), default=0) * float(self.style["condense"])

    def render(self) -> tuple[Image.Image, int, int]:
        """Повертає (шар RGBA, відступ_x, відступ_y) — відступи навколо самого тексту."""
        st, font, size = self.style, self.font, self.size
        asc, desc, lh = self.metrics()
        sw = max(0, round(size * float(st["stroke_width"])))
        glow_r = size * float(st["glow_radius"]) if st.get("glow_color") else 0
        sh_off = st["shadow_offset"] if st.get("shadow_color") else [0, 0]
        sh_blur = size * float(st["shadow_blur"]) if st.get("shadow_color") else 0
        pad = int(math.ceil(sw + max(glow_r * 2.2, sh_blur * 2.5 + abs(sh_off[0] * size) + abs(sh_off[1] * size)) + 4))
        tw = int(math.ceil(max(l.width for l in self.lines)))
        th = int(math.ceil(self.text_height()))
        W, H = tw + 2 * pad, th + 2 * pad

        m_norm = Image.new("L", (W, H), 0)
        m_acc = Image.new("L", (W, H), 0)
        m_stroke = Image.new("L", (W, H), 0) if sw else None
        dn, da = ImageDraw.Draw(m_norm), ImageDraw.Draw(m_acc)
        ds = ImageDraw.Draw(m_stroke) if sw else None
        align = st.get("align", "center")
        for k, ln in enumerate(self.lines):
            if align == "left":
                x0 = pad
            elif align == "right":
                x0 = pad + tw - ln.width
            else:
                x0 = pad + (tw - ln.width) / 2
            y = pad + asc + k * lh
            if ds is not None:
                ds.text((x0, y), ln.text, font=font, fill=255, anchor="ls", stroke_width=sw, stroke_fill=255)
            # сегменти однакового кольору
            i = 0
            while i < len(ln.text):
                j = i
                while j < len(ln.text) and ln.accent[j] == ln.accent[i]:
                    j += 1
                seg = ln.text[i:j]
                if seg.strip():
                    xs = x0 + font.getlength(ln.text[:i])
                    (da if ln.accent[i] and st.get("accent_color") else dn).text((xs, y), seg, font=font, fill=255, anchor="ls")
                i = j

        full = ImageChops.lighter(m_norm, m_acc)
        if m_stroke is not None:
            full = ImageChops.lighter(full, m_stroke)
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        if st.get("shadow_color"):
            sm = full.filter(ImageFilter.GaussianBlur(max(0.1, sh_blur)))
            sl = _fill_layer(sm, st["shadow_color"])
            off = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            off.alpha_composite(sl, (int(round(sh_off[0] * size)), int(round(sh_off[1] * size))))
            out.alpha_composite(off)
        if st.get("glow_color"):
            k = float(st["glow_strength"])
            inner = _amplify(full.filter(ImageFilter.GaussianBlur(max(0.5, glow_r * 0.3))), 2.2 * k)
            outer = _amplify(full.filter(ImageFilter.GaussianBlur(max(0.5, glow_r))), 1.0 * k)
            out.alpha_composite(_fill_layer(ImageChops.lighter(inner, outer), st["glow_color"]))
        if m_stroke is not None:
            out.alpha_composite(_fill_layer(m_stroke, st["stroke_color"]))
        out.alpha_composite(_fill_layer(m_norm, st["color"]))
        if st.get("accent_color"):
            out.alpha_composite(_fill_layer(m_acc, st["accent_color"]))

        op = float(st.get("opacity", 1.0))
        if op < 1:
            out.putalpha(out.getchannel("A").point(lambda v: int(v * op)))
        cx = float(st["condense"])
        if abs(cx - 1) > 1e-3:
            out = out.resize((max(1, round(W * cx)), H), Image.LANCZOS)
            return out, round(pad * cx), pad
        return out, pad, pad


# ---------------------------------------------------------------- фон

@lru_cache(maxsize=16)
def _load_bg(path: str, mtime: float) -> Image.Image:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def load_background(path: Path) -> Image.Image:
    return _load_bg(str(path), path.stat().st_mtime)


def cover(im: Image.Image, size: tuple[int, int], focus=(0.5, 0.5)) -> Image.Image:
    W, H = size
    scale = max(W / im.width, H / im.height)
    nw, nh = max(W, round(im.width * scale)), max(H, round(im.height * scale))
    fx, fy = focus
    left = min(max(0, round(nw * fx - W / 2)), nw - W)
    top = min(max(0, round(nh * fy - H / 2)), nh - H)
    # обрізаємо в координатах оригіналу, потім масштабуємо (швидше, ніж масштабувати все)
    box = (left / scale, top / scale, (left + W) / scale, (top + H) / scale)
    return im.resize((W, H), Image.LANCZOS, box=box, reducing_gap=3.0)


def apply_overlay(img: Image.Image, ov: dict | None, text_box: tuple[int, int, int, int] | None) -> Image.Image:
    if not ov:
        return img
    W, H = img.size
    img = img.convert("RGBA")
    if ov.get("color") and float(ov.get("opacity", 0)) > 0:
        r, g, b, _ = parse_color(ov["color"])
        img.alpha_composite(Image.new("RGBA", (W, H), (r, g, b, int(255 * float(ov["opacity"])))))
    v = float(ov.get("vignette", 0))
    if v > 0:
        rg = Image.radial_gradient("L").resize((W, H))  # 0 у центрі -> 255 на краях
        a = rg.point(lambda x: int(min(255, max(0, (x - 90) * 1.6)) * v))
        img.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (W, H), 0).split(), a)))
    panel = ov.get("panel")
    if panel and text_box:
        x0, y0, x1, y1 = text_box
        rad = int(min(W, H) * float(panel.get("radius", 0.03)))
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=parse_color(panel.get("color", "#00000066")))
        if panel.get("blur"):
            layer = layer.filter(ImageFilter.GaussianBlur(float(panel["blur"])))
        img.alpha_composite(layer)
    return img


# ---------------------------------------------------------------- головна функція

@dataclass
class RenderResult:
    image: Image.Image
    warnings: list[str]
    body_size: int


def _prepare(text: str, st: dict) -> tuple[list[Paragraph], set[str]]:
    t = typography(text)
    if st.get("uppercase"):
        t = t.upper()
    pars = parse_markup(t)
    dropped = sanitize_for_font(pars, st["font"])
    pars = [p for p in pars if p.units]
    return pars, dropped


def render_card(template: dict, background: Path, text: str, title: str | None = None,
                footer: str | None = None, size: str | None = None,
                overrides: dict | None = None) -> RenderResult:
    overrides = overrides or {}
    W, H = parse_size(size, template.get("default_size", "landscape"))
    base = math.sqrt(W * H)  # масштаб кегля: середнє геометричне сторін
    warnings: list[str] = []

    body_st = merge_style(template.get("body"), overrides.get("body"))
    title_st = merge_style({"max_size": 0.11, "min_size": 0.03, "uppercase": True}, template.get("title"), overrides.get("title"))
    footer_st = merge_style({"max_size": 0.024, "min_size": 0.014, "glow_color": None, "shadow_color": None},
                            template.get("footer"), overrides.get("footer"))
    for st in (body_st, title_st, footer_st):
        get_font(st["font"], 12)  # перевірка назви шрифту

    if not text or not text.strip():
        raise RenderError("Порожній текст")
    body_pars, dropped = _prepare(text, body_st)
    title_pars, d2 = _prepare(title, title_st) if title and title.strip() else ([], set())
    footer_pars, d3 = _prepare(footer, footer_st) if footer and footer.strip() else ([], set())
    dropped |= d2 | d3
    if dropped:
        warnings.append("Шрифт не містить символів, їх пропущено: " + " ".join(sorted(dropped)))
    if not body_pars:
        raise RenderError("Після обробки текст порожній (шрифт не містить жодного символу?)")

    bx = template.get("box", [0.07, 0.08, 0.93, 0.92])
    x0, y0, x1, y1 = bx[0] * W, bx[1] * H, bx[2] * W, bx[3] * H
    ov = template.get("overlay") or {}
    panel = ov.get("panel")
    panel_box = None
    if panel:
        pad = float(panel.get("padding", 0.04)) * base
        panel_box = (x0, y0, x1, y1)
        x0, y0, x1, y1 = x0 + pad, y0 + pad, x1 - pad, y1 - pad

    # підпис — внизу рамки, займає свою висоту
    footer_block = None
    if footer_pars:
        fs = max(8, round(float(footer_st["max_size"]) * base))
        lines = wrap_paragraphs(footer_pars, get_font(footer_st["font"], fs), (x1 - x0) / float(footer_st["condense"]))
        while lines is None and fs > 8:
            fs -= 1
            lines = wrap_paragraphs(footer_pars, get_font(footer_st["font"], fs), (x1 - x0) / float(footer_st["condense"]))
        if lines:
            footer_block = Block(lines[:2], footer_st["font"], fs, footer_st)
            y1 -= footer_block.text_height() + fs * 0.8

    box_w, box_h = x1 - x0, y1 - y0
    bmax = max(8, round(float(body_st["max_size"]) * base))
    bmin = max(8, round(float(body_st["min_size"]) * base))
    tmax = round(float(title_st["max_size"]) * base)
    tmin = max(8, round(float(title_st["min_size"]) * base))
    ratio = tmax / bmax if bmax else 1
    gap_k = float((template.get("title") or {}).get("gap", 0.45))

    def layout(bsize: int):
        body_lines = wrap_paragraphs(body_pars, get_font(body_st["font"], bsize), box_w / float(body_st["condense"]))
        if body_lines is None:
            return None
        body = Block(body_lines, body_st["font"], bsize, body_st)
        tblock = None
        total = body.text_height()
        if title_pars:
            ts = max(tmin, min(tmax, round(bsize * ratio)))
            tl = None
            while ts >= tmin:
                tl = wrap_paragraphs(title_pars, get_font(title_st["font"], ts), box_w / float(title_st["condense"]))
                if tl is not None and len(tl) <= 2:
                    break
                ts -= max(1, ts // 30)
            if tl is None or ts < tmin:
                return None
            tblock = Block(tl, title_st["font"], ts, title_st)
            total += tblock.text_height() + ts * gap_k
        if total > box_h:
            return None
        return body, tblock, total

    lo, hi, best = bmin, bmax, None
    while lo <= hi:
        mid = (lo + hi) // 2
        res = layout(mid)
        if res:
            best, lo = (mid, res), mid + 1
        else:
            hi = mid - 1
    if best is None:
        raise RenderError("Текст задовгий для цього шаблону й розміру: скоротіть його або виберіть інший формат")
    bsize, (body, tblock, total) = best

    # --- малювання
    img = cover(load_background(background), (W, H), tuple(template.get("focus", (0.5, 0.5))))
    if panel_box:
        # панель облягає текст по висоті, а не всю рамку
        pad = float(panel.get("padding", 0.04)) * base
        valign = template.get("valign", "center")
        top = _vstart(y0, y1, total, valign)
        fh = footer_block.text_height() + footer_block.size * 0.8 if footer_block else 0
        panel_box = (int(panel_box[0]), int(top - pad), int(panel_box[2]), int(top + total + fh + pad))
    img = apply_overlay(img, ov, panel_box).convert("RGBA")

    y = _vstart(y0, y1, total, template.get("valign", "center"))
    for blk, gap in ((tblock, tblock.size * gap_k if tblock else 0), (body, 0)):
        if blk is None:
            continue
        layer, px, py = blk.render()
        align = blk.style.get("align", "center")
        tw = blk.text_width()
        if align == "left":
            x = x0
        elif align == "right":
            x = x1 - tw
        else:
            x = x0 + (box_w - tw) / 2
        img.alpha_composite(layer, (int(round(x - px)), int(round(y - py))))
        y += blk.text_height() + gap
    if footer_block:
        layer, px, py = footer_block.render()
        tw = footer_block.text_width()
        if panel_box:   # усередині панелі, одразу під текстом
            fy = y + footer_block.size * 0.8
        else:           # унизу рамки
            fy = bx[3] * H - footer_block.text_height()
        img.alpha_composite(layer, (int(round(x0 + (box_w - tw) / 2 - px)), int(round(fy - py))))

    return RenderResult(img.convert("RGB"), warnings, bsize)


def _vstart(y0, y1, total, valign):
    if valign == "top":
        return y0
    if valign == "bottom":
        return y1 - total
    return y0 + (y1 - y0 - total) / 2


def encode(img: Image.Image, fmt: str) -> tuple[bytes, str]:
    fmt = (fmt or "png").lower()
    buf = io.BytesIO()
    if fmt in ("jpg", "jpeg"):
        img.save(buf, "JPEG", quality=92, optimize=True, progressive=True, subsampling=0)
        return buf.getvalue(), "image/jpeg"
    if fmt == "webp":
        img.save(buf, "WEBP", quality=90, method=4)
        return buf.getvalue(), "image/webp"
    if fmt == "png":
        img.save(buf, "PNG", compress_level=6)
        return buf.getvalue(), "image/png"
    raise RenderError("Формат: png, jpg або webp")
