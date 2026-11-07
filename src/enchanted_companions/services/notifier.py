"""Sending to Telegram with retries, file_id caching and graceful handling of API errors."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message, ReactionTypeEmoji

from enchanted_companions.db.repositories import Media
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.clock import GameClock

log = logging.getLogger(__name__)
T = TypeVar("T")

CAPTION_LIMIT = 1024
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


class MediaLibrary:
    """Finds images under assets/images: <slug>/stage-N.*, <slug>/sleep.*, locations/<scene>.*"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _find(self, rel_stem: str) -> Path | None:
        for ext in IMAGE_EXTENSIONS:
            path = self.root / f"{rel_stem}{ext}"
            if path.is_file():
                return path
        return None

    def pet_image(self, slug: str, stage: int, sleeping: bool = False) -> Path | None:
        """Sleep image if asleep; else the stage image, falling back to earlier stages only."""
        if sleeping and (found := self._find(f"{slug}/sleep")):
            return found
        for st in range(stage, 0, -1):
            if found := self._find(f"{slug}/stage-{st}"):
                return found
        return None

    def scene(self, slug: str) -> Path | None:
        return self._find(f"locations/{slug}")

    def key(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.name

    def missing_report(self, slugs: list[str], stages: int) -> list[str]:
        missing = [f"{slug}/stage-1" for slug in slugs if self._find(f"{slug}/stage-1") is None]
        missing += [f"{slug}/sleep" for slug in slugs if self._find(f"{slug}/sleep") is None]
        return missing


class Notifier:
    def __init__(self, bot: Bot, sm: SessionFactory, clock: GameClock, media: MediaLibrary, group_chat_id: int) -> None:
        self.bot, self.sm, self.clock, self.media = bot, sm, clock, media
        self.group_chat_id = group_chat_id
        self._fingerprints: dict[tuple[str, float], str] = {}

    async def _call(self, what: str, factory: Callable[[], Awaitable[T]], attempts: int = 4) -> T | None:
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                return await factory()
            except TelegramRetryAfter as exc:
                log.warning("telegram flood control", extra={"what": what, "retry_after": exc.retry_after})
                await asyncio.sleep(exc.retry_after + 0.5)
            except TelegramForbiddenError:
                log.info("chat unavailable (bot blocked or not a member)", extra={"what": what})
                return None
            except TelegramNetworkError as exc:
                if attempt == attempts:
                    log.error("telegram network error", extra={"what": what, "error": str(exc)})
                    return None
                await asyncio.sleep(delay)
                delay *= 2
            except TelegramBadRequest as exc:
                log.warning("telegram rejected request", extra={"what": what, "error": str(exc)})
                return None
        return None

    async def text(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> Message | None:
        return await self._call(
            "send_message",
            lambda: self.bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True),
        )

    def _fingerprint(self, path: Path) -> str:
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime)
        if cache_key not in self._fingerprints:
            self._fingerprints[cache_key] = hashlib.sha256(path.read_bytes()).hexdigest()
        return self._fingerprints[cache_key]

    async def photo(
        self, chat_id: int, path: Path | None, caption: str, markup: InlineKeyboardMarkup | None = None
    ) -> Message | None:
        if path is None:
            return await self.text(chat_id, caption, markup)
        long_caption = len(caption) > CAPTION_LIMIT
        photo_caption = None if long_caption else caption
        photo_markup = None if long_caption else markup

        key = self.media.key(path)
        fingerprint = self._fingerprint(path)
        async with self.sm() as s:
            cached = await Media.get(s, key)
        file_id = cached.tg_file_id if cached and cached.fingerprint == fingerprint else None

        message: Message | None = None
        if file_id:
            try:
                message = await self.bot.send_photo(chat_id, file_id, caption=photo_caption, reply_markup=photo_markup)
            except TelegramBadRequest:
                file_id = None  # stale file_id: upload again below
            except (TelegramForbiddenError, TelegramRetryAfter, TelegramNetworkError):
                message = await self._call(
                    "send_photo",
                    lambda: self.bot.send_photo(chat_id, file_id, caption=photo_caption, reply_markup=photo_markup),
                )
        if message is None and not file_id:
            message = await self._call(
                "send_photo",
                lambda: self.bot.send_photo(
                    chat_id, FSInputFile(path), caption=photo_caption, reply_markup=photo_markup
                ),
            )
            if message is not None and message.photo:
                async with self.sm() as s, s.begin():
                    await Media.put(s, key, fingerprint, message.photo[-1].file_id, self.clock.now())
        if message is None:
            return await self.text(chat_id, caption, markup)
        if long_caption:
            return await self.text(chat_id, caption, markup)
        return message

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        await self._call(
            "set_message_reaction",
            lambda: self.bot.set_message_reaction(chat_id, message_id, [ReactionTypeEmoji(emoji=emoji)]),
        )

    def has_group(self) -> bool:
        if not self.group_chat_id:
            log.warning("GROUP_CHAT_ID is not set: group message skipped")
            return False
        return True

    async def group_text(self, text: str, markup: InlineKeyboardMarkup | None = None) -> Message | None:
        return await self.text(self.group_chat_id, text, markup) if self.has_group() else None

    async def group_photo(
        self, path: Path | None, caption: str, markup: InlineKeyboardMarkup | None = None
    ) -> Message | None:
        return await self.photo(self.group_chat_id, path, caption, markup) if self.has_group() else None
