"""Members confirm an online class with the code word: /code слово or the menu button."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.bot.callbacks import Menu
from enchanted_companions.bot.handlers.common import announce_grants
from enchanted_companions.bot.states import CodeStates
from enchanted_companions.container import Container
from enchanted_companions.services.attendance import RedeemStatus

router = Router(name="attendance")
router.message.filter(F.chat.type == "private")


async def _redeem(message: Message, c: Container, code: str) -> None:
    t = c.content.texts
    tg_id = message.from_user.id
    result = await c.attendance.redeem(tg_id, code)
    match result.status:
        case RedeemStatus.OK:
            assert result.session and result.pet and result.grant
            key = "classes.ok_live" if result.kind == "live" else "classes.ok_recording"
            await message.answer(
                t.render(key, name=result.pet.name, title=result.session.title, energy=result.grant.amount),
                reply_markup=kb.main_menu(t),
            )
            await announce_grants(c, (result.grant, result.pet.id, tg_id))
        case RedeemStatus.ALREADY:
            await message.answer(t.render("classes.already", title=result.session.title if result.session else ""))
        case RedeemStatus.INVALID:
            await message.answer(t.render("classes.invalid"))
        case RedeemStatus.LOCKED:
            await message.answer(t.render("classes.locked", minutes=result.retry_in_minutes))
        case RedeemStatus.NOT_REGISTERED:
            await message.answer(t.render("common.not_registered"))
        case RedeemStatus.EMPTY:
            await message.answer(t.render("classes.ask_code"))


@router.message(Command("code"))
async def code_cmd(message: Message, command: CommandObject, state: FSMContext, container: Container) -> None:
    if command.args:
        await state.clear()
        await _redeem(message, container, command.args)
        return
    await state.set_state(CodeStates.waiting)
    await message.answer(container.content.texts.render("classes.ask_code"))


@router.callback_query(Menu.filter(F.action == "code"))
async def code_menu(query: CallbackQuery, state: FSMContext, container: Container) -> None:
    await query.answer()
    await state.set_state(CodeStates.waiting)
    await container.notifier.text(query.from_user.id, container.content.texts.render("classes.ask_code"))


@router.message(CodeStates.waiting, F.text, ~F.text.startswith("/"))
async def code_text(message: Message, state: FSMContext, container: Container) -> None:
    await state.clear()
    await _redeem(message, container, message.text or "")
