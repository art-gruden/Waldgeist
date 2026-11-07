"""/world, /stats, /help."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.bot.callbacks import Menu
from enchanted_companions.container import Container

router = Router(name="community")


async def _world(c: Container, chat_id: int) -> None:
    t = c.content.texts
    w = await c.community.world()
    pets = "\n".join(
        t.render(
            "world.pet_line",
            html_values={"label": c.messenger.label(p)},
            species=c.messenger.species_name(p),
            level=p.level,
        )
        for p in w.pets
    ) or t.render("world.no_pets")
    if w.last_class:
        last_class = t.render(
            "world.last_class", title=w.last_class.title, live=w.last_class_live, recording=w.last_class_recording
        )
    else:
        last_class = t.render("world.no_class")
    await c.notifier.text(
        chat_id,
        t.render(
            "world.card",
            html_values={"pets": pets, "last_class": last_class},
            registered=w.registered,
            active=w.active,
            greetings=w.greetings_today,
            active_days=c.cfg.community.active_days,
        ),
    )


@router.message(Command("world"))
async def world_cmd(message: Message, container: Container) -> None:
    await _world(container, message.chat.id)


@router.callback_query(Menu.filter(F.action == "world"))
async def world_menu(query: CallbackQuery, container: Container) -> None:
    await query.answer()
    await _world(container, query.from_user.id)


async def _stats(c: Container, chat_id: int, tg_id: int) -> None:
    t = c.content.texts
    st = await c.community.stats(tg_id)
    if st is None:
        await c.notifier.text(chat_id, t.render("common.not_registered"))
        return
    await c.notifier.text(
        chat_id,
        t.render(
            "stats.card",
            html_values={"label": c.messenger.label(st.pet)},
            level=st.progress.level,
            energy=st.progress.energy,
            energy_month=st.energy_month,
            mornings_total=st.mornings_total,
            mornings_month=st.mornings_month,
            classes=st.classes_total,
            visits_made=st.visits_made,
            visits_received=st.visits_received,
        ),
        kb.main_menu(t),
    )


@router.message(Command("stats"), F.chat.type == "private")
async def stats_cmd(message: Message, container: Container) -> None:
    await _stats(container, message.chat.id, message.from_user.id)


@router.callback_query(Menu.filter(F.action == "stats"))
async def stats_menu(query: CallbackQuery, container: Container) -> None:
    await query.answer()
    await _stats(container, query.from_user.id, query.from_user.id)


@router.message(Command("help"))
async def help_cmd(message: Message, container: Container) -> None:
    t = container.content.texts
    markup = kb.main_menu(t) if message.chat.type == "private" else None
    await message.answer(_help_text(container), reply_markup=markup)


@router.callback_query(Menu.filter(F.action == "help"))
async def help_menu(query: CallbackQuery, container: Container) -> None:
    await query.answer()
    await container.notifier.text(query.from_user.id, _help_text(container), kb.main_menu(container.content.texts))


def _help_text(c: Container) -> str:
    m, cl, v = c.cfg.morning, c.cfg.classes, c.cfg.visits
    return c.content.texts.render(
        "help.text",
        window_start=m.window_start.strftime("%H:%M"),
        window_end=m.window_end.strftime("%H:%M"),
        morning=m.energy,
        live=cl.live_energy,
        recording=cl.recording_energy,
        days=cl.recording_valid_days,
        host=v.host_energy,
        guest=v.guest_energy,
    )


@router.message(Command("activities", "quests"), F.chat.type == "private")
async def coming_soon(message: Message, container: Container) -> None:
    await message.answer(
        container.content.texts.render("common.coming_soon"), reply_markup=kb.main_menu(container.content.texts)
    )
