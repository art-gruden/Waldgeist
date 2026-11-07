"""Test fixtures.

Integration tests use TEST_DATABASE_URL (PostgreSQL recommended, as in production):
    TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/enchanted_test pytest
Without it they fall back to a temporary SQLite file.
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from enchanted_companions.content import Content
from enchanted_companions.db.models import Base
from enchanted_companions.db.seed import seed_species
from enchanted_companions.db.session import create_session_factory
from enchanted_companions.domain.affirmations import AffirmationCatalog
from enchanted_companions.domain.clock import GameClock
from enchanted_companions.settings import GameConfig

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeClock(GameClock):
    """GameClock whose 'now' is set by the test (UTC)."""

    def __init__(self, cfg: GameConfig, start: datetime) -> None:
        self.current = start
        super().__init__(cfg.tz, now=lambda: self.current)

    def set_local(self, year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
        self.current = datetime(year, month, day, hour, minute, tzinfo=self.tz).astimezone(UTC)
        return self.current

    def advance(self, **kwargs) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current


@pytest.fixture(scope="session")
def cfg() -> GameConfig:
    return GameConfig.load(ROOT / "config" / "game.yaml")


@pytest.fixture(scope="session")
def content() -> Content:
    c = Content.load(ROOT / "content" / "uk")
    c.texts._rng = random.Random(1)
    return c


@pytest.fixture(scope="session")
def affirmations() -> AffirmationCatalog:
    return AffirmationCatalog.load(FIXTURES / "affirmations.yaml")


@pytest.fixture
def clock(cfg: GameConfig) -> FakeClock:
    clk = FakeClock(cfg, datetime(2026, 11, 9, 8, 0, tzinfo=UTC))
    clk.set_local(2026, 11, 9, 8, 0)  # Monday morning in Kyiv
    return clk


@pytest_asyncio.fixture
async def sm(tmp_path, content):
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    await seed_species(factory, content)
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def is_postgres() -> bool:
    return (os.environ.get("TEST_DATABASE_URL") or "").startswith("postgresql")
