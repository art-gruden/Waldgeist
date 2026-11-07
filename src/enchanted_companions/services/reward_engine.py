"""The single place where energy is granted.

Guarantees:
* one idempotency key = at most one ledger row (UNIQUE constraint, checked inside a savepoint);
* the pet row is locked (SELECT ... FOR UPDATE) by the caller, so the cached total and level
  are updated consistently with the ledger in the same transaction;
* the level never goes down, even after an admin correction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from enchanted_companions.db.models import EnergyEvent, Pet
from enchanted_companions.db.repositories import ActivityLog
from enchanted_companions.domain.progression import level_for, stage_for
from enchanted_companions.domain.rewards import Source
from enchanted_companions.settings import LevelsConfig


@dataclass(frozen=True, slots=True)
class LevelUp:
    pet_id: int
    old_level: int
    new_level: int
    old_stage: int
    new_stage: int

    @property
    def evolved(self) -> bool:
        return self.new_stage > self.old_stage


@dataclass(frozen=True, slots=True)
class GrantResult:
    granted: bool
    amount: int
    energy_total: int
    level: int
    level_up: LevelUp | None = None
    event_id: int | None = None


class RewardEngine:
    def __init__(self, levels: LevelsConfig) -> None:
        self.levels = levels

    async def grant(
        self,
        s: AsyncSession,
        pet: Pet,
        *,
        amount: int,
        source: Source,
        key: str,
        game_date: date,
        now: datetime,
        ref_type: str | None = None,
        ref_id: int | None = None,
        actor_user_id: int | None = None,
        reason: str | None = None,
    ) -> GrantResult:
        """Grant energy to a pet the caller has locked with `Pets.lock`."""
        if amount == 0:
            return GrantResult(False, 0, pet.energy_total, pet.level)
        if amount < 0 and source is not Source.ADMIN_ADJUST:
            raise ValueError("only admin corrections may be negative")

        event = EnergyEvent(
            pet_id=pet.id,
            amount=amount,
            source=source.value,
            idempotency_key=key,
            game_date=game_date,
            ref_type=ref_type,
            ref_id=ref_id,
            actor_user_id=actor_user_id,
            reason=reason,
            created_at=now,
        )
        try:
            async with s.begin_nested():
                s.add(event)
                await s.flush()
        except IntegrityError:
            return GrantResult(False, 0, pet.energy_total, pet.level)

        old_level = pet.level
        pet.energy_total += amount
        new_level = max(old_level, level_for(pet.energy_total, self.levels.thresholds))
        pet.level = new_level

        level_up = None
        if new_level > old_level:
            level_up = LevelUp(
                pet_id=pet.id,
                old_level=old_level,
                new_level=new_level,
                old_stage=stage_for(old_level, self.levels.stage_starts),
                new_stage=stage_for(new_level, self.levels.stage_starts),
            )
            ActivityLog.add(s, pet.id, "level_up", now, old=old_level, new=new_level)
            if level_up.evolved:
                ActivityLog.add(s, pet.id, "evolved", now, stage=level_up.new_stage)

        return GrantResult(True, amount, pet.energy_total, pet.level, level_up, event.id)
