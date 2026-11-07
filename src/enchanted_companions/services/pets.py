"""Pet state: activity, natural sleep, profile view."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from enchanted_companions.db.models import Pet
from enchanted_companions.db.repositories import ActivityLog, Pets
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.domain.progression import Progress, progress
from enchanted_companions.domain.sleep import SleepState, sleep_state_for
from enchanted_companions.settings import GameConfig


@dataclass(frozen=True, slots=True)
class ProfileView:
    pet: Pet
    progress: Progress
    state: SleepState


class PetService:
    def __init__(self, sm: SessionFactory, clock: GameClock, cfg: GameConfig) -> None:
        self.sm, self.clock, self.cfg = sm, clock, cfg

    def days_inactive(self, pet: Pet, now: datetime | None = None) -> int:
        return (self.clock.game_date(now) - self.clock.game_date(pet.last_active_at)).days

    def current_state(self, pet: Pet, now: datetime | None = None) -> SleepState:
        sleep = self.cfg.sleep
        return sleep_state_for(
            self.days_inactive(pet, now),
            sleep.dozing_after_days,
            sleep.sleeping_after_days,
            sleep.dreaming_after_days,
        )

    def progress_of(self, pet: Pet) -> Progress:
        levels = self.cfg.levels
        return progress(pet.energy_total, pet.level, levels.thresholds, levels.stage_starts)

    def touch(self, s: AsyncSession, pet: Pet, now: datetime) -> SleepState | None:
        """Mark the pet active. Returns the previous state when the pet just woke up."""
        previous = self.current_state(pet, now)
        pet.last_active_at = now
        pet.sleep_state = SleepState.AWAKE.value
        pet.reminder_sent_at = None
        if previous is SleepState.AWAKE:
            return None
        ActivityLog.add(s, pet.id, "woke_up", now, previous=previous.value)
        return previous

    async def touch_by_tg(self, tg_id: int) -> tuple[Pet | None, SleepState | None]:
        now = self.clock.now()
        async with self.sm() as s, s.begin():
            pet = await Pets.by_owner_tg(s, tg_id, lock=True)
            if pet is None:
                return None, None
            return pet, self.touch(s, pet, now)

    async def profile(self, tg_id: int) -> ProfileView | None:
        async with self.sm() as s:
            pet = await Pets.by_owner_tg(s, tg_id)
            if pet is None:
                return None
            return ProfileView(pet=pet, progress=self.progress_of(pet), state=self.current_state(pet))
