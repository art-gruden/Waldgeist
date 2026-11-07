"""Wiring: builds every service once and hands them to handlers and jobs."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot

from enchanted_companions.content import Content
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.affirmations import AffirmationCatalog
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.services.admin import AdminService
from enchanted_companions.services.attendance import AttendanceService
from enchanted_companions.services.community import CommunityService
from enchanted_companions.services.messenger import Messenger
from enchanted_companions.services.morning import MorningService
from enchanted_companions.services.notifier import MediaLibrary, Notifier
from enchanted_companions.services.pets import PetService
from enchanted_companions.services.registration import RegistrationService
from enchanted_companions.services.reward_engine import RewardEngine
from enchanted_companions.services.sleep import SleepService
from enchanted_companions.services.visits import VisitService
from enchanted_companions.settings import GameConfig, Settings


@dataclass
class Container:
    settings: Settings
    cfg: GameConfig
    content: Content
    clock: GameClock
    sm: SessionFactory
    bot: Bot
    engine: RewardEngine
    media: MediaLibrary
    notifier: Notifier
    messenger: Messenger
    pets: PetService
    registration: RegistrationService
    morning: MorningService
    sleep: SleepService
    attendance: AttendanceService
    visits: VisitService
    community: CommunityService
    admin: AdminService
    bot_username: str = ""


def build_container(
    settings: Settings,
    cfg: GameConfig,
    content: Content,
    affirmations: AffirmationCatalog,
    sm: SessionFactory,
    bot: Bot,
    clock: GameClock | None = None,
) -> Container:
    clock = clock or GameClock(cfg.tz)
    engine = RewardEngine(cfg.levels)
    media = MediaLibrary(settings.assets_dir)
    notifier = Notifier(bot, sm, clock, media, settings.group_chat_id)
    pets = PetService(sm, clock, cfg)
    return Container(
        settings=settings,
        cfg=cfg,
        content=content,
        clock=clock,
        sm=sm,
        bot=bot,
        engine=engine,
        media=media,
        notifier=notifier,
        messenger=Messenger(notifier, media, content, cfg),
        pets=pets,
        registration=RegistrationService(sm, clock, cfg, content),
        morning=MorningService(sm, clock, cfg, engine, pets),
        sleep=SleepService(sm, clock, cfg, pets),
        attendance=AttendanceService(sm, clock, cfg, engine),
        visits=VisitService(sm, clock, cfg, engine, pets, affirmations),
        community=CommunityService(sm, clock, cfg, pets),
        admin=AdminService(sm, clock, engine, content, settings.admin_tg_ids),
    )
