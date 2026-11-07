"""Middlewares: wake the pet on any private interaction; drop button/message spam."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from enchanted_companions.container import Container

log = logging.getLogger(__name__)


def _private_user_id(event: TelegramObject) -> int | None:
    if isinstance(event, Message):
        if event.chat.type == "private" and event.from_user and not event.from_user.is_bot:
            return event.from_user.id
    elif isinstance(event, CallbackQuery):
        msg = event.message
        if event.from_user and (msg is None or getattr(msg, "chat", None) is None or msg.chat.type == "private"):
            return event.from_user.id
    return None


class ActivityMiddleware(BaseMiddleware):
    """Any private interaction counts as activity: a sleeping pet wakes up and says hello."""

    def __init__(self, container: Container) -> None:
        self.c = container

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_id = _private_user_id(event)
        if tg_id is not None:
            try:
                pet, woke_from = await self.c.pets.touch_by_tg(tg_id)
                if pet is not None and woke_from is not None:
                    await self.c.messenger.send_wake(tg_id, pet, woke_from)
            except Exception:  # never block the user's action because of this
                log.exception("activity update failed")
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Ignore repeated clicks/messages from the same user within `interval` seconds."""

    def __init__(self, interval: float = 0.6) -> None:
        self.interval = interval
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.chat.type != "private":
            return await handler(event, data)  # group messages are never throttled
        user = getattr(event, "from_user", None)
        if user is not None:
            now = time.monotonic()
            last = self._last.get(user.id, 0.0)
            if now - last < self.interval:
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return None
            self._last[user.id] = now
            if len(self._last) > 10_000:
                self._last.clear()
        return await handler(event, data)
