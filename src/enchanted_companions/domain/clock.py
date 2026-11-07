"""Game time. The database stores UTC; every daily limit and report uses the local game date."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


class GameClock:
    def __init__(self, tz: ZoneInfo, now: Callable[[], datetime] | None = None) -> None:
        self.tz = tz
        self._now = now or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        """Current moment, timezone-aware UTC."""
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return value.astimezone(UTC)

    def local(self, moment: datetime | None = None) -> datetime:
        return (moment or self.now()).astimezone(self.tz)

    def game_date(self, moment: datetime | None = None) -> date:
        return self.local(moment).date()

    def in_window(self, start: time, end: time, moment: datetime | None = None) -> bool:
        """True when the local time is in [start, end)."""
        local_time = self.local(moment).time()
        return start <= local_time < end

    def at(self, day: date, at_time: time) -> datetime:
        """The UTC moment of a local wall-clock time on a game date (DST-safe)."""
        return datetime.combine(day, at_time, tzinfo=self.tz).astimezone(UTC)

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        """[start, end) of a local day in UTC; 23 or 25 hours long on DST change days."""
        start = self.at(day, time(0, 0))
        end = self.at(day + timedelta(days=1), time(0, 0))
        return start, end

    def month_start(self, moment: datetime | None = None) -> date:
        return self.game_date(moment).replace(day=1)


def iso_week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def days_between(earlier: date, later: date) -> int:
    return (later - earlier).days
