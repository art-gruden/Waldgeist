"""Entry point: python -m enchanted_companions"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from enchanted_companions.bot.handlers import build_router
from enchanted_companions.bot.middlewares import ActivityMiddleware, ThrottlingMiddleware
from enchanted_companions.container import Container, build_container
from enchanted_companions.content import Content
from enchanted_companions.db.seed import seed_species
from enchanted_companions.db.session import create_engine, create_session_factory
from enchanted_companions.domain.affirmations import AffirmationCatalog
from enchanted_companions.logging_setup import setup_logging
from enchanted_companions.scheduler import jobs
from enchanted_companions.scheduler.setup import build_scheduler
from enchanted_companions.settings import GameConfig, Settings

log = logging.getLogger("enchanted_companions")


async def set_commands(c: Container) -> None:
    t = c.content.texts
    private = [
        BotCommand(command=name, description=t.render(f"commands.{name}"))
        for name in ("pet", "code", "visit", "world", "stats", "settings", "help", "start")
    ]
    await c.bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
    if c.settings.group_chat_id:
        group = [BotCommand(command=n, description=t.render(f"commands.{n}")) for n in ("world", "help")]
        try:
            await c.bot.set_my_commands(group, scope=BotCommandScopeChat(chat_id=c.settings.group_chat_id))
        except Exception:  # the bot may not be in the group yet
            log.warning("could not set group commands")


def build_dispatcher(c: Container, throttle_interval: float = 0.6) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), container=c)
    throttling = ThrottlingMiddleware(throttle_interval)
    activity = ActivityMiddleware(c)
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(throttling)
        observer.middleware(activity)
    dp.include_router(build_router())
    return dp


async def run() -> None:
    settings = Settings()
    setup_logging(settings.log_level, settings.log_dir)
    cfg = GameConfig.load(settings.game_config_path)
    content = Content.load(settings.content_dir)
    affirmations = AffirmationCatalog.load(cfg.visits.affirmations_file)

    engine = create_engine(settings.database_url)
    sm = create_session_factory(engine)
    bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    c = build_container(settings, cfg, content, affirmations, sm, bot)

    scheduler = None
    try:
        me = await bot.get_me()
        c.bot_username = me.username or ""
        log.info(
            "bot starting",
            extra={
                "bot": c.bot_username,
                "group": settings.group_chat_id,
                "admins": len(settings.admin_tg_ids),
                "tz": cfg.timezone,
            },
        )
        await seed_species(sm, content)
        missing = c.media.missing_report([s.slug for s in content.species], 4)
        if missing:
            log.warning("images missing (bot shows text instead)", extra={"missing": ", ".join(missing)})
        await set_commands(c)

        dp = build_dispatcher(c)
        scheduler = build_scheduler(c)
        scheduler.start()
        await jobs.catch_up(c)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)
    finally:
        log.info("shutting down")
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=True)
        await bot.session.close()
        await engine.dispose()
        log.info("stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("fatal error, exiting (Docker will restart the bot)")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
