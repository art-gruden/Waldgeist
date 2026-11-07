"""Last-resort error handler: log it (without secrets) and apologise softly."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ErrorEvent

from enchanted_companions.container import Container

log = logging.getLogger(__name__)
router = Router(name="errors")


@router.errors()
async def on_error(event: ErrorEvent, **data: object) -> bool:
    log.exception("unhandled error in handler", exc_info=event.exception)
    update = event.update
    user = None
    if update.message and update.message.chat.type == "private":
        user = update.message.from_user
    elif update.callback_query:
        user = update.callback_query.from_user
        try:
            await update.callback_query.answer()
        except Exception:  # noqa: BLE001 - the query may already be answered or expired
            pass
    container = data.get("container")
    if user is not None and isinstance(container, Container):
        await container.notifier.text(user.id, container.content.texts.render("common.error"))
    return True
