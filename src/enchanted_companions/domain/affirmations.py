"""Affirmations and spiritual laws from Omniya's lessons, given to guests of her pet.

Reads the existing `affirmations.yaml` (sections: templates, affirmations, laws) without changes.
"""

from __future__ import annotations

import html
import logging
import random
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_DEFAULT_TEMPLATES = {
    "affirmation": "🌟 <b>Афірмація для тебе</b>\n\n{text}\n{closing}",
    "law": "📜 <b>{name}</b>\n\n{essence}\n{quote_block}{practice_block}",
    "quote_block": "\n💬 <i>«{quote}»</i>\n",
    "practice_block": "\n✨ <b>У житті:</b> {practice}\n",
}


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    kind: str  # "affirmation" | "law"
    fields: dict[str, str]


class _Blank(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


class AffirmationCatalog:
    def __init__(self, entries: list[Entry], templates: dict[str, str]) -> None:
        self.entries = entries
        self.templates = {**_DEFAULT_TEMPLATES, **templates}
        self._by_id = {e.id: e for e in entries}

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, entry_id: str) -> Entry | None:
        return self._by_id.get(entry_id)

    @classmethod
    def empty(cls) -> AffirmationCatalog:
        return cls([], {})

    @classmethod
    def load(cls, path: Path) -> AffirmationCatalog:
        if not path.exists():
            log.warning(
                "affirmations file not found, oracle visits will give no affirmation", extra={"path": str(path)}
            )
            return cls.empty()
        with path.open(encoding="utf-8") as fh:
            data: dict[str, Any] = yaml.safe_load(fh) or {}
        entries: list[Entry] = []
        for kind, section in (("affirmation", "affirmations"), ("law", "laws")):
            for raw in data.get(section) or []:
                if not isinstance(raw, dict) or "id" not in raw:
                    continue
                fields = {k: str(v).strip() for k, v in raw.items() if isinstance(v, str | int | float)}
                entries.append(Entry(id=str(raw["id"]), kind=kind, fields=fields))
        templates = {k: str(v) for k, v in (data.get("templates") or {}).items() if isinstance(v, str)}
        ids = [e.id for e in entries]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate ids in {path}")
        log.info("affirmations loaded", extra={"count": len(entries)})
        return cls(entries, templates)

    def pick(self, seen_ids: Iterable[str], rng: random.Random | None = None) -> Entry | None:
        """A random entry the guest has not seen yet; a new round starts once all were seen."""
        if not self.entries:
            return None
        rng = rng or random.Random()
        seen = set(seen_ids)
        pool = [e for e in self.entries if e.id not in seen] or self.entries
        return rng.choice(pool)

    def render(self, entry: Entry) -> str:
        """HTML for Telegram (parse_mode=HTML). Field values are escaped; templates are trusted."""
        values = _Blank({k: html.escape(v) for k, v in entry.fields.items()})
        if entry.kind == "law":
            values["quote_block"] = (
                self.templates["quote_block"].format_map(values) if entry.fields.get("quote") else ""
            )
            values["practice_block"] = (
                self.templates["practice_block"].format_map(values) if entry.fields.get("practice") else ""
            )
        text = self.templates[entry.kind].format_map(values)
        return "\n".join(line.rstrip() for line in text.strip().splitlines()).replace("\n\n\n", "\n\n")
