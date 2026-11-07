"""Administrator commands. Restricted to ADMIN_TG_IDS (numeric Telegram IDs)."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.bot.callbacks import NewClassConfirm
from enchanted_companions.bot.handlers.common import announce_grants, safe_edit_markup
from enchanted_companions.bot.states import NewClassStates
from enchanted_companions.container import Container
from enchanted_companions.services.admin import AdminError
from enchanted_companions.services.attendance import RedeemStatus, normalize_code

log = logging.getLogger(__name__)


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, container: Container) -> bool:
        user = getattr(event, "from_user", None)
        return container.admin.is_admin(user.id if user else None)


router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

private = F.chat.type == "private"


@router.message(Command("chatid"))
async def chat_id(message: Message, container: Container) -> None:
    await message.reply(container.content.texts.render("admin.chat_id", chat_id=message.chat.id))


@router.message(Command("admin"), private)
async def admin_help(message: Message, container: Container) -> None:
    await message.answer(container.content.texts.render("admin.help"))


# ------------------------------------------------------------------ classes


def _parse_day(text: str, today: date) -> date | None:
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s*", text or "")
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else today.year
    if year < 100:
        year += 2000
    try:
        value = date(year, month, day)
    except ValueError:
        return None
    if not m.group(3) and value < today:
        value = value.replace(year=year + 1)
    return value


def _parse_time(text: str) -> time | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return time(int(m.group(1)), int(m.group(2)))


@router.message(Command("new_class"), private)
async def new_class(message: Message, state: FSMContext, container: Container) -> None:
    await state.set_state(NewClassStates.day)
    await message.answer(container.content.texts.render("admin.class_ask_day"))


@router.message(NewClassStates.day, F.text, ~F.text.startswith("/"))
async def new_class_day(message: Message, state: FSMContext, container: Container) -> None:
    day = _parse_day(message.text or "", container.clock.game_date())
    if day is None:
        await message.answer(container.content.texts.render("admin.class_bad_day"))
        return
    await state.update_data(day=day.isoformat())
    await state.set_state(NewClassStates.time)
    await message.answer(container.content.texts.render("admin.class_ask_time"))


@router.message(NewClassStates.time, F.text, ~F.text.startswith("/"))
async def new_class_time(message: Message, state: FSMContext, container: Container) -> None:
    at = _parse_time(message.text or "")
    if at is None:
        await message.answer(container.content.texts.render("admin.class_bad_time"))
        return
    await state.update_data(time=at.strftime("%H:%M"))
    await state.set_state(NewClassStates.title)
    await message.answer(container.content.texts.render("admin.class_ask_title"))


@router.message(NewClassStates.title, F.text, ~F.text.startswith("/"))
async def new_class_title(message: Message, state: FSMContext, container: Container) -> None:
    await state.update_data(title=(message.text or "").strip()[:200])
    await state.set_state(NewClassStates.code)
    await message.answer(container.content.texts.render("admin.class_ask_code"))


@router.message(NewClassStates.code, F.text, ~F.text.startswith("/"))
async def new_class_code(message: Message, state: FSMContext, container: Container) -> None:
    t = container.content.texts
    code = normalize_code(message.text or "")
    if not code:
        await message.answer(t.render("admin.class_ask_code"))
        return
    await state.update_data(code=code)
    data = await state.get_data()
    await state.set_state(NewClassStates.confirm)
    day = date.fromisoformat(data["day"])
    await message.answer(
        t.render(
            "admin.class_confirm", day=day.strftime("%d.%m.%Y"), time=data["time"], title=data["title"], code=code
        ),
        reply_markup=kb.confirm_class(t),
    )


@router.callback_query(NewClassConfirm.filter(), NewClassStates.confirm)
async def new_class_confirm(
    query: CallbackQuery, callback_data: NewClassConfirm, state: FSMContext, container: Container
) -> None:
    t = container.content.texts
    data = await state.get_data()
    await state.clear()
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await safe_edit_markup(query.message)
    if not callback_data.ok:
        await query.message.answer(t.render("common.cancelled"))
        return
    session = await container.attendance.create_session(
        query.from_user.id,
        date.fromisoformat(data["day"]),
        datetime.strptime(data["time"], "%H:%M").time(),
        data["title"],
        data["code"],
    )
    log.info("class created", extra={"admin": query.from_user.id, "session": session.id})
    await query.message.answer(t.render("admin.class_created", id=session.id, title=session.title))


@router.message(Command("rec"), private)
async def recording_code(message: Message, command: CommandObject, container: Container) -> None:
    t = container.content.texts
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer(t.render("admin.rec_usage"))
        return
    session = await container.attendance.set_recording_code(int(parts[0]), parts[1])
    if session is None:
        await message.answer(t.render("admin.class_not_found"))
        return
    await message.answer(
        t.render(
            "admin.rec_set", id=session.id, code=session.recording_code, days=container.cfg.classes.recording_valid_days
        )
    )
    await container.notifier.group_text(
        t.render("classes.recording_available", title=session.title, days=container.cfg.classes.recording_valid_days)
    )


@router.message(Command("cancel_class"), private)
async def cancel_class(message: Message, command: CommandObject, container: Container) -> None:
    t = container.content.texts
    arg = (command.args or "").strip()
    session = await container.attendance.cancel(int(arg)) if arg.isdigit() else None
    await message.answer(t.render("admin.class_cancelled", id=arg) if session else t.render("admin.class_not_found"))


@router.message(Command("classes"), private)
async def classes(message: Message, container: Container) -> None:
    t, clock = container.content.texts, container.clock
    rows = await container.attendance.recent()
    if not rows:
        await message.answer(t.render("admin.classes_empty"))
        return
    lines = [
        t.render(
            "admin.classes_line",
            id=cs.id,
            when=clock.local(cs.starts_at).strftime("%d.%m %H:%M"),
            title=cs.title,
            code=cs.live_code,
            rec=cs.recording_code or "—",
            live=counts.get("live", 0),
            recording=counts.get("recording", 0),
            status=cs.status,
        )
        for cs, counts in rows
    ]
    await message.answer("\n".join(lines))


@router.message(Command("mark"), private)
async def mark(message: Message, command: CommandObject, container: Container) -> None:
    t = container.content.texts
    parts = (command.args or "").split()
    if len(parts) != 3 or not parts[0].isdigit() or parts[2] not in ("live", "rec"):
        await message.answer(t.render("admin.mark_usage"))
        return
    pet = await container.admin.find_pet(parts[1])
    if pet is None:
        await message.answer(t.render("admin.who_not_found", who=parts[1]))
        return
    kind = "live" if parts[2] == "live" else "recording"
    result = await container.attendance.admin_mark(message.from_user.id, int(parts[0]), pet.id, kind)
    if result.status is RedeemStatus.OK and result.grant:
        await message.answer(t.render("admin.mark_ok", name=pet.name, energy=result.grant.amount))
        await container.notifier.text(
            pet.owner.tg_id,
            t.render("classes.marked_by_admin", name=pet.name, title=result.session.title, energy=result.grant.amount),
        )
        await announce_grants(container, (result.grant, pet.id, pet.owner.tg_id))
    elif result.status is RedeemStatus.ALREADY:
        await message.answer(t.render("admin.mark_already", name=pet.name))
    else:
        await message.answer(t.render("admin.class_not_found"))


# ------------------------------------------------------------------- energy


@router.message(Command("grant"), private)
async def grant(message: Message, command: CommandObject, container: Container) -> None:
    t = container.content.texts
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 3 or not re.fullmatch(r"[+-]?\d+", parts[1]):
        await message.answer(t.render("admin.grant_usage"))
        return
    try:
        result = await container.admin.adjust(message.from_user.id, parts[0], int(parts[1]), parts[2])
    except AdminError as exc:
        await message.answer(t.render(f"admin.error_{exc.args[0]}", who=parts[0]))
        return
    await message.answer(
        t.render("admin.grant_ok", name=result.pet.name, amount=int(parts[1]), total=result.grant.energy_total)
    )
    owner = result.pet.owner.tg_id if result.pet.owner else None
    if owner and int(parts[1]) > 0:
        await container.notifier.text(
            owner, t.render("admin.grant_notice", name=result.pet.name, amount=int(parts[1]), reason=parts[2])
        )
        await announce_grants(container, (result.grant, result.pet.id, owner))


@router.message(Command("species"), private)
async def change_species(message: Message, command: CommandObject, container: Container) -> None:
    t = container.content.texts
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            t.render("admin.species_usage", slugs=", ".join(s.slug for s in container.content.species))
        )
        return
    try:
        pet = await container.admin.change_species(message.from_user.id, parts[0], parts[1])
    except AdminError as exc:
        await message.answer(t.render(f"admin.error_{exc.args[0]}", who=parts[0]))
        return
    await message.answer(t.render("admin.species_ok", name=pet.name, species=container.messenger.species_name(pet)))


@router.message(Command("announce"), private)
async def announce(message: Message, command: CommandObject, container: Container) -> None:
    t = container.content.texts
    if not command.args:
        await message.answer(t.render("admin.announce_usage"))
        return
    sent = await container.notifier.group_text(t.render("admin.announcement", text=command.args))
    await message.answer(t.render("admin.announce_ok" if sent else "admin.announce_failed"))


@router.message(Command("missing_images"), private)
async def missing_images(message: Message, container: Container) -> None:
    t = container.content.texts
    missing = container.media.missing_report([s.slug for s in container.content.species], 4)
    await message.answer(
        t.render("admin.missing_images", items=", ".join(missing)) if missing else t.render("admin.no_missing_images")
    )
