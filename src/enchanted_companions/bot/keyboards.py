"""Inline keyboards. All button labels come from content/uk/texts.yaml."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from enchanted_companions.bot.callbacks import (
    Menu,
    MorningHello,
    NewClassConfirm,
    PickSpecies,
    Registration,
    Reminders,
    VisitPet,
)
from enchanted_companions.content import Texts
from enchanted_companions.db.models import Pet
from enchanted_companions.services.registration import SpeciesSlot


def main_menu(t: Texts) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for action in ("pet", "code", "visit", "world", "stats", "settings", "help"):
        b.button(text=t.render(f"buttons.{action}"), callback_data=Menu(action=action))
    b.adjust(1, 2, 2, 2)
    return b.as_markup()


def species_picker(t: Texts, board: list[SpeciesSlot]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for slot in board:
        sp = slot.species
        label = (
            t.render("register.taken_button", emoji=sp.emoji, species=sp.name, owner=slot.taken_by)
            if slot.taken_by
            else f"{sp.emoji} {sp.name}"
        )
        b.button(text=label, callback_data=PickSpecies(slug=sp.slug))
    b.adjust(2)
    return b.as_markup()


def skip_birthday(t: Texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t.render("buttons.skip"), callback_data=Registration(action="skip_birthday").pack()
                )
            ]
        ]
    )


def morning_card(t: Texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t.render("buttons.good_morning"), callback_data=MorningHello().pack())]
        ]
    )


def open_bot(t: Texts, bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t.render("buttons.open_bot"), url=f"https://t.me/{bot_username}?start=forest")]
        ]
    )


def visit_list(pets: list[Pet], label) -> InlineKeyboardMarkup:  # label: Callable[[Pet], str]
    b = InlineKeyboardBuilder()
    for pet in pets:
        b.button(text=label(pet), callback_data=VisitPet(pet_id=pet.id))
    b.adjust(2)
    return b.as_markup()


def settings(t: Texts, reminders_on: bool) -> InlineKeyboardMarkup:
    key = "buttons.reminders_off" if reminders_on else "buttons.reminders_on"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t.render(key), callback_data=Reminders(on=not reminders_on).pack())],
            [InlineKeyboardButton(text=t.render("buttons.menu"), callback_data=Menu(action="pet").pack())],
        ]
    )


def reminder_off(t: Texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t.render("buttons.reminders_off"), callback_data=Reminders(on=False).pack())]
        ]
    )


def confirm_class(t: Texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t.render("buttons.confirm"), callback_data=NewClassConfirm(ok=True).pack()),
                InlineKeyboardButton(text=t.render("buttons.cancel"), callback_data=NewClassConfirm(ok=False).pack()),
            ]
        ]
    )
