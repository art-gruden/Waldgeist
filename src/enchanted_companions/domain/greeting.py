"""Morning greeting detection for Ukrainian, Russian and English (and German) messages."""

from __future__ import annotations

import re
import unicodedata

_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "ё": "е", "Ё": "Е"})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).translate(_APOSTROPHES).lower()
    return re.sub(r"\s+", " ", text).strip()


class GreetingDetector:
    def __init__(self, patterns: list[str]) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]

    def is_greeting(self, text: str | None) -> bool:
        if not text:
            return False
        normalized = normalize(text)
        return any(p.search(normalized) for p in self._patterns)
