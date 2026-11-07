"""/pet profile, /settings and the main menu."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.bot.callbacks import Menu, Reminders
from enchanted_companions.bot.handlers.common import safe_edit_text, show_profile
from enchanted_companions.container import Container
from enchanted_companions.db.repositories import Pets

router = Router(name="pet")
router.message.filter(F.chat.type == "private")


async def _profile_or_hint(c: Container, chat_id: int, tg_id: int) -> None:
    if not await show_profile(c, chat_id, tg_id):
        await c.notifier.text(chat_id, c.content.texts.render("common.not_registered"))


@router.message(Command("pet"))
async def pet_cmd(message: Message, container: Container) -> None:
    await _profile_or_hint(container, message.chat.id, message.from_user.id)


@router.callback_query(Menu.filter(F.action == "pet"))
async def pet_menu(query: CallbackQuery, container: Container) -> None:
    await query.answer()
    await _profile_or_hint(container, query.from_user.id, query.from_user.id)


async def _settings(c: Container, chat_id: int, tg_id: int) -> None:
    async with c.sm() as s:
        pet = await Pets.by_owner_tg(s, tg_id)
    t = c.content.texts
    if pet is None:
        await c.notifier.text(chat_id, t.render("common.not_registered"))
        return
    key = "settings.reminders_on" if pet.owner.reminders_on else "settings.reminders_off"
    await c.notifier.text(chat_id, t.render(key), kb.settings(t, pet.owner.reminders_on))


@router.message(Command("settings"))
async def settings_cmd(message: Message, container: Container) -> None:
    await _settings(container, message.chat.id, message.from_user.id)


@router.callback_query(Menu.filter(F.action == "settings"))
async def settings_menu(query: CallbackQuery, container: Container) -> None:
    await query.answer()
    await _settings(container, query.from_user.id, query.from_user.id)


@router.callback_query(Reminders.filter())
async def toggle_reminders(query: CallbackQuery, callback_data: Reminders, container: Container) -> None:
    t = container.content.texts
    await container.sleep.set_reminders(query.from_user.id, callback_data.on)
    await query.answer(t.render("settings.saved"))
    key = "settings.reminders_on" if callback_data.on else "settings.reminders_off"
    await safe_edit_text(query.message, t.render(key), kb.settings(t, callback_data.on))
