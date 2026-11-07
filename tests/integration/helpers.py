from __future__ import annotations

import random
from dataclasses import dataclass

from enchanted_companions.services.admin import AdminService
from enchanted_companions.services.attendance import AttendanceService
from enchanted_companions.services.community import CommunityService
from enchanted_companions.services.morning import MorningService
from enchanted_companions.services.pets import PetService
from enchanted_companions.services.registration import RegistrationService
from enchanted_companions.services.reward_engine import RewardEngine
from enchanted_companions.services.sleep import SleepService
from enchanted_companions.services.visits import VisitService

ORACLE_TG = 2116564097  # @omniyaskala in config/game.yaml
ADMIN_TG = 835852842


@dataclass
class Services:
    engine: RewardEngine
    pets: PetService
    registration: RegistrationService
    morning: MorningService
    sleep: SleepService
    attendance: AttendanceService
    visits: VisitService
    community: CommunityService
    admin: AdminService


def make_services(sm, clock, cfg, content, affirmations) -> Services:
    engine = RewardEngine(cfg.levels)
    pets = PetService(sm, clock, cfg)
    return Services(
        engine=engine,
        pets=pets,
        registration=RegistrationService(sm, clock, cfg, content),
        morning=MorningService(sm, clock, cfg, engine, pets),
        sleep=SleepService(sm, clock, cfg, pets),
        attendance=AttendanceService(sm, clock, cfg, engine),
        visits=VisitService(sm, clock, cfg, engine, pets, affirmations, rng=random.Random(5)),
        community=CommunityService(sm, clock, cfg, pets),
        admin=AdminService(sm, clock, engine, content, [ADMIN_TG]),
    )


async def register(svc: Services, tg_id: int, slug: str, name: str, username: str | None = None):
    return await svc.registration.register(tg_id, username or f"user{tg_id}", f"User{tg_id}", slug, name, None)
