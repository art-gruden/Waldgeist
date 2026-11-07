"""Morning greetings in the group: the postcard button or a greeting message."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.bot.callbacks import MorningHello
from enchanted_companions.bot.handlers.common import announce_grants
from enchanted_companions.container import Container
from enchanted_companions.services.morning import MorningResult, MorningStatus

log = logging.getLogger(__name__)
router = Router(name="morning")


async def _after_counted(c: Container, tg_id: int, result: MorningResult) -> None:
    if result.pet is None:
        return
    if result.woke_from is not None:
        await c.messenger.send_wake(tg_id, result.pet, result.woke_from)
    await announce_grants(c, (result.grant, result.pet.id, tg_id))


@router.callback_query(MorningHello.filter())
async def hello_button(query: CallbackQuery, container: Container) -> None:
    c, t = container, container.content.texts
    result = await c.morning.greet(query.from_user.id)
    if result.status is MorningStatus.COUNTED and result.pet:
        await query.answer(t.render("morning.toast_counted", name=result.pet.name, energy=c.cfg.morning.energy))
        await _after_counted(c, query.from_user.id, result)
    elif result.status is MorningStatus.ALREADY and result.pet:
        await query.answer(t.render("morning.toast_already", name=result.pet.name))
    elif result.status is MorningStatus.OUTSIDE_WINDOW:
        await query.answer(t.render("morning.toast_outside"))
    else:
        await query.answer(t.render("morning.toast_not_registered", bot=c.bot_username), show_alert=True)


@router.message(F.chat.type.in_({"group", "supergroup"}), ~F.from_user.is_bot)
async def group_message(message: Message, container: Container) -> None:
    c, t = container, container.content.texts
    if message.chat.id != c.settings.group_chat_id or message.from_user is None:
        return
    if not c.morning.in_window():
        return
    if not c.morning.message_counts(message.text or message.caption):
        return
    result = await c.morning.greet(message.from_user.id)
    if result.status is MorningStatus.COUNTED:
        await c.notifier.react(message.chat.id, message.message_id, c.cfg.morning.counted_reaction)
        await _after_counted(c, message.from_user.id, result)
    elif result.status is MorningStatus.NOT_REGISTERED and await c.morning.claim_invite(message.from_user.id):
        sent = await c.notifier.text(
            message.from_user.id,
            t.render("morning.invite", first_name=message.from_user.first_name),
            kb.open_bot(t, c.bot_username),
        )
        if sent is None:  # the member never opened the bot: a quiet reaction instead
            await c.notifier.react(message.chat.id, message.message_id, c.cfg.morning.invite_reaction)
