"""Шаблони: вбудовані (templates/ у коді, лише читання) + власні (DATA_DIR/templates, з вебсторінки/API)."""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .render import RenderError, get_font, parse_color, SIZES

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "templates"
ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
STYLE_KEYS = {
    "font", "color", "accent_color", "uppercase", "max_size", "min_size", "line_spacing", "condense",
    "align", "stroke_width", "stroke_color", "glow_color", "glow_radius", "glow_strength",
    "shadow_color", "shadow_offset", "shadow_blur", "opacity", "gap",
}
MAX_BG_SIDE = 2560


@dataclass
class Template:
    id: str
    dir: Path
    config: dict
    custom: bool

    @property
    def background(self) -> Path:
        return self.dir / self.config.get("background", "background.jpg")

    @property
    def mtime(self) -> float:
        return max(self.background.stat().st_mtime, (self.dir / "template.json").stat().st_mtime)

    def public(self) -> dict:
        return {"id": self.id, "name": self.config.get("name", self.id), "custom": self.custom,
                "default_size": self.config.get("default_size", "landscape")}


def validate_style(st: dict, where: str) -> dict:
    if not isinstance(st, dict):
        raise RenderError(f"{where}: очікується об'єкт")
    bad = set(st) - STYLE_KEYS
    if bad:
        raise RenderError(f"{where}: невідомі поля {', '.join(sorted(bad))}")
    out = {}
    for k, v in st.items():
        if v is None:
            out[k] = None
            continue
        if k == "font":
            get_font(str(v), 12)
        elif k in ("color",):
            if isinstance(v, list) and len(v) == 2 and all(isinstance(c, str) for c in v):
                for c in v:
                    parse_color(c)
            else:
                parse_color(v)
        elif k.endswith("_color"):
            parse_color(v)
        elif k == "align":
            if v not in ("left", "center", "right"):
                raise RenderError(f"{where}.align: left, center або right")
        elif k == "uppercase":
            v = bool(v)
        elif k == "shadow_offset":
            if not (isinstance(v, list) and len(v) == 2):
                raise RenderError(f"{where}.shadow_offset: [x, y]")
            v = [max(-0.3, min(0.3, float(x))) for x in v]
        else:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise RenderError(f"{where}.{k}: очікується число")
            limits = {"max_size": (0.01, 0.4), "min_size": (0.005, 0.3), "line_spacing": (0.7, 3),
                      "condense": (0.5, 1.5), "stroke_width": (0, 0.3), "glow_radius": (0, 1.5),
                      "glow_strength": (0, 6), "shadow_blur": (0, 1), "opacity": (0, 1), "gap": (0, 3)}
            lo, hi = limits.get(k, (-1e9, 1e9))
            if not lo <= v <= hi:
                raise RenderError(f"{where}.{k}: допустимо {lo}…{hi}")
        out[k] = v
    return out


def validate_config(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise RenderError("Шаблон: очікується JSON-об'єкт")
    out = dict(cfg)
    out["name"] = str(cfg.get("name") or "Без назви")[:60]
    if out.get("default_size", "landscape") not in SIZES:
        raise RenderError(f"default_size: одна з {', '.join(SIZES)}")
    for key in ("box",):
        if key in cfg:
            b = cfg[key]
            if not (isinstance(b, list) and len(b) == 4 and 0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1):
                raise RenderError("box: [x0, y0, x1, y1] у частках 0…1, x0<x1, y0<y1")
    if "focus" in cfg:
        f = cfg["focus"]
        if not (isinstance(f, list) and len(f) == 2 and all(0 <= float(v) <= 1 for v in f)):
            raise RenderError("focus: [x, y] у частках 0…1")
    if cfg.get("valign", "center") not in ("top", "center", "bottom"):
        raise RenderError("valign: top, center або bottom")
    for part in ("title", "body", "footer"):
        if part in cfg:
            out[part] = validate_style(cfg[part], part)
    ov = cfg.get("overlay")
    if ov:
        if ov.get("color"):
            parse_color(ov["color"])
        if ov.get("panel") and ov["panel"].get("color"):
            parse_color(ov["panel"]["color"])
    return out


class TemplateStore:
    def __init__(self, data_dir: Path):
        self.custom_dir = data_dir / "templates"
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _load(self, d: Path, custom: bool) -> Template | None:
        try:
            cfg = json.loads((d / "template.json").read_text("utf-8"))
            t = Template(d.name, d, cfg, custom)
            return t if t.background.is_file() else None
        except (OSError, ValueError):
            return None

    def all(self) -> list[Template]:
        out = []
        for base, custom in ((BUILTIN_DIR, False), (self.custom_dir, True)):
            if base.is_dir():
                for d in sorted(base.iterdir()):
                    if d.is_dir() and ID_RE.fullmatch(d.name):
                        t = self._load(d, custom)
                        if t and not (custom and any(x.id == t.id for x in out)):
                            out.append(t)
        out.sort(key=lambda t: (t.custom, t.config.get("order", 999), t.config.get("name", t.id)))
        return out

    def get(self, tid: str) -> Template:
        if not tid or not ID_RE.fullmatch(tid):
            raise KeyError(tid)
        for base, custom in ((BUILTIN_DIR, False), (self.custom_dir, True)):
            d = base / tid
            if d.is_dir():
                t = self._load(d, custom)
                if t:
                    return t
        raise KeyError(tid)

    def create(self, name: str, image_bytes: bytes, base_id: str, focus=None) -> Template:
        base = self.get(base_id)
        try:
            im = Image.open(io.BytesIO(image_bytes))
            im.load()
        except Exception:
            raise RenderError("Не вдалося прочитати зображення (підтримуються JPEG, PNG, WebP, …)")
        im = ImageOps.exif_transpose(im)
        if im.width < 400 or im.height < 400:
            raise RenderError("Зображення замале: потрібно щонайменше 400×400 px")
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        im = im.convert("RGB")
        im.thumbnail((MAX_BG_SIDE, MAX_BG_SIDE), Image.LANCZOS)
        cfg = {k: v for k, v in base.config.items() if k not in ("order",)}
        cfg["name"] = (name or "Мій фон").strip()[:60] or "Мій фон"
        cfg["background"] = "background.jpg"
        cfg["based_on"] = base.id
        if focus:
            cfg["focus"] = focus
        cfg = validate_config(cfg)
        tid = "u-" + secrets.token_hex(4)
        tmp = self.custom_dir / f".{tid}.tmp"
        tmp.mkdir()
        try:
            im.save(tmp / "background.jpg", quality=90, optimize=True, progressive=True)
            (tmp / "template.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, self.custom_dir / tid)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return self.get(tid)

    def update(self, tid: str, cfg: dict) -> Template:
        t = self.get(tid)
        if not t.custom:
            raise PermissionError("Вбудовані шаблони змінювати не можна: створіть власний на їхній основі")
        cfg = validate_config({**cfg, "background": "background.jpg"})
        with self.lock:
            p = t.dir / "template.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, p)
        return self.get(tid)

    def delete(self, tid: str) -> None:
        t = self.get(tid)
        if not t.custom:
            raise PermissionError("Вбудовані шаблони видаляти не можна")
        shutil.rmtree(t.dir)
