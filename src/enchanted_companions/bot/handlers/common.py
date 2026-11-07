"""Shared helpers for handlers."""

from __future__ import annotations

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.container import Container
from enchanted_companions.db.repositories import Pets
from enchanted_companions.services.reward_engine import GrantResult

_MEMBER = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.RESTRICTED,
}


async def is_group_member(c: Container, tg_id: int) -> bool:
    """Only members of the study group can play. Without GROUP_CHAT_ID everyone may register."""
    group = c.settings.group_chat_id
    if not group:
        return True
    try:
        member = await c.bot.get_chat_member(group, tg_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    return member.status in _MEMBER


async def safe_edit_markup(message: object, markup=None) -> None:
    """Remove/replace an inline keyboard; ignore 'message is not modified' and similar."""
    if isinstance(message, Message):
        try:
            await message.edit_reply_markup(reply_markup=markup)
        except TelegramBadRequest:
            pass


async def safe_edit_text(message: object, text: str, markup=None) -> None:
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass


async def show_profile(c: Container, chat_id: int, tg_id: int) -> bool:
    view = await c.pets.profile(tg_id)
    if view is None:
        return False
    await c.notifier.photo(
        chat_id, c.messenger.profile_image(view), c.messenger.profile_caption(view), kb.main_menu(c.content.texts)
    )
    return True


async def announce_grants(c: Container, *grants: tuple[GrantResult | None, int, int]) -> None:
    """Announce level-ups. Each item: (grant, pet_id, owner_tg_id)."""
    for grant, pet_id, owner_tg_id in grants:
        if grant is None or grant.level_up is None:
            continue
        async with c.sm() as s:
            pet = await Pets.get(s, pet_id)
        if pet is not None:
            await c.messenger.announce_level_up(pet, grant.level_up, owner_tg_id)
