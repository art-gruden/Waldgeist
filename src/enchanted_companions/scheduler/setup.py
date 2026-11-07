"""APScheduler wiring. The scheduler only wakes jobs up; all state lives in PostgreSQL."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from enchanted_companions.container import Container
from enchanted_companions.scheduler import jobs


def build_scheduler(c: Container) -> AsyncIOScheduler:
    tz = c.cfg.tz
    scheduler = AsyncIOScheduler(
        timezone=tz,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 15 * 60},
    )

    def cron(t) -> CronTrigger:
        return CronTrigger(hour=t.hour, minute=t.minute, timezone=tz)

    m, sl = c.cfg.morning, c.cfg.sleep
    scheduler.add_job(jobs.morning_card, cron(m.card_time), args=[c], id="morning_card")
    scheduler.add_job(jobs.morning_summary, cron(m.summary_time), args=[c], id="morning_summary")
    scheduler.add_job(jobs.sleep_nightly, cron(sl.job_time), args=[c], id="sleep_nightly")
    scheduler.add_job(jobs.classes_tick, IntervalTrigger(minutes=5), args=[c], id="classes_tick")
    scheduler.add_job(
        jobs.write_heartbeat, IntervalTrigger(seconds=30), args=[c.settings.heartbeat_file], id="heartbeat"
    )
    return scheduler
