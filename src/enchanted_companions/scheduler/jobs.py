"""Scheduled jobs. Each daily job claims (job, date) in job_runs first, so it runs once
per day even across restarts or a second accidental instance."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.container import Container
from enchanted_companions.db.repositories import Community, Jobs

log = logging.getLogger(__name__)


async def run_once(c: Container, job_name: str, run_key: str, body: Callable[[], Awaitable[None]]) -> bool:
    now = c.clock.now()
    async with c.sm() as s, s.begin():
        if not await Jobs.claim(s, job_name, run_key, now):
            return False
    error: str | None = None
    try:
        await body()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("job failed", extra={"job": job_name, "key": run_key})
    async with c.sm() as s, s.begin():
        await Jobs.finish(s, job_name, run_key, c.clock.now(), error)
    if error is None:
        log.info("job done", extra={"job": job_name, "key": run_key})
    return error is None


async def morning_card(c: Container) -> None:
    now = c.clock.now()
    day = c.clock.game_date(now)
    if not c.morning.in_window(now):
        return  # too late today: no postcard after the window closes

    async def body() -> None:
        t = c.content.texts
        message = await c.notifier.group_photo(
            c.media.scene(c.cfg.morning.card_scene),
            t.render("morning.card", window_end=c.cfg.morning.window_end.strftime("%H:%M")),
            kb.morning_card(t),
        )
        async with c.sm() as s, s.begin():
            event = await Community.add_once(s, "morning_card", day.isoformat(), day, c.clock.now())
            if event is not None and message is not None:
                event.tg_message_id = message.message_id

    await run_once(c, "morning_card", day.isoformat(), body)


async def morning_summary(c: Container) -> None:
    day = c.clock.game_date()

    async def body() -> None:
        count = await c.morning.count_today()
        t = c.content.texts
        key = "morning.summary_none" if count == 0 else "morning.summary"
        await c.notifier.group_text(t.render(key, count=count))

    await run_once(c, "morning_summary", day.isoformat(), body)


async def sleep_nightly(c: Container) -> None:
    day = c.clock.game_date()

    async def body() -> None:
        t = c.content.texts
        for reminder in await c.sleep.nightly():
            await c.notifier.text(
                reminder.tg_id, t.render("sleep.reminder", name=reminder.pet_name), kb.reminder_off(t)
            )

    await run_once(c, "sleep_nightly", day.isoformat(), body)


async def classes_tick(c: Container) -> None:
    """Every few minutes: class announcements and the evening thank-you (idempotent)."""
    t = c.content.texts
    for item in await c.attendance.due_announcements():
        when = c.clock.local(item.session.starts_at)
        key = "classes.announce_soon" if item.hours <= 1 else "classes.announce_tomorrow"
        await c.notifier.group_text(
            t.render(
                key,
                title=item.session.title,
                time=when.strftime("%H:%M"),
                date=when.strftime("%d.%m"),
                energy=c.cfg.classes.live_energy,
            )
        )
    for thanks in await c.attendance.due_thanks():
        key = "classes.thanks" if thanks.live else "classes.thanks_empty"
        await c.notifier.group_text(t.render(key, title=thanks.session.title, count=thanks.live))


def write_heartbeat(path: Path) -> None:
    try:
        path.write_text(datetime.now().isoformat())
    except OSError:
        log.warning("cannot write heartbeat file", extra={"path": str(path)})


async def catch_up(c: Container) -> None:
    """After a restart: run today's jobs that were missed while the bot was down."""
    now = c.clock.now()
    local = c.clock.local(now).time()
    m = c.cfg.morning
    await sleep_nightly(c)
    if m.card_time <= local < m.window_end:
        await morning_card(c)
    summary_late_limit = (datetime.combine(date.min, m.summary_time) + timedelta(hours=3)).time()
    if m.summary_time <= local < summary_late_limit:
        await morning_summary(c)  # not in the evening: a late "good morning" summary would be odd
    await classes_tick(c)
