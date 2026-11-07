"""Service-level integration tests against a real database (PostgreSQL via TEST_DATABASE_URL)."""

from __future__ import annotations

import asyncio
from datetime import date, time

import pytest
from sqlalchemy import func, select

from enchanted_companions.db.models import EnergyEvent, JobRun, Pet
from enchanted_companions.db.repositories import Jobs, Pets
from enchanted_companions.domain.rewards import Source
from enchanted_companions.domain.sleep import SleepState
from enchanted_companions.services.admin import AdminError
from enchanted_companions.services.attendance import RedeemStatus
from enchanted_companions.services.morning import MorningStatus
from enchanted_companions.services.registration import AlreadyRegistered, SpeciesTaken
from enchanted_companions.services.visits import VisitStatus
from tests.conftest import is_postgres
from tests.integration.helpers import ADMIN_TG, ORACLE_TG, make_services, register


@pytest.fixture
def svc(sm, clock, cfg, content, affirmations):
    return make_services(sm, clock, cfg, content, affirmations)


async def energy_of(sm, pet_id: int) -> tuple[int, int, int]:
    """(cached total, ledger sum, level)"""
    async with sm() as s:
        pet = await s.get(Pet, pet_id)
        ledger = await s.scalar(
            select(func.coalesce(func.sum(EnergyEvent.amount), 0)).where(EnergyEvent.pet_id == pet_id)
        )
        return pet.energy_total, int(ledger), pet.level


# ------------------------------------------------------------- registration


async def test_species_is_first_come_first_served(svc) -> None:
    await register(svc, 1, "fox", "Фелікс")
    with pytest.raises(SpeciesTaken):
        await register(svc, 2, "fox", "Лис")
    with pytest.raises(AlreadyRegistered):
        await register(svc, 1, "owl", "Сова")
    board = {slot.species.slug: slot.taken_by for slot in await svc.registration.species_board()}
    assert board["fox"] == "Фелікс" and board["owl"] is None and len(board) == 16


async def test_concurrent_registration_of_same_species(svc) -> None:
    results = await asyncio.gather(
        register(svc, 1, "unicorn", "Перший"), register(svc, 2, "unicorn", "Другий"), return_exceptions=True
    )
    assert sum(isinstance(r, SpeciesTaken) for r in results) == 1
    assert sum(isinstance(r, Pet) for r in results) == 1


# ------------------------------------------------------------------ morning


async def test_morning_once_per_day(svc, sm, clock) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    clock.set_local(2026, 11, 9, 7, 5)
    first = await svc.morning.greet(1)
    second = await svc.morning.greet(1)
    assert first.status is MorningStatus.COUNTED and second.status is MorningStatus.ALREADY
    assert await energy_of(sm, pet.id) == (10, 10, 1)
    clock.set_local(2026, 11, 10, 6, 0)
    assert (await svc.morning.greet(1)).status is MorningStatus.COUNTED
    assert (await energy_of(sm, pet.id))[0] == 20
    assert await svc.morning.count_today() == 1


async def test_morning_window(svc, clock) -> None:
    await register(svc, 1, "fox", "Фелікс")
    clock.set_local(2026, 11, 9, 4, 59)
    assert (await svc.morning.greet(1)).status is MorningStatus.OUTSIDE_WINDOW
    clock.set_local(2026, 11, 9, 12, 0)
    assert (await svc.morning.greet(1)).status is MorningStatus.OUTSIDE_WINDOW
    clock.set_local(2026, 11, 9, 11, 59)
    assert (await svc.morning.greet(1)).status is MorningStatus.COUNTED


async def test_concurrent_greetings_pay_once(svc, sm, clock) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    clock.set_local(2026, 11, 9, 8, 0)
    results = await asyncio.gather(*(svc.morning.greet(1) for _ in range(5)))
    assert [r.status for r in results].count(MorningStatus.COUNTED) == 1
    assert await energy_of(sm, pet.id) == (10, 10, 1)


async def test_unregistered_invited_once(svc) -> None:
    assert (await svc.morning.greet(999)).status is MorningStatus.NOT_REGISTERED
    assert await svc.morning.claim_invite(999) is True
    assert await svc.morning.claim_invite(999) is False


async def test_message_mode(svc) -> None:
    assert svc.morning.message_counts("Доброго ранку!")
    assert not svc.morning.message_counts("Що задали?")


# ------------------------------------------------------------ reward engine


async def test_level_up_and_evolution(svc, sm, clock) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    adj = await svc.admin.adjust(ADMIN_TG, "1", 760, "тест")
    assert adj.grant.level_up and adj.grant.level_up.old_level == 1 and adj.grant.level_up.new_level == 4
    assert adj.grant.level_up.evolved and adj.grant.level_up.new_stage == 2
    assert await energy_of(sm, pet.id) == (760, 760, 4)


async def test_admin_correction_never_lowers_level(svc, sm) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    await svc.admin.adjust(ADMIN_TG, "@user1", 450, "помилково")
    result = await svc.admin.adjust(ADMIN_TG, "1", -450, "скасування")
    assert result.grant.granted and result.grant.level_up is None
    assert await energy_of(sm, pet.id) == (0, 0, 3)


async def test_admin_errors(svc) -> None:
    await register(svc, 1, "fox", "Фелікс")
    for who, amount, reason, code in [("1", 0, "x", "amount"), ("1", 5, " ", "reason"), ("@ghost", 5, "x", "who")]:
        with pytest.raises(AdminError) as exc:
            await svc.admin.adjust(ADMIN_TG, who, amount, reason)
        assert exc.value.args[0] == code


async def test_negative_grant_only_for_admin(svc, sm, clock) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    async with sm() as s, s.begin():
        locked = (await Pets.lock(s, pet.id))[pet.id]
        with pytest.raises(ValueError):
            await svc.engine.grant(
                s, locked, amount=-5, source=Source.MORNING, key="x", game_date=date(2026, 11, 9), now=clock.now()
            )


async def test_change_species_keeps_energy(svc, sm) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    await register(svc, 2, "owl", "Сова")
    await svc.admin.adjust(ADMIN_TG, "1", 200, "тест")
    changed = await svc.admin.change_species(ADMIN_TG, "1", "dragon")
    assert changed.species_id == "dragon" and changed.energy_total == 200 and changed.level == 2
    with pytest.raises(AdminError):
        await svc.admin.change_species(ADMIN_TG, "1", "owl")
    assert (await energy_of(sm, pet.id))[0] == 200


# -------------------------------------------------------------------- sleep


async def test_natural_sleep_and_single_reminder(svc, sm, clock) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    clock.set_local(2026, 11, 9, 9, 0)
    await svc.pets.touch_by_tg(1)

    clock.set_local(2026, 11, 13, 0, 5)  # 4 days later
    assert await svc.sleep.nightly() == []
    async with sm() as s:
        assert (await s.get(Pet, pet.id)).sleep_state == SleepState.SLEEPING

    clock.set_local(2026, 11, 16, 0, 5)  # 7 days later: one reminder
    reminders = await svc.sleep.nightly()
    assert [r.tg_id for r in reminders] == [1]
    clock.set_local(2026, 11, 17, 0, 5)
    assert await svc.sleep.nightly() == []  # never twice

    clock.set_local(2026, 11, 17, 10, 0)
    _, woke_from = await svc.pets.touch_by_tg(1)
    assert woke_from is SleepState.DREAMING
    view = await svc.pets.profile(1)
    assert view.state is SleepState.AWAKE and view.pet.energy_total == 0  # nothing lost, nothing gained


async def test_reminders_can_be_disabled(svc, clock) -> None:
    await register(svc, 1, "fox", "Фелікс")
    await svc.sleep.set_reminders(1, False)
    clock.advance(days=10)
    assert await svc.sleep.nightly() == []


# ------------------------------------------------------------------ classes


async def make_class(svc, clock, code: str = "Світанок"):
    return await svc.attendance.create_session(ADMIN_TG, date(2026, 11, 12), time(19, 0), "Карма", code)


async def test_live_code_on_class_day_only(svc, sm, clock) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    session = await make_class(svc, clock)
    clock.set_local(2026, 11, 11, 19, 30)
    assert (await svc.attendance.redeem(1, "світанок")).status is RedeemStatus.INVALID
    clock.set_local(2026, 11, 12, 19, 40)
    ok = await svc.attendance.redeem(1, "«СВІТАНОК»")
    assert ok.status is RedeemStatus.OK and ok.kind == "live" and ok.session.id == session.id
    assert (await svc.attendance.redeem(1, "світанок")).status is RedeemStatus.ALREADY
    assert await energy_of(sm, pet.id) == (30, 30, 1)


async def test_recording_code_valid_7_days_and_not_both(svc, sm, clock) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    other = await register(svc, 2, "owl", "Сова")
    session = await make_class(svc, clock)
    clock.set_local(2026, 11, 12, 22, 0)
    await svc.attendance.set_recording_code(session.id, "Місяць")
    clock.set_local(2026, 11, 18, 20, 0)
    rec = await svc.attendance.redeem(2, "місяць")
    assert rec.status is RedeemStatus.OK and rec.kind == "recording" and rec.grant.amount == 15
    clock.set_local(2026, 11, 20, 20, 0)
    assert (await svc.attendance.redeem(1, "місяць")).status is RedeemStatus.INVALID  # expired
    assert (await energy_of(sm, other.id))[0] == 15

    # live participant cannot add the recording on top
    clock.set_local(2026, 11, 12, 19, 30)
    await svc.attendance.redeem(1, "світанок")
    clock.set_local(2026, 11, 13, 9, 0)
    assert (await svc.attendance.redeem(1, "місяць")).status is RedeemStatus.ALREADY
    assert (await energy_of(sm, pet.id))[0] == 30


async def test_code_lockout_after_failures(svc, clock) -> None:
    await register(svc, 1, "fox", "Фелікс")
    await make_class(svc, clock)
    clock.set_local(2026, 11, 12, 19, 30)
    for _ in range(5):
        assert (await svc.attendance.redeem(1, "неправильно")).status is RedeemStatus.INVALID
    locked = await svc.attendance.redeem(1, "світанок")
    assert locked.status is RedeemStatus.LOCKED and locked.retry_in_minutes >= 1
    clock.advance(minutes=11)
    assert (await svc.attendance.redeem(1, "світанок")).status is RedeemStatus.OK


async def test_admin_mark(svc, sm, clock) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    session = await make_class(svc, clock)
    res = await svc.attendance.admin_mark(ADMIN_TG, session.id, pet.id, "live")
    assert res.status is RedeemStatus.OK
    again = await svc.attendance.admin_mark(ADMIN_TG, session.id, pet.id, "recording")
    assert again.status is RedeemStatus.ALREADY
    assert (await energy_of(sm, pet.id))[0] == 30


async def test_announcements_and_thanks_once(svc, clock) -> None:
    await register(svc, 1, "fox", "Фелікс")
    session = await make_class(svc, clock)
    clock.set_local(2026, 11, 10, 19, 0)
    assert await svc.attendance.due_announcements() == []
    clock.set_local(2026, 11, 11, 19, 30)
    first = await svc.attendance.due_announcements()
    assert [(a.session.id, a.hours) for a in first] == [(session.id, 24)]
    assert await svc.attendance.due_announcements() == []
    clock.set_local(2026, 11, 12, 18, 10)
    assert [a.hours for a in await svc.attendance.due_announcements()] == [1]

    clock.set_local(2026, 11, 12, 19, 30)
    await svc.attendance.redeem(1, "світанок")
    assert await svc.attendance.due_thanks() == []  # before 21:00
    clock.set_local(2026, 11, 12, 21, 5)
    thanks = await svc.attendance.due_thanks()
    assert len(thanks) == 1 and thanks[0].live == 1
    assert await svc.attendance.due_thanks() == []


async def test_late_created_class_gets_only_urgent_announcement(svc, clock) -> None:
    clock.set_local(2026, 11, 12, 18, 30)
    await make_class(svc, clock)
    assert [a.hours for a in await svc.attendance.due_announcements()] == [1]
    assert await svc.attendance.due_announcements() == []


# ------------------------------------------------------------------- visits


async def test_visit_rewards_host_and_guest(svc, sm, clock) -> None:
    guest = await register(svc, 1, "fox", "Фелікс")
    host = await register(svc, 2, "owl", "Сова")
    res = await svc.visits.visit(1, host.id)
    assert res.status is VisitStatus.VISITED and res.affirmation_html is None
    assert (await energy_of(sm, guest.id))[0] == 3 and (await energy_of(sm, host.id))[0] == 20
    assert (await svc.visits.visit(1, host.id)).status is VisitStatus.ALREADY_TODAY
    clock.set_local(2026, 11, 10, 9, 0)
    assert (await svc.visits.visit(1, host.id)).status is VisitStatus.VISITED


async def test_host_away_means_nobody_home_and_attempt_kept(svc, sm, clock) -> None:
    a = await register(svc, 1, "fox", "Фелікс")
    b = await register(svc, 2, "owl", "Сова")
    c = await register(svc, 3, "bear", "Тедді")
    clock.set_local(2026, 11, 9, 10, 0)
    assert (await svc.visits.visit(2, c.id)).status is VisitStatus.VISITED  # Сова went to Тедді
    clock.advance(minutes=30)
    away = await svc.visits.visit(1, b.id)
    assert away.status is VisitStatus.NOBODY_HOME
    assert (await energy_of(sm, a.id))[0] == 0 and (await energy_of(sm, b.id))[0] == 3
    assert (await svc.visits.visit(1, c.id)).status is VisitStatus.VISITED  # the day's visit was not used
    clock.set_local(2026, 11, 10, 10, 0)
    assert (await svc.visits.visit(1, b.id)).status is VisitStatus.VISITED


async def test_host_back_after_away_minutes(svc, clock) -> None:
    await register(svc, 1, "fox", "Фелікс")
    b = await register(svc, 2, "owl", "Сова")
    c = await register(svc, 3, "bear", "Тедді")
    clock.set_local(2026, 11, 9, 10, 0)
    await svc.visits.visit(2, c.id)
    clock.advance(minutes=61)
    assert (await svc.visits.visit(1, b.id)).status is VisitStatus.VISITED


async def test_mutual_visits_at_the_same_time(svc, sm) -> None:
    a = await register(svc, 1, "fox", "Фелікс")
    b = await register(svc, 2, "owl", "Сова")
    results = await asyncio.gather(svc.visits.visit(1, b.id), svc.visits.visit(2, a.id))
    statuses = sorted(r.status for r in results)
    if is_postgres():
        assert statuses == sorted([VisitStatus.VISITED, VisitStatus.NOBODY_HOME])
    else:  # SQLite serialises writers; either order is valid but never a double payout
        assert VisitStatus.VISITED in statuses
    total = (await energy_of(sm, a.id))[0] + (await energy_of(sm, b.id))[0]
    assert total in (23, 46)


async def test_oracle_gives_unique_affirmations(svc, affirmations, clock) -> None:
    await register(svc, ORACLE_TG, "unicorn", "Світанка")
    oracle = await svc.pets.profile(ORACLE_TG)
    await register(svc, 1, "fox", "Фелікс")
    seen = []
    for day in range(len(affirmations)):
        clock.set_local(2026, 11, 9 + day, 10, 0)
        res = await svc.visits.visit(1, oracle.pet.id)
        assert res.status is VisitStatus.VISITED and res.affirmation_html
        seen.append(res.affirmation_html)
    assert len(set(seen)) == len(affirmations)


async def test_sleeping_host_gets_gift(svc, clock) -> None:
    await register(svc, 1, "fox", "Фелікс")
    host = await register(svc, 2, "owl", "Сова")
    clock.advance(days=5)
    res = await svc.visits.visit(1, host.id)
    assert res.status is VisitStatus.VISITED and res.host_sleeping


async def test_cannot_visit_self(svc) -> None:
    pet = await register(svc, 1, "fox", "Фелікс")
    assert (await svc.visits.visit(1, pet.id)).status is VisitStatus.SELF
    assert (await svc.visits.visit(5, pet.id)).status is VisitStatus.NOT_REGISTERED


# ------------------------------------------------------- community and jobs


async def test_world_and_stats(svc, clock) -> None:
    await register(svc, 1, "fox", "Фелікс")
    owl = await register(svc, 2, "owl", "Сова")
    clock.set_local(2026, 11, 9, 8, 0)
    await svc.morning.greet(1)
    await svc.visits.visit(1, owl.id)
    world = await svc.community.world()
    assert world.registered == 2 and world.greetings_today == 1 and world.last_class is None
    stats = await svc.community.stats(1)
    assert stats.mornings_total == 1 and stats.visits_made == 1 and stats.energy_month == 13


async def test_job_claim_once_and_retry_after_failure(sm, clock) -> None:
    async with sm() as s, s.begin():
        assert await Jobs.claim(s, "morning_card", "2026-11-09", clock.now())
    async with sm() as s, s.begin():
        assert not await Jobs.claim(s, "morning_card", "2026-11-09", clock.now())
        await Jobs.finish(s, "morning_card", "2026-11-09", clock.now(), error="boom")
    async with sm() as s, s.begin():
        assert await Jobs.claim(s, "morning_card", "2026-11-09", clock.now())
        await Jobs.finish(s, "morning_card", "2026-11-09", clock.now())
    async with sm() as s:
        assert (await s.get(JobRun, ("morning_card", "2026-11-09"))).status == "done"
