"""Morning greetings: +energy once per local day, by button or by a greeting message."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from enchanted_companions.db.models import Pet
from enchanted_companions.db.repositories import Community, Ledger, Pets
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.domain.greeting import GreetingDetector
from enchanted_companions.domain.rewards import Source, morning_key
from enchanted_companions.domain.sleep import SleepState
from enchanted_companions.services.pets import PetService
from enchanted_companions.services.reward_engine import GrantResult, RewardEngine
from enchanted_companions.settings import GameConfig


class MorningStatus(StrEnum):
    COUNTED = "counted"
    ALREADY = "already"
    OUTSIDE_WINDOW = "outside_window"
    NOT_REGISTERED = "not_registered"


@dataclass(frozen=True, slots=True)
class MorningResult:
    status: MorningStatus
    pet: Pet | None = None
    woke_from: SleepState | None = None
    grant: GrantResult | None = None


class MorningService:
    def __init__(
        self, sm: SessionFactory, clock: GameClock, cfg: GameConfig, engine: RewardEngine, pets: PetService
    ) -> None:
        self.sm, self.clock, self.cfg, self.engine, self.pets = sm, clock, cfg, engine, pets
        self.detector = GreetingDetector(cfg.morning.greeting_patterns)

    def in_window(self, now: datetime | None = None) -> bool:
        m = self.cfg.morning
        return self.clock.in_window(m.window_start, m.window_end, now)

    def message_counts(self, text: str | None) -> bool:
        if self.cfg.morning.mode == "any_message":
            return True
        return self.detector.is_greeting(text)

    async def greet(self, tg_id: int, now: datetime | None = None) -> MorningResult:
        now = now or self.clock.now()
        if not self.in_window(now):
            return MorningResult(MorningStatus.OUTSIDE_WINDOW)
        day = self.clock.game_date(now)
        async with self.sm() as s, s.begin():
            pet = await Pets.by_owner_tg(s, tg_id, lock=True)
            if pet is None:
                return MorningResult(MorningStatus.NOT_REGISTERED)
            grant = await self.engine.grant(
                s,
                pet,
                amount=self.cfg.morning.energy,
                source=Source.MORNING,
                key=morning_key(pet.id, day),
                game_date=day,
                now=now,
            )
            if not grant.granted:
                return MorningResult(MorningStatus.ALREADY, pet=pet)
            woke_from = self.pets.touch(s, pet, now)
            return MorningResult(MorningStatus.COUNTED, pet=pet, woke_from=woke_from, grant=grant)

    async def claim_invite(self, tg_id: int, now: datetime | None = None) -> bool:
        """True only the first time an unregistered member greets: invite them once."""
        now = now or self.clock.now()
        async with self.sm() as s, s.begin():
            return await Community.add_once(s, "invite", str(tg_id), self.clock.game_date(now), now) is not None

    async def count_today(self, now: datetime | None = None) -> int:
        async with self.sm() as s:
            return await Ledger.count_by_source(s, Source.MORNING.value, day=self.clock.game_date(now))
