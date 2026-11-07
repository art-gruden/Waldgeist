"""Visits: /visit or the menu button, then a pet to visit."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.bot.callbacks import Menu, VisitPet
from enchanted_companions.bot.handlers.common import announce_grants, safe_edit_markup
from enchanted_companions.container import Container
from enchanted_companions.services.visits import VisitStatus

router = Router(name="visits")


async def _list(c: Container, chat_id: int, tg_id: int) -> None:
    t = c.content.texts
    if await c.pets.profile(tg_id) is None:
        await c.notifier.text(chat_id, t.render("common.not_registered"))
        return
    if await c.visits.visited_today(tg_id):
        await c.notifier.text(chat_id, t.render("visits.already_today"))
        return
    pets = await c.visits.candidates(tg_id)
    if not pets:
        await c.notifier.text(chat_id, t.render("visits.nobody_to_visit"))
        return

    def label(pet) -> str:
        mark = " ✨" if c.visits.is_oracle(pet) else ""
        return f"{c.messenger.emoji(pet)} {pet.name}{mark}"

    await c.notifier.text(chat_id, t.render("visits.choose"), kb.visit_list(pets, label))


@router.message(Command("visit"), F.chat.type == "private")
async def visit_cmd(message: Message, container: Container) -> None:
    await _list(container, message.chat.id, message.from_user.id)


@router.callback_query(Menu.filter(F.action == "visit"))
async def visit_menu(query: CallbackQuery, container: Container) -> None:
    await query.answer()
    await _list(container, query.from_user.id, query.from_user.id)


@router.callback_query(VisitPet.filter())
async def do_visit(query: CallbackQuery, callback_data: VisitPet, container: Container) -> None:
    c, t = container, container.content.texts
    tg_id = query.from_user.id
    result = await c.visits.visit(tg_id, callback_data.pet_id)
    await query.answer()
    await safe_edit_markup(query.message)

    if result.status is VisitStatus.VISITED:
        guest, host = result.guest, result.host
        assert guest and host
        guest_key = "visits.gift_at_door" if result.host_sleeping else "visits.scene_guest"
        await c.notifier.text(
            tg_id,
            t.render(
                guest_key,
                html_values={"guest": c.messenger.label(guest), "host": c.messenger.label(host)},
                energy=c.cfg.visits.guest_energy,
            ),
        )
        if result.affirmation_html:
            await c.notifier.text(tg_id, t.render("visits.oracle_intro", host=host.name))
            await c.notifier.text(tg_id, result.affirmation_html)
        host_key = "visits.host_while_asleep" if result.host_sleeping else "visits.scene_host"
        await c.notifier.text(
            host.owner.tg_id,
            t.render(
                host_key,
                html_values={"guest": c.messenger.label(guest), "host": c.messenger.label(host)},
                energy=c.cfg.visits.host_energy,
            ),
        )
        await announce_grants(c, (result.guest_grant, guest.id, tg_id), (result.host_grant, host.id, host.owner.tg_id))
        return

    key = {
        VisitStatus.NOBODY_HOME: "visits.nobody_home",
        VisitStatus.ALREADY_TODAY: "visits.already_today",
        VisitStatus.SELF: "visits.self",
        VisitStatus.NOT_REGISTERED: "common.not_registered",
        VisitStatus.NO_HOST: "visits.no_host",
    }[result.status]
    host_name = result.host.name if result.host else ""
    await c.notifier.text(
        tg_id, t.render(key, host=host_name), kb.main_menu(t) if key == "visits.nobody_home" else None
    )
