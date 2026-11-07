"""Nightly sleep update and the single gentle reminder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from enchanted_companions.db.repositories import Pets
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.services.pets import PetService
from enchanted_companions.settings import GameConfig


@dataclass(frozen=True, slots=True)
class Reminder:
    tg_id: int
    pet_name: str
    species_slug: str


class SleepService:
    def __init__(self, sm: SessionFactory, clock: GameClock, cfg: GameConfig, pets: PetService) -> None:
        self.sm, self.clock, self.cfg, self.pets = sm, clock, cfg, pets

    async def nightly(self, now: datetime | None = None) -> list[Reminder]:
        """Recompute sleep states; return reminders to send (each pet at most once per absence)."""
        now = now or self.clock.now()
        reminders: list[Reminder] = []
        async with self.sm() as s, s.begin():
            for pet in await Pets.all_with_owners(s):
                pet.sleep_state = self.pets.current_state(pet, now).value
                days = self.pets.days_inactive(pet, now)
                owner = pet.owner
                if days >= self.cfg.sleep.reminder_after_days and owner.reminders_on and pet.reminder_sent_at is None:
                    pet.reminder_sent_at = now
                    reminders.append(Reminder(owner.tg_id, pet.name, pet.species_id))
        return reminders

    async def set_reminders(self, tg_id: int, on: bool) -> bool:
        async with self.sm() as s, s.begin():
            pet = await Pets.by_owner_tg(s, tg_id)
            if pet is None:
                return False
            pet.owner.reminders_on = on
            return True
