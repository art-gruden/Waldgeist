"""Database models (PostgreSQL in production; tests can also run on SQLite).

The energy ledger (`energy_events`) is the source of truth. `pets.energy_total` and `pets.level`
are a cache updated in the same transaction as each ledger row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator[datetime]):
    """TIMESTAMPTZ that always returns aware UTC datetimes (also on SQLite in tests)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed; use timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# BIGINT on PostgreSQL, INTEGER on SQLite (so autoincrement works in tests)
BigId = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _now_default() -> object:
    return func.now()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    birth_day: Mapped[int | None] = mapped_column(SmallInteger)
    birth_month: Mapped[int | None] = mapped_column(SmallInteger)
    reminders_on: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), server_default=_now_default())

    pet: Mapped[Pet | None] = relationship(back_populates="owner", uselist=False)

    __table_args__ = (
        CheckConstraint("birth_day IS NULL OR (birth_day BETWEEN 1 AND 31)", name="birth_day_range"),
        CheckConstraint("birth_month IS NULL OR (birth_month BETWEEN 1 AND 12)", name="birth_month_range"),
    )


class Species(Base):
    __tablename__ = "species"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # slug
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    emoji: Mapped[str] = mapped_column(String(16), default="✨", nullable=False)
    character: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    favorite_place: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Pet(Base):
    __tablename__ = "pets"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), unique=True)
    species_id: Mapped[str] = mapped_column(ForeignKey("species.id", ondelete="RESTRICT"), unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    energy_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    sleep_state: Mapped[str] = mapped_column(String(16), default="awake", nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), server_default=_now_default())

    owner: Mapped[User] = relationship(back_populates="pet")
    species: Mapped[Species] = relationship()

    __table_args__ = (Index("ix_pets_last_active_at", "last_active_at"),)


class EnergyEvent(Base):
    __tablename__ = "energy_events"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    pet_id: Mapped[int] = mapped_column(ForeignKey("pets.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    game_date: Mapped[date] = mapped_column(Date, nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount_nonzero"),
        CheckConstraint("amount > 0 OR source = 'admin_adjust'", name="negative_only_admin"),
        Index("ix_energy_events_pet_day", "pet_id", "game_date"),
        Index("ix_energy_events_source_day", "source", "game_date"),
    )


class PetActivityLog(Base):
    __tablename__ = "pet_activity_log"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    pet_id: Mapped[int] = mapped_column(ForeignKey("pets.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    __table_args__ = (Index("ix_pet_activity_log_pet_time", "pet_id", "created_at"),)


class ClassSession(Base):
    __tablename__ = "class_sessions"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    game_date: Mapped[date] = mapped_column(Date, nullable=False)
    live_code: Mapped[str] = mapped_column(String(64), nullable=False)
    recording_code: Mapped[str | None] = mapped_column(String(64))
    recording_valid_until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    status: Mapped[str] = mapped_column(String(16), default="scheduled", nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), server_default=_now_default())

    __table_args__ = (Index("ix_class_sessions_starts_at", "starts_at"),)


class Attendance(Base):
    __tablename__ = "attendance"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("class_sessions.id", ondelete="RESTRICT"), nullable=False)
    pet_id: Mapped[int] = mapped_column(ForeignKey("pets.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # live | recording
    method: Mapped[str] = mapped_column(String(16), nullable=False)  # code | admin
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    energy_event_id: Mapped[int | None] = mapped_column(ForeignKey("energy_events.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "pet_id"),)


class SocialEvent(Base):
    __tablename__ = "social_events"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # visit
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # visited | nobody_home
    from_pet_id: Mapped[int] = mapped_column(ForeignKey("pets.id", ondelete="RESTRICT"), nullable=False)
    to_pet_id: Mapped[int] = mapped_column(ForeignKey("pets.id", ondelete="RESTRICT"), nullable=False)
    game_date: Mapped[date] = mapped_column(Date, nullable=False)
    affirmation_id: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    __table_args__ = (
        Index("ix_social_events_from_time", "from_pet_id", "created_at"),
        Index("ix_social_events_to_time", "to_pet_id", "created_at"),
        Index(
            "uq_social_events_one_visit_per_day",
            "kind",
            "from_pet_id",
            "game_date",
            unique=True,
            postgresql_where=sql_text("status = 'visited'"),
            sqlite_where=sql_text("status = 'visited'"),
        ),
    )


class CommunityEvent(Base):
    __tablename__ = "community_events"

    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    game_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    __table_args__ = (UniqueConstraint("kind", "dedupe_key"),)


class JobRun(Base):
    __tablename__ = "job_runs"

    job_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # running | done | failed
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    error: Mapped[str | None] = mapped_column(Text)


class MediaCache(Base):
    __tablename__ = "media_cache"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)  # relative path, e.g. fox/stage-1.jpg
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 of the file
    tg_file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
