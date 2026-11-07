"""Typed callback data. aiogram parses and validates it; malformed data never reaches a handler."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

MENU_ACTIONS = {"pet", "code", "visit", "world", "stats", "help", "settings"}


class Menu(CallbackData, prefix="m"):
    action: str


class PickSpecies(CallbackData, prefix="sp"):
    slug: str


class Registration(CallbackData, prefix="rg"):
    action: str  # skip_birthday


class MorningHello(CallbackData, prefix="mh"):
    v: int = 1


class VisitPet(CallbackData, prefix="v"):
    pet_id: int


class Reminders(CallbackData, prefix="rm"):
    on: bool


class NewClassConfirm(CallbackData, prefix="nc"):
    ok: bool
