"""Visits: once a day a member visits another pet.

Host +host_energy, guest +guest_energy. A host that is visiting someone else (within
away_minutes) is "not at home": nothing is granted and the guest keeps the day's visit.
Guests of the oracle pet (@omniyaskala) also receive an affirmation or a spiritual law.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy.exc import IntegrityError

from enchanted_companions.db.models import Pet, SocialEvent
from enchanted_companions.db.repositories import Pets, Social
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.affirmations import AffirmationCatalog
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.domain.rewards import Source, visit_keys
from enchanted_companions.domain.sleep import shows_sleep_image
from enchanted_companions.services.pets import PetService
from enchanted_companions.services.reward_engine import GrantResult, RewardEngine
from enchanted_companions.settings import GameConfig


class VisitStatus(StrEnum):
    VISITED = "visited"
    NOBODY_HOME = "nobody_home"
    ALREADY_TODAY = "already_today"
    SELF = "self"
    NOT_REGISTERED = "not_registered"
    NO_HOST = "no_host"


@dataclass(frozen=True, slots=True)
class VisitResult:
    status: VisitStatus
    guest: Pet | None = None
    host: Pet | None = None
    host_sleeping: bool = False
    affirmation_html: str | None = None
    guest_grant: GrantResult | None = None
    host_grant: GrantResult | None = None


class VisitService:
    def __init__(
        self,
        sm: SessionFactory,
        clock: GameClock,
        cfg: GameConfig,
        engine: RewardEngine,
        pets: PetService,
        affirmations: AffirmationCatalog,
        rng: random.Random | None = None,
    ) -> None:
        self.sm, self.clock, self.cfg, self.engine, self.pets = sm, clock, cfg, engine, pets
        self.affirmations = affirmations
        self.rng = rng or random.Random()

    def is_oracle(self, pet: Pet) -> bool:
        oracle = self.cfg.visits.oracle_owner_tg_id
        return bool(oracle) and pet.owner.tg_id == oracle

    async def candidates(self, guest_tg_id: int) -> list[Pet]:
        async with self.sm() as s:
            return [p for p in await Pets.all_with_owners(s) if p.owner.tg_id != guest_tg_id]

    async def visited_today(self, guest_tg_id: int) -> bool:
        async with self.sm() as s:
            guest = await Pets.by_owner_tg(s, guest_tg_id)
            return guest is not None and await Social.visited_today(s, guest.id, self.clock.game_date())

    async def visit(self, guest_tg_id: int, host_pet_id: int, now: datetime | None = None) -> VisitResult:
        now = now or self.clock.now()
        day = self.clock.game_date(now)
        cfg = self.cfg.visits
        async with self.sm() as s, s.begin():
            guest_ref = await Pets.by_owner_tg(s, guest_tg_id)
            if guest_ref is None:
                return VisitResult(VisitStatus.NOT_REGISTERED)
            if guest_ref.id == host_pet_id:
                return VisitResult(VisitStatus.SELF, guest=guest_ref)
            locked = await Pets.lock(s, guest_ref.id, host_pet_id)
            guest, host = locked.get(guest_ref.id), locked.get(host_pet_id)
            if guest is None or host is None or not host.owner.is_active:
                return VisitResult(VisitStatus.NO_HOST, guest=guest)

            if await Social.visited_today(s, guest.id, day):
                return VisitResult(VisitStatus.ALREADY_TODAY, guest=guest, host=host)

            last_out = await Social.last_visit_out(s, host.id)
            if last_out is not None and last_out.created_at > now - timedelta(minutes=cfg.away_minutes):
                s.add(
                    SocialEvent(
                        kind="visit",
                        status="nobody_home",
                        from_pet_id=guest.id,
                        to_pet_id=host.id,
                        game_date=day,
                        created_at=now,
                    )
                )
                return VisitResult(VisitStatus.NOBODY_HOME, guest=guest, host=host)

            affirmation_id: str | None = None
            affirmation_html: str | None = None
            if self.is_oracle(host) and len(self.affirmations):
                entry = self.affirmations.pick(await Social.seen_affirmations(s, guest.id), self.rng)
                if entry is not None:
                    affirmation_id, affirmation_html = entry.id, self.affirmations.render(entry)

            try:
                async with s.begin_nested():
                    s.add(
                        SocialEvent(
                            kind="visit",
                            status="visited",
                            from_pet_id=guest.id,
                            to_pet_id=host.id,
                            game_date=day,
                            affirmation_id=affirmation_id,
                            created_at=now,
                        )
                    )
                    await s.flush()
            except IntegrityError:
                return VisitResult(VisitStatus.ALREADY_TODAY, guest=guest, host=host)

            guest_key, host_key = visit_keys(guest.id, day)
            guest_grant = await self.engine.grant(
                s,
                guest,
                amount=cfg.guest_energy,
                source=Source.VISIT_GUEST,
                key=guest_key,
                game_date=day,
                now=now,
                ref_type="pet",
                ref_id=host.id,
            )
            host_grant = await self.engine.grant(
                s,
                host,
                amount=cfg.host_energy,
                source=Source.VISIT_HOST,
                key=host_key,
                game_date=day,
                now=now,
                ref_type="pet",
                ref_id=guest.id,
            )
            return VisitResult(
                VisitStatus.VISITED,
                guest=guest,
                host=host,
                host_sleeping=shows_sleep_image(self.pets.current_state(host, now)),
                affirmation_html=affirmation_html,
                guest_grant=guest_grant,
                host_grant=host_grant,
            )
