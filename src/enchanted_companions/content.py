"""Editable content: species catalog and all bot texts (content/uk/*.yaml)."""

from __future__ import annotations

import html
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class SpeciesInfo:
    slug: str
    name: str
    emoji: str
    character: str
    favorite_place: str
    trait: str
    sort_order: int


class _Keep(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Texts:
    """Message texts with several variants each. `render` picks one at random."""

    def __init__(self, data: dict[str, Any], rng: random.Random | None = None) -> None:
        self._data = data
        self._rng = rng or random.Random()

    def has(self, key: str) -> bool:
        return self._lookup(key) is not None

    def _lookup(self, key: str) -> Any:
        node: Any = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def render(self, key: str, *, html_values: dict[str, str] | None = None, **values: Any) -> str:
        """Fill a template. Plain values are HTML-escaped; `html_values` are inserted as-is."""
        node = self._lookup(key)
        if node is None:
            raise KeyError(f"text not found: {key}")
        template = self._rng.choice(node) if isinstance(node, list) else str(node)
        escaped = {k: html.escape(str(v)) for k, v in values.items()}
        escaped.update(html_values or {})
        return template.format_map(_Keep(escaped)).strip()


@dataclass
class Content:
    species: list[SpeciesInfo]
    texts: Texts

    def species_by_slug(self, slug: str) -> SpeciesInfo | None:
        return next((s for s in self.species if s.slug == slug), None)

    @classmethod
    def load(cls, content_dir: Path) -> Content:
        with (content_dir / "species.yaml").open(encoding="utf-8") as fh:
            raw_species = yaml.safe_load(fh)["species"]
        species = [
            SpeciesInfo(
                slug=str(s["slug"]),
                name=str(s["name"]),
                emoji=str(s.get("emoji", "✨")),
                character=str(s.get("character", "")),
                favorite_place=str(s.get("favorite_place", "")),
                trait=str(s.get("trait", "")),
                sort_order=i,
            )
            for i, s in enumerate(raw_species)
        ]
        slugs = [s.slug for s in species]
        if len(slugs) != len(set(slugs)):
            raise ValueError("duplicate species slug in species.yaml")
        with (content_dir / "texts.yaml").open(encoding="utf-8") as fh:
            texts = yaml.safe_load(fh)
        return cls(species=species, texts=Texts(texts))
