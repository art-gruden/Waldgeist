"""Levels and evolution stages derived from total energy. Pure functions."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Progress:
    level: int
    stage: int
    energy: int
    next_threshold: int | None  # None at the maximum level

    @property
    def to_next(self) -> int | None:
        return None if self.next_threshold is None else self.next_threshold - self.energy

    @property
    def is_max(self) -> bool:
        return self.next_threshold is None


def level_for(energy: int, thresholds: list[int]) -> int:
    """Level 1..N for the given total energy. Negative energy still means level 1."""
    return max(1, bisect_right(thresholds, max(energy, 0)))


def stage_for(level: int, stage_starts: list[int]) -> int:
    """Evolution stage 1..len(stage_starts) for a level."""
    return max(1, bisect_right(stage_starts, level))


def progress(energy: int, kept_level: int, thresholds: list[int], stage_starts: list[int]) -> Progress:
    """Progress for display. `kept_level` is the stored level: levels never go down."""
    level = max(level_for(energy, thresholds), kept_level, 1)
    level = min(level, len(thresholds))
    next_threshold = thresholds[level] if level < len(thresholds) else None
    return Progress(level=level, stage=stage_for(level, stage_starts), energy=energy, next_threshold=next_threshold)
