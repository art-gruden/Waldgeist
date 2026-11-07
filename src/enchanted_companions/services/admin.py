"""Administrator actions. Every action is logged with the admin's Telegram ID."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from enchanted_companions.content import Content
from enchanted_companions.db.models import Pet, User
from enchanted_companions.db.repositories import ActivityLog, Pets, Users
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.domain.rewards import Source, admin_key
from enchanted_companions.services.reward_engine import GrantResult, RewardEngine

log = logging.getLogger(__name__)


class AdminError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Adjustment:
    pet: Pet
    grant: GrantResult


class AdminService:
    def __init__(
        self, sm: SessionFactory, clock: GameClock, engine: RewardEngine, content: Content, admin_ids: list[int]
    ) -> None:
        self.sm, self.clock, self.engine, self.content = sm, clock, engine, content
        self.admin_ids = set(admin_ids)

    def is_admin(self, tg_id: int | None) -> bool:
        return tg_id is not None and tg_id in self.admin_ids

    @staticmethod
    async def _resolve(s, who: str) -> User | None:
        who = who.strip()
        if who.lstrip("-").isdigit():
            return await Users.by_tg_id(s, int(who))
        if who.startswith("@") or who.replace("_", "").isalnum():
            return await Users.by_username(s, who)
        return None

    async def find_pet(self, who: str) -> Pet | None:
        async with self.sm() as s:
            user = await self._resolve(s, who)
            return await Pets.by_owner_tg(s, user.tg_id) if user else None

    async def adjust(self, admin_tg_id: int, who: str, amount: int, reason: str) -> Adjustment:
        if amount == 0:
            raise AdminError("amount")
        if not reason.strip():
            raise AdminError("reason")
        now = self.clock.now()
        async with self.sm() as s, s.begin():
            user = await self._resolve(s, who)
            if user is None:
                raise AdminError("who")
            pet = await Pets.by_owner_tg(s, user.tg_id, lock=True)
            if pet is None:
                raise AdminError("no_pet")
            admin = await Users.by_tg_id(s, admin_tg_id)
            grant = await self.engine.grant(
                s,
                pet,
                amount=amount,
                source=Source.ADMIN_ADJUST,
                key=admin_key(),
                game_date=self.clock.game_date(now),
                now=now,
                actor_user_id=admin.id if admin else None,
                reason=reason.strip()[:500],
            )
        log.info("admin energy adjustment", extra={"admin": admin_tg_id, "pet": pet.id, "amount": amount})
        return Adjustment(pet, grant)

    async def change_species(self, admin_tg_id: int, who: str, slug: str) -> Pet:
        if self.content.species_by_slug(slug) is None:
            raise AdminError("species")
        now = self.clock.now()
        async with self.sm() as s, s.begin():
            user = await self._resolve(s, who)
            if user is None:
                raise AdminError("who")
            pet = await Pets.by_owner_tg(s, user.tg_id, lock=True)
            if pet is None:
                raise AdminError("no_pet")
            old = pet.species_id
            pet.species_id = slug
            try:
                async with s.begin_nested():
                    await s.flush()
            except IntegrityError as exc:
                raise AdminError("taken") from exc
            ActivityLog.add(s, pet.id, "species_changed", now, old=old, new=slug, admin=admin_tg_id)
        log.info("admin species change", extra={"admin": admin_tg_id, "pet": pet.id, "species": slug})
        async with self.sm() as s:
            loaded = await Pets.get(s, pet.id)
            assert loaded is not None
            return loaded
