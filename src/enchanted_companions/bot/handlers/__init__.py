"""Router tree. Order matters: admin first, the catch-all group handler last."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.bot.handlers import (
    admin,
    attendance,
    community,
    errors,
    morning,
    pet,
    start,
    visits,
)
from enchanted_companions.container import Container

fallback = Router(name="fallback")


@fallback.message(F.chat.type == "private")
async def unknown_private(message: Message, container: Container) -> None:
    t = container.content.texts
    await message.answer(t.render("common.unknown"), reply_markup=kb.main_menu(t))


@fallback.callback_query()
async def unknown_callback(query: CallbackQuery) -> None:
    await query.answer()


def build_router() -> Router:
    """A new root router. Module routers are detached first, so a dispatcher can be
    built more than once in one process (tests do that)."""
    routers = [
        errors.router,
        admin.router,
        start.router,
        pet.router,
        attendance.router,
        visits.router,
        community.router,
        morning.router,
        fallback,
    ]
    for router in routers:
        router._parent_router = None  # noqa: SLF001
    root = Router(name="root")
    root.include_routers(*routers)
    return root
