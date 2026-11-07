"""Registration: species choice (first come, first served), pet name, optional birthday."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from enchanted_companions.content import Content, SpeciesInfo
from enchanted_companions.db.models import Pet, User
from enchanted_companions.db.repositories import ActivityLog, Pets, Users
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.settings import GameConfig


class RegistrationError(Exception):
    """Base for expected registration problems shown to the user."""


class SpeciesTaken(RegistrationError):
    pass


class UnknownSpecies(RegistrationError):
    pass


class AlreadyRegistered(RegistrationError):
    pass


class InvalidName(RegistrationError):
    pass


class InvalidBirthday(RegistrationError):
    pass


@dataclass(frozen=True, slots=True)
class SpeciesSlot:
    species: SpeciesInfo
    taken_by: str | None  # pet name of the owner, if taken


_NAME_RE = re.compile(r"^[^\W_](?:[\w' \-]*[^\W_])?$", re.UNICODE)
_BIRTHDAY_RE = re.compile(r"^\s*(\d{1,2})[./\-](\d{1,2})\s*$")
_DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def clean_name(raw: str, min_len: int, max_len: int) -> str:
    name = re.sub(r"\s+", " ", (raw or "").replace("’", "'").replace("ʼ", "'")).strip()
    if not (min_len <= len(name) <= max_len):
        raise InvalidName("length")
    lowered = name.lower()
    if "http" in lowered or "www." in lowered or "@" in name or "/" in name:
        raise InvalidName("link")
    if not _NAME_RE.match(name):
        raise InvalidName("chars")
    return name


def parse_birthday(raw: str) -> tuple[int, int]:
    match = _BIRTHDAY_RE.match(raw or "")
    if not match:
        raise InvalidBirthday("format")
    day, month = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 1 <= day <= _DAYS_IN_MONTH[month - 1]):
        raise InvalidBirthday("range")
    return day, month


class RegistrationService:
    def __init__(self, sm: SessionFactory, clock: GameClock, cfg: GameConfig, content: Content) -> None:
        self.sm, self.clock, self.cfg, self.content = sm, clock, cfg, content

    async def start(self, tg_id: int, username: str | None, first_name: str) -> tuple[User, Pet | None]:
        async with self.sm() as s, s.begin():
            user = await Users.upsert(s, tg_id, username, first_name)
            await s.flush()
            pet = await Pets.by_owner_tg(s, tg_id)
            return user, pet

    async def species_board(self) -> list[SpeciesSlot]:
        async with self.sm() as s:
            taken = await Pets.taken_species(s)
        return [SpeciesSlot(sp, taken.get(sp.slug)) for sp in self.content.species]

    async def is_taken(self, slug: str) -> bool:
        async with self.sm() as s:
            return slug in await Pets.taken_species(s)

    def clean_name(self, raw: str) -> str:
        reg = self.cfg.registration
        return clean_name(raw, reg.name_min_length, reg.name_max_length)

    async def register(
        self,
        tg_id: int,
        username: str | None,
        first_name: str,
        species_slug: str,
        pet_name: str,
        birthday: tuple[int, int] | None,
    ) -> Pet:
        if self.content.species_by_slug(species_slug) is None:
            raise UnknownSpecies(species_slug)
        name = self.clean_name(pet_name)
        now = self.clock.now()
        async with self.sm() as s, s.begin():
            user = await Users.upsert(s, tg_id, username, first_name)
            await s.flush()
            if await Pets.by_owner_tg(s, tg_id) is not None:
                raise AlreadyRegistered()
            if birthday is not None:
                user.birth_day, user.birth_month = birthday
            pet = Pet(user_id=user.id, species_id=species_slug, name=name, last_active_at=now)
            try:
                async with s.begin_nested():
                    s.add(pet)
                    await s.flush()
            except IntegrityError as exc:
                raise SpeciesTaken(species_slug) from exc
            ActivityLog.add(s, pet.id, "registered", now, species=species_slug)
        async with self.sm() as s:
            loaded = await Pets.get(s, pet.id)
            assert loaded is not None
            return loaded
