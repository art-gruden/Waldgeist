"""Queries used by the services. No game rules here."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from enchanted_companions.db.models import (
    Attendance,
    ClassSession,
    CommunityEvent,
    EnergyEvent,
    JobRun,
    MediaCache,
    Pet,
    PetActivityLog,
    SocialEvent,
    Species,
    User,
)


class Users:
    @staticmethod
    async def by_tg_id(s: AsyncSession, tg_id: int) -> User | None:
        return await s.scalar(select(User).where(User.tg_id == tg_id))

    @staticmethod
    async def by_username(s: AsyncSession, username: str) -> User | None:
        username = username.lstrip("@").lower()
        return await s.scalar(select(User).where(func.lower(User.username) == username))

    @staticmethod
    async def upsert(s: AsyncSession, tg_id: int, username: str | None, first_name: str) -> User:
        user = await Users.by_tg_id(s, tg_id)
        if user is None:
            user = User(tg_id=tg_id, username=username, first_name=first_name or "")
            s.add(user)
            try:
                async with s.begin_nested():
                    await s.flush()
            except IntegrityError:  # created concurrently
                user = await Users.by_tg_id(s, tg_id)
                assert user is not None
        else:
            user.username = username
            user.first_name = first_name or user.first_name
            user.is_active = True
        return user


class Pets:
    @staticmethod
    async def by_owner_tg(s: AsyncSession, tg_id: int, *, lock: bool = False) -> Pet | None:
        stmt = select(Pet).join(User, Pet.user_id == User.id).where(User.tg_id == tg_id)
        if lock:
            stmt = stmt.with_for_update(of=Pet).options(selectinload(Pet.owner), selectinload(Pet.species))
            stmt = stmt.execution_options(populate_existing=True)
        else:
            stmt = stmt.options(joinedload(Pet.owner), joinedload(Pet.species))
        return await s.scalar(stmt)

    @staticmethod
    async def get(s: AsyncSession, pet_id: int) -> Pet | None:
        return await s.scalar(
            select(Pet).where(Pet.id == pet_id).options(joinedload(Pet.owner), joinedload(Pet.species))
        )

    @staticmethod
    async def lock(s: AsyncSession, *pet_ids: int) -> dict[int, Pet]:
        """Lock pet rows in ascending id order, so concurrent transactions never deadlock."""
        result: dict[int, Pet] = {}
        for pet_id in sorted(set(pet_ids)):
            pet = await s.scalar(
                select(Pet)
                .where(Pet.id == pet_id)
                .with_for_update()
                .options(selectinload(Pet.owner), selectinload(Pet.species))
                .execution_options(populate_existing=True)
            )
            if pet is not None:
                result[pet_id] = pet
        return result

    @staticmethod
    async def all_with_owners(s: AsyncSession, *, active_only: bool = True) -> list[Pet]:
        stmt = (
            select(Pet)
            .join(User, Pet.user_id == User.id)
            .options(joinedload(Pet.owner), joinedload(Pet.species))
            .order_by(Pet.name)
        )
        if active_only:
            stmt = stmt.where(User.is_active.is_(True))
        return list((await s.scalars(stmt)).unique())

    @staticmethod
    async def count(s: AsyncSession) -> int:
        return int(await s.scalar(select(func.count()).select_from(Pet)) or 0)

    @staticmethod
    async def count_active_since(s: AsyncSession, since: datetime) -> int:
        return int(await s.scalar(select(func.count()).select_from(Pet).where(Pet.last_active_at >= since)) or 0)

    @staticmethod
    async def taken_species(s: AsyncSession) -> dict[str, str]:
        """species slug -> owner's pet name"""
        rows = await s.execute(select(Pet.species_id, Pet.name))
        return {slug: name for slug, name in rows.all()}


class SpeciesRepo:
    @staticmethod
    async def upsert_all(s: AsyncSession, rows: list[dict[str, object]]) -> None:
        existing = {sp.id: sp for sp in await s.scalars(select(Species))}
        for row in rows:
            sp = existing.get(str(row["id"]))
            if sp is None:
                s.add(Species(**row))
            else:
                for key, value in row.items():
                    setattr(sp, key, value)


class Ledger:
    @staticmethod
    async def sum_for_pet(s: AsyncSession, pet_id: int) -> int:
        return int(
            await s.scalar(select(func.coalesce(func.sum(EnergyEvent.amount), 0)).where(EnergyEvent.pet_id == pet_id))
            or 0
        )

    @staticmethod
    async def sum_since(s: AsyncSession, pet_id: int, since: date) -> int:
        return int(
            await s.scalar(
                select(func.coalesce(func.sum(EnergyEvent.amount), 0)).where(
                    EnergyEvent.pet_id == pet_id, EnergyEvent.game_date >= since
                )
            )
            or 0
        )

    @staticmethod
    async def count_by_source(
        s: AsyncSession, source: str, *, day: date | None = None, pet_id: int | None = None
    ) -> int:
        stmt = select(func.count()).select_from(EnergyEvent).where(EnergyEvent.source == source)
        if day is not None:
            stmt = stmt.where(EnergyEvent.game_date == day)
        if pet_id is not None:
            stmt = stmt.where(EnergyEvent.pet_id == pet_id)
        return int(await s.scalar(stmt) or 0)

    @staticmethod
    async def count_by_source_since(s: AsyncSession, source: str, pet_id: int, since: date) -> int:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(EnergyEvent)
                .where(EnergyEvent.source == source, EnergyEvent.pet_id == pet_id, EnergyEvent.game_date >= since)
            )
            or 0
        )


class ActivityLog:
    @staticmethod
    def add(s: AsyncSession, pet_id: int, kind: str, at: datetime, **payload: object) -> None:
        s.add(PetActivityLog(pet_id=pet_id, kind=kind, payload=payload, created_at=at))


class Classes:
    @staticmethod
    async def get(s: AsyncSession, session_id: int) -> ClassSession | None:
        return await s.get(ClassSession, session_id)

    @staticmethod
    async def live_for_code(s: AsyncSession, code: str, day: date) -> ClassSession | None:
        return await s.scalar(
            select(ClassSession)
            .where(ClassSession.live_code == code, ClassSession.game_date == day, ClassSession.status != "cancelled")
            .order_by(ClassSession.starts_at.desc())
        )

    @staticmethod
    async def recording_for_code(s: AsyncSession, code: str, now: datetime) -> ClassSession | None:
        return await s.scalar(
            select(ClassSession)
            .where(
                ClassSession.recording_code == code,
                ClassSession.recording_valid_until >= now,
                ClassSession.status != "cancelled",
            )
            .order_by(ClassSession.starts_at.desc())
        )

    @staticmethod
    async def between(s: AsyncSession, start: datetime, end: datetime) -> list[ClassSession]:
        return list(
            await s.scalars(
                select(ClassSession)
                .where(
                    ClassSession.starts_at >= start, ClassSession.starts_at < end, ClassSession.status != "cancelled"
                )
                .order_by(ClassSession.starts_at)
            )
        )

    @staticmethod
    async def on_day(s: AsyncSession, day: date) -> list[ClassSession]:
        return list(
            await s.scalars(
                select(ClassSession)
                .where(ClassSession.game_date == day, ClassSession.status != "cancelled")
                .order_by(ClassSession.starts_at)
            )
        )

    @staticmethod
    async def recent(s: AsyncSession, limit: int = 10) -> list[ClassSession]:
        return list(await s.scalars(select(ClassSession).order_by(ClassSession.starts_at.desc()).limit(limit)))

    @staticmethod
    async def last_started(s: AsyncSession, now: datetime) -> ClassSession | None:
        return await s.scalar(
            select(ClassSession)
            .where(ClassSession.starts_at <= now, ClassSession.status != "cancelled")
            .order_by(ClassSession.starts_at.desc())
        )

    @staticmethod
    async def attendance_counts(s: AsyncSession, session_id: int) -> dict[str, int]:
        rows = await s.execute(
            select(Attendance.kind, func.count()).where(Attendance.session_id == session_id).group_by(Attendance.kind)
        )
        return {kind: int(n) for kind, n in rows.all()}

    @staticmethod
    async def count_for_pet(s: AsyncSession, pet_id: int) -> int:
        return int(await s.scalar(select(func.count()).select_from(Attendance).where(Attendance.pet_id == pet_id)) or 0)


class Social:
    @staticmethod
    async def visited_today(s: AsyncSession, guest_pet_id: int, day: date) -> bool:
        found = await s.scalar(
            select(SocialEvent.id).where(
                SocialEvent.kind == "visit",
                SocialEvent.status == "visited",
                SocialEvent.from_pet_id == guest_pet_id,
                SocialEvent.game_date == day,
            )
        )
        return found is not None

    @staticmethod
    async def last_visit_out(s: AsyncSession, pet_id: int) -> SocialEvent | None:
        return await s.scalar(
            select(SocialEvent)
            .where(SocialEvent.kind == "visit", SocialEvent.status == "visited", SocialEvent.from_pet_id == pet_id)
            .order_by(SocialEvent.created_at.desc())
            .limit(1)
        )

    @staticmethod
    async def seen_affirmations(s: AsyncSession, guest_pet_id: int) -> list[str]:
        rows = await s.scalars(
            select(SocialEvent.affirmation_id).where(
                SocialEvent.from_pet_id == guest_pet_id, SocialEvent.affirmation_id.is_not(None)
            )
        )
        return [r for r in rows if r]

    @staticmethod
    async def visit_counts(s: AsyncSession, pet_id: int) -> tuple[int, int]:
        made = await s.scalar(
            select(func.count())
            .select_from(SocialEvent)
            .where(SocialEvent.kind == "visit", SocialEvent.status == "visited", SocialEvent.from_pet_id == pet_id)
        )
        received = await s.scalar(
            select(func.count())
            .select_from(SocialEvent)
            .where(SocialEvent.kind == "visit", SocialEvent.status == "visited", SocialEvent.to_pet_id == pet_id)
        )
        return int(made or 0), int(received or 0)


class Community:
    @staticmethod
    async def exists(s: AsyncSession, kind: str, dedupe_key: str) -> bool:
        found = await s.scalar(
            select(CommunityEvent.id).where(CommunityEvent.kind == kind, CommunityEvent.dedupe_key == dedupe_key)
        )
        return found is not None

    @staticmethod
    async def add_once(
        s: AsyncSession, kind: str, dedupe_key: str, day: date, at: datetime, **payload: object
    ) -> CommunityEvent | None:
        """Insert unless (kind, dedupe_key) exists. Returns the new row or None."""
        event = CommunityEvent(kind=kind, dedupe_key=dedupe_key, game_date=day, payload=payload, created_at=at)
        try:
            async with s.begin_nested():
                s.add(event)
                await s.flush()
        except IntegrityError:
            return None
        return event


class Jobs:
    @staticmethod
    async def claim(s: AsyncSession, job_name: str, run_key: str, now: datetime) -> bool:
        """Claim a run. False when it already ran (or is running). A failed run may be retried."""
        try:
            async with s.begin_nested():
                s.add(JobRun(job_name=job_name, run_key=run_key, status="running", started_at=now))
                await s.flush()
            return True
        except IntegrityError:
            pass
        result = await s.execute(
            update(JobRun)
            .where(JobRun.job_name == job_name, JobRun.run_key == run_key, JobRun.status == "failed")
            .values(status="running", started_at=now, finished_at=None, error=None)
        )
        return bool(result.rowcount)

    @staticmethod
    async def finish(s: AsyncSession, job_name: str, run_key: str, now: datetime, error: str | None = None) -> None:
        await s.execute(
            update(JobRun)
            .where(JobRun.job_name == job_name, JobRun.run_key == run_key)
            .values(status="failed" if error else "done", finished_at=now, error=error)
        )


class Media:
    @staticmethod
    async def get(s: AsyncSession, key: str) -> MediaCache | None:
        return await s.get(MediaCache, key)

    @staticmethod
    async def put(s: AsyncSession, key: str, fingerprint: str, file_id: str, now: datetime) -> None:
        row = await s.get(MediaCache, key)
        if row is None:
            s.add(MediaCache(key=key, fingerprint=fingerprint, tg_file_id=file_id, updated_at=now))
        else:
            row.fingerprint, row.tg_file_id, row.updated_at = fingerprint, file_id, now
