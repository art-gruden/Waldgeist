"""Weekly online classes: code words, recordings, admin marks, announcements and thanks."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from sqlalchemy.exc import IntegrityError

from enchanted_companions.db.models import Attendance, ClassSession, Pet
from enchanted_companions.db.repositories import Classes, Community, Pets, Users
from enchanted_companions.db.session import SessionFactory
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.domain.rewards import Source, class_key
from enchanted_companions.services.reward_engine import GrantResult, RewardEngine
from enchanted_companions.settings import GameConfig

_EDGE_PUNCT = "\"'«»„“”.,!?;:()[]{}"


def normalize_code(raw: str) -> str:
    code = (raw or "").replace("ё", "е").replace("Ё", "Е").replace("’", "'").lower()
    code = re.sub(r"\s+", " ", code).strip().strip(_EDGE_PUNCT).strip()
    return code


class RedeemStatus(StrEnum):
    OK = "ok"
    ALREADY = "already"
    INVALID = "invalid"
    LOCKED = "locked"
    NOT_REGISTERED = "not_registered"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class RedeemResult:
    status: RedeemStatus
    kind: str | None = None  # live | recording
    session: ClassSession | None = None
    pet: Pet | None = None
    grant: GrantResult | None = None
    retry_in_minutes: int = 0


@dataclass(frozen=True, slots=True)
class Announcement:
    session: ClassSession
    hours: int


@dataclass(frozen=True, slots=True)
class Thanks:
    session: ClassSession
    live: int
    recording: int


class AttendanceService:
    def __init__(self, sm: SessionFactory, clock: GameClock, cfg: GameConfig, engine: RewardEngine) -> None:
        self.sm, self.clock, self.cfg, self.engine = sm, clock, cfg, engine
        self._failures: dict[int, deque[datetime]] = defaultdict(deque)

    # ------------------------------------------------------------- admin side

    async def create_session(self, admin_tg_id: int, day: date, at: time, title: str, live_code: str) -> ClassSession:
        code = normalize_code(live_code)
        if not code:
            raise ValueError("empty code")
        async with self.sm() as s, s.begin():
            admin = await Users.by_tg_id(s, admin_tg_id)
            session = ClassSession(
                title=title.strip()[:200],
                starts_at=self.clock.at(day, at),
                game_date=day,
                live_code=code,
                created_by=admin.id if admin else None,
            )
            s.add(session)
            await s.flush()
            return session

    async def set_recording_code(self, session_id: int, raw_code: str) -> ClassSession | None:
        code = normalize_code(raw_code)
        if not code:
            raise ValueError("empty code")
        now = self.clock.now()
        async with self.sm() as s, s.begin():
            session = await Classes.get(s, session_id)
            if session is None:
                return None
            session.recording_code = code
            session.recording_valid_until = now + timedelta(days=self.cfg.classes.recording_valid_days)
            return session

    async def cancel(self, session_id: int) -> ClassSession | None:
        async with self.sm() as s, s.begin():
            session = await Classes.get(s, session_id)
            if session is not None:
                session.status = "cancelled"
            return session

    async def recent(self, limit: int = 8) -> list[tuple[ClassSession, dict[str, int]]]:
        async with self.sm() as s:
            sessions = await Classes.recent(s, limit)
            return [(cs, await Classes.attendance_counts(s, cs.id)) for cs in sessions]

    async def admin_mark(
        self, admin_tg_id: int, session_id: int, target_pet_id: int, kind: str, now: datetime | None = None
    ) -> RedeemResult:
        if kind not in ("live", "recording"):
            raise ValueError("kind must be live or recording")
        now = now or self.clock.now()
        async with self.sm() as s, s.begin():
            session = await Classes.get(s, session_id)
            if session is None:
                return RedeemResult(RedeemStatus.INVALID)
            admin = await Users.by_tg_id(s, admin_tg_id)
            pets = await Pets.lock(s, target_pet_id)
            pet = pets.get(target_pet_id)
            if pet is None:
                return RedeemResult(RedeemStatus.NOT_REGISTERED)
            return await self._record(s, session, pet, kind, "admin", admin.id if admin else None, now)

    # -------------------------------------------------------------- user side

    def _locked_minutes(self, tg_id: int, now: datetime) -> int:
        cfg = self.cfg.classes
        window = timedelta(minutes=cfg.lockout_minutes)
        failures = self._failures[tg_id]
        while failures and now - failures[0] > window:
            failures.popleft()
        if len(failures) >= cfg.max_failed_attempts:
            remaining = window - (now - failures[0])
            return max(1, int(remaining.total_seconds() // 60) + 1)
        return 0

    async def redeem(self, tg_id: int, raw_code: str, now: datetime | None = None) -> RedeemResult:
        now = now or self.clock.now()
        code = normalize_code(raw_code)
        if not code:
            return RedeemResult(RedeemStatus.EMPTY)
        locked = self._locked_minutes(tg_id, now)
        if locked:
            return RedeemResult(RedeemStatus.LOCKED, retry_in_minutes=locked)

        today = self.clock.game_date(now)
        async with self.sm() as s, s.begin():
            pet = await Pets.by_owner_tg(s, tg_id, lock=True)
            if pet is None:
                return RedeemResult(RedeemStatus.NOT_REGISTERED)
            session = await Classes.live_for_code(s, code, today)
            kind = "live"
            if session is None:
                session = await Classes.recording_for_code(s, code, now)
                kind = "recording"
            if session is None:
                self._failures[tg_id].append(now)
                return RedeemResult(RedeemStatus.INVALID, pet=pet)
            result = await self._record(s, session, pet, kind, "code", None, now)
        if result.status is RedeemStatus.OK:
            self._failures.pop(tg_id, None)
        return result

    async def _record(
        self,
        s,
        session: ClassSession,
        pet: Pet,
        kind: str,
        method: str,
        confirmed_by: int | None,
        now: datetime,
    ) -> RedeemResult:
        cfg = self.cfg.classes
        amount = cfg.live_energy if kind == "live" else cfg.recording_energy
        source = Source.CLASS_LIVE if kind == "live" else Source.CLASS_RECORDING
        grant = await self.engine.grant(
            s,
            pet,
            amount=amount,
            source=source,
            key=class_key(session.id, pet.id),
            game_date=self.clock.game_date(now),
            now=now,
            ref_type="class_session",
            ref_id=session.id,
            actor_user_id=confirmed_by,
        )
        if not grant.granted:
            return RedeemResult(RedeemStatus.ALREADY, kind=kind, session=session, pet=pet)
        try:
            async with s.begin_nested():
                s.add(
                    Attendance(
                        session_id=session.id,
                        pet_id=pet.id,
                        kind=kind,
                        method=method,
                        confirmed_by=confirmed_by,
                        energy_event_id=grant.event_id,
                        created_at=now,
                    )
                )
                await s.flush()
        except IntegrityError:
            # The ledger key already guarantees one reward per class; this keeps both tables in step.
            return RedeemResult(RedeemStatus.ALREADY, kind=kind, session=session, pet=pet)
        return RedeemResult(RedeemStatus.OK, kind=kind, session=session, pet=pet, grant=grant)

    # ------------------------------------------------------------- scheduler

    async def due_announcements(self, now: datetime | None = None) -> list[Announcement]:
        """Announcements to post now. Each (session, hours) is returned once; a late-created
        session gets only the most urgent one."""
        now = now or self.clock.now()
        hours = sorted(set(self.cfg.classes.announce_before_hours))
        if not hours:
            return []
        due: list[Announcement] = []
        async with self.sm() as s, s.begin():
            for session in await Classes.between(s, now, now + timedelta(hours=max(hours))):
                left = session.starts_at - now
                eligible = [h for h in hours if left <= timedelta(hours=h)]
                if not eligible:
                    continue
                target = min(eligible)
                if await Community.exists(s, "class_announce", f"{session.id}:{target}"):
                    continue
                for h in eligible:
                    await Community.add_once(
                        s, "class_announce", f"{session.id}:{h}", self.clock.game_date(now), now, hours=h
                    )
                due.append(Announcement(session, target))
        return due

    async def due_thanks(self, now: datetime | None = None) -> list[Thanks]:
        now = now or self.clock.now()
        today = self.clock.game_date(now)
        if now < self.clock.at(today, self.cfg.classes.thanks_time):
            return []
        result: list[Thanks] = []
        async with self.sm() as s, s.begin():
            for session in await Classes.on_day(s, today):
                if session.starts_at > now:
                    continue
                if await Community.add_once(s, "class_thanks", str(session.id), today, now) is None:
                    continue
                counts = await Classes.attendance_counts(s, session.id)
                result.append(Thanks(session, counts.get("live", 0), counts.get("recording", 0)))
        return result
