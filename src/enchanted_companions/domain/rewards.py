"""Reward sources and idempotency keys. One key = at most one reward, enforced by the database."""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum


class Source(StrEnum):
    MORNING = "morning"
    CLASS_LIVE = "class_live"
    CLASS_RECORDING = "class_recording"
    VISIT_HOST = "visit_host"
    VISIT_GUEST = "visit_guest"
    ADMIN_ADJUST = "admin_adjust"


def morning_key(pet_id: int, day: date) -> str:
    return f"morning:{pet_id}:{day.isoformat()}"


def class_key(session_id: int, pet_id: int) -> str:
    """Shared by live and recording rewards: a class pays once, never both."""
    return f"class:{session_id}:{pet_id}"


def visit_keys(guest_pet_id: int, day: date) -> tuple[str, str]:
    base = f"visit:{guest_pet_id}:{day.isoformat()}"
    return f"{base}:guest", f"{base}:host"


def admin_key() -> str:
    return f"admin:{uuid.uuid4()}"
