"""Community dashboard (/world) and personal statistics (/stats)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from enchanted_companions.db.models import ClassSession, Pet
from enchanted_companions.db.repositories import Classes, Ledger, Pets, Social
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.domain.progression import Progress
from enchanted_companions.domain.rewards import Source
from enchanted_companions.services.pets import PetService
from enchanted_companions.settings import GameConfig


@dataclass(frozen=True, slots=True)
class WorldView:
    registered: int
    active: int
    greetings_today: int
    last_class: ClassSession | None
    last_class_live: int
    last_class_recording: int
    pets: list[Pet]


@dataclass(frozen=True, slots=True)
class StatsView:
    pet: Pet
    progress: Progress
    mornings_total: int
    mornings_month: int
    classes_total: int
    visits_made: int
    visits_received: int
    energy_month: int


class CommunityService:
    def __init__(self, sm: SessionFactory, clock: GameClock, cfg: GameConfig, pets: PetService) -> None:
        self.sm, self.clock, self.cfg, self.pets = sm, clock, cfg, pets

    async def world(self) -> WorldView:
        now = self.clock.now()
        async with self.sm() as s:
            all_pets = await Pets.all_with_owners(s)
            active = await Pets.count_active_since(s, now - timedelta(days=self.cfg.community.active_days))
            greetings = await Ledger.count_by_source(s, Source.MORNING.value, day=self.clock.game_date(now))
            last = await Classes.last_started(s, now)
            counts = await Classes.attendance_counts(s, last.id) if last else {}
        return WorldView(
            registered=len(all_pets),
            active=active,
            greetings_today=greetings,
            last_class=last,
            last_class_live=counts.get("live", 0),
            last_class_recording=counts.get("recording", 0),
            pets=all_pets,
        )

    async def stats(self, tg_id: int) -> StatsView | None:
        now = self.clock.now()
        month_start = self.clock.month_start(now)
        async with self.sm() as s:
            pet = await Pets.by_owner_tg(s, tg_id)
            if pet is None:
                return None
            made, received = await Social.visit_counts(s, pet.id)
            return StatsView(
                pet=pet,
                progress=self.pets.progress_of(pet),
                mornings_total=await Ledger.count_by_source(s, Source.MORNING.value, pet_id=pet.id),
                mornings_month=await Ledger.count_by_source_since(s, Source.MORNING.value, pet.id, month_start),
                classes_total=await Classes.count_for_pet(s, pet.id),
                visits_made=made,
                visits_received=received,
                energy_month=await Ledger.sum_since(s, pet.id, month_start),
            )
