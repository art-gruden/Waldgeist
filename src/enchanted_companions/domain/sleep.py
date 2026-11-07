"""Natural sleep: a purely visual state. It never changes any number."""

from __future__ import annotations

from enum import StrEnum


class SleepState(StrEnum):
    AWAKE = "awake"
    DOZING = "dozing"
    SLEEPING = "sleeping"
    DREAMING = "dreaming"


def sleep_state_for(days_inactive: int, dozing_after: int, sleeping_after: int, dreaming_after: int) -> SleepState:
    if days_inactive >= dreaming_after:
        return SleepState.DREAMING
    if days_inactive >= sleeping_after:
        return SleepState.SLEEPING
    if days_inactive >= dozing_after:
        return SleepState.DOZING
    return SleepState.AWAKE


def shows_sleep_image(state: SleepState) -> bool:
    return state in (SleepState.SLEEPING, SleepState.DREAMING)
