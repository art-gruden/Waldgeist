"""Тести рендера листівок: python3 tests/test_render.py"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="textcard-test-")
os.environ["DATA_DIR"] = TMP

from PIL import Image  # noqa: E402

from textcard import render as R  # noqa: E402
from textcard.store import TemplateStore  # noqa: E402

TEXT = "Я відкриваю своє серце присутності Творця. Нехай Божественна Мудрість наповнює мої думки, слова, почуття та *вчинки*."
TEXT20 = ("Мені не потрібно шукати світло поза собою, адже я можу відкрити його в глибині власної Душі "
          "і щодня ділитися ним.")


class Typography(unittest.TestCase):
    def test_apostrophe_quotes_dash(self):
        s = R.typography('зв\'язок  "Світло" - це  я...')
        self.assertEqual(s, "зв’язок «Світло» — це я…")

    def test_newlines(self):
        self.assertEqual(R.typography("а\r\n\r\n\r\nб  \n в"), "а\nб\nв")

    def test_words(self):
        self.assertEqual(R.count_words("Я усвідомлюю свій зв’язок із Джерелом Життя."), 7)
        self.assertEqual(R.count_words("*вчинки*, 2026 — так"), 2)

    def test_glue_and_accent(self):
        pars = R.parse_markup(R.typography("відкрити його в глибині власної *Душі*. Я і світ — одне"))
        units = [u.text for u in pars[0].units]
        self.assertIn("в глибині", units)
        self.assertIn("Я і світ —", units)  # «Я», «і» прилипають, тире — до попереднього
        acc = [u for u in pars[0].units if "Душі" in u.text][0]
        self.assertEqual("".join(c for c, a in zip(acc.text, acc.accent) if a), "Душі")

    def test_escaped_star(self):
        pars = R.parse_markup("5 \\* 3")
        self.assertEqual(" ".join(u.text for u in pars[0].units), "5 * 3")

    def test_no_preposition_at_line_end(self):
        font = R.get_font("PTSerif-Bold", 60)
        pars = R.parse_markup(R.typography(TEXT20))
        for width in range(300, 1200, 37):
            lines = R.wrap_paragraphs(pars, font, width)
            if not lines:
                continue
            for ln in lines:
                last = ln.text.split(" ")[-1].lower()
                self.assertNotIn(last, R.GLUE_NEXT, f"рядок закінчується на «{last}»: {ln.text!r}")
                self.assertLessEqual(ln.width, width + 0.5)

    def test_balanced(self):
        font = R.get_font("PTSerif-Bold", 60)
        lines = R.wrap_paragraphs(R.parse_markup(TEXT), font, 1100)
        widths = [l.width for l in lines]
        self.assertLess(max(widths) - min(widths), 0.45 * max(widths), widths)


class Rendering(unittest.TestCase):
    def test_all_templates_all_sizes(self):
        store = TemplateStore(Path(TMP))
        ts = store.all()
        self.assertGreaterEqual(len(ts), 8)
        for t in ts:
            for size, (w, h) in R.SIZES.items():
                for text in (TEXT, TEXT20, "Світло"):
                    res = R.render_card(t.config, t.background, text, "Енергозавантаження", "dmphoto.in.ua", size)
                    self.assertEqual(res.image.size, (w, h))
                    self.assertEqual(res.warnings, [], (t.id, size))
                    self.assertGreaterEqual(res.body_size, 12)

    def test_text_changes_pixels(self):
        t = TemplateStore(Path(TMP)).get("pink_glow")
        a = R.render_card(t.config, t.background, TEXT).image
        b = R.render_card(t.config, t.background, TEXT20).image
        self.assertNotEqual(a.tobytes(), b.tobytes())
        bg = R.cover(R.load_background(t.background), a.size)
        diff = sum(1 for p, q in zip(a.resize((160, 90)).get_flattened_data(), bg.resize((160, 90)).get_flattened_data()) if abs(p[0] - q[0]) > 30)
        self.assertGreater(diff, 300)  # текст справді намальований

    def test_custom_size_and_formats(self):
        t = TemplateStore(Path(TMP)).get("ocean")
        res = R.render_card(t.config, t.background, TEXT, size="1200x630")
        self.assertEqual(res.image.size, (1200, 630))
        for fmt, magic in (("png", b"\x89PNG"), ("jpg", b"\xff\xd8"), ("webp", b"RIFF")):
            data, _ = R.encode(res.image, fmt)
            self.assertTrue(data.startswith(magic))
        with self.assertRaises(R.RenderError):
            R.encode(res.image, "gif")

    def test_errors(self):
        t = TemplateStore(Path(TMP)).get("paper")
        with self.assertRaises(R.RenderError):
            R.render_card(t.config, t.background, "   ")
        with self.assertRaises(R.RenderError):
            R.render_card(t.config, t.background, "Надзвичайнодовгесловобезпробілів" * 8, size="300x300")
        with self.assertRaises(R.RenderError):
            R.render_card(t.config, t.background, " ".join(["слово"] * 1500))
        with self.assertRaises(R.RenderError):
            R.parse_size("10x10", "landscape")
        with self.assertRaises(R.RenderError):
            R.parse_color("червоний")
        with self.assertRaises(R.RenderError):
            R.render_card(t.config, t.background, TEXT, overrides={"body": {"font": "../../etc/passwd"}})

    def test_missing_glyph_warning(self):
        t = TemplateStore(Path(TMP)).get("pink_glow")
        res = R.render_card(t.config, t.background, "Світло всередині 🌞 мене ✨ сяє")
        self.assertTrue(res.warnings and "🌞" in res.warnings[0], res.warnings)

    def test_fallback_apostrophe(self):
        # MarckScript не має ʼ, але має ’ — а якщо й ’ нема, підставиться '
        self.assertTrue(R.has_glyph("MarckScript-Regular", "’"))
        self.assertFalse(R.has_glyph("MarckScript-Regular", "ʼ"))
        self.assertFalse(R.has_glyph("PTSerif-Bold", "🌞"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
