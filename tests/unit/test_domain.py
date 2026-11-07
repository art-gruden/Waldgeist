"""Unit tests for pure game rules."""

from __future__ import annotations

import random
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from enchanted_companions.domain.affirmations import AffirmationCatalog
from enchanted_companions.domain.clock import GameClock, iso_week_key
from enchanted_companions.domain.greeting import GreetingDetector
from enchanted_companions.domain.progression import level_for, progress, stage_for
from enchanted_companions.domain.rewards import class_key, morning_key, visit_keys
from enchanted_companions.domain.sleep import SleepState, sleep_state_for
from enchanted_companions.logging_setup import mask_secrets
from enchanted_companions.services.attendance import normalize_code
from enchanted_companions.services.messenger import progress_bar
from enchanted_companions.services.registration import (
    InvalidBirthday,
    InvalidName,
    clean_name,
    parse_birthday,
)

THRESHOLDS = [0, 150, 400, 750, 1200, 1800, 2500, 3300, 4200, 5200]
STAGES = [1, 4, 7, 10]


@pytest.mark.parametrize(
    ("energy", "level"),
    [(0, 1), (149, 1), (150, 2), (399, 2), (400, 3), (750, 4), (5199, 9), (5200, 10), (99999, 10), (-20, 1)],
)
def test_level_for(energy: int, level: int) -> None:
    assert level_for(energy, THRESHOLDS) == level


@pytest.mark.parametrize(("level", "stage"), [(1, 1), (3, 1), (4, 2), (6, 2), (7, 3), (9, 3), (10, 4)])
def test_stage_for(level: int, stage: int) -> None:
    assert stage_for(level, STAGES) == stage


def test_progress_keeps_level_after_correction() -> None:
    p = progress(energy=100, kept_level=3, thresholds=THRESHOLDS, stage_starts=STAGES)
    assert p.level == 3 and p.next_threshold == 750 and p.to_next == 650


def test_progress_max_level() -> None:
    p = progress(6000, 10, THRESHOLDS, STAGES)
    assert p.is_max and p.to_next is None and p.stage == 4
    assert progress_bar(p, THRESHOLDS) == "▰" * 10


def test_progress_bar_half() -> None:
    p = progress(275, 2, THRESHOLDS, STAGES)  # halfway between 150 and 400
    assert progress_bar(p, THRESHOLDS) == "▰" * 5 + "▱" * 5


@pytest.mark.parametrize(
    ("days", "state"),
    [
        (0, SleepState.AWAKE),
        (1, SleepState.AWAKE),
        (2, SleepState.DOZING),
        (3, SleepState.DOZING),
        (4, SleepState.SLEEPING),
        (7, SleepState.SLEEPING),
        (8, SleepState.DREAMING),
        (90, SleepState.DREAMING),
    ],
)
def test_sleep_states_follow_gdd(days: int, state: SleepState) -> None:
    assert sleep_state_for(days, 2, 4, 8) is state


class TestClock:
    def clock(self, now: datetime, cfg) -> GameClock:
        return GameClock(cfg.tz, now=lambda: now)

    def test_game_date_uses_kyiv_not_utc(self, cfg) -> None:
        # 23:30 UTC on Nov 8 is already 01:30 on Nov 9 in Kyiv (UTC+2)
        clk = self.clock(datetime(2026, 11, 8, 23, 30, tzinfo=UTC), cfg)
        assert clk.game_date() == date(2026, 11, 9)

    def test_morning_window_bounds(self, cfg) -> None:
        start, end = cfg.morning.window_start, cfg.morning.window_end
        for hhmm, inside in [((4, 59), False), ((5, 0), True), ((11, 59), True), ((12, 0), False)]:
            moment = datetime(2026, 11, 9, *hhmm, tzinfo=cfg.tz)
            assert GameClock(cfg.tz).in_window(start, end, moment) is inside

    def test_dst_spring_day_is_23_hours(self, cfg) -> None:
        clk = GameClock(cfg.tz)
        start, end = clk.day_bounds(date(2026, 3, 29))  # Europe/Kyiv switches to summer time
        assert end - start == timedelta(hours=23)

    def test_dst_autumn_day_is_25_hours(self, cfg) -> None:
        clk = GameClock(cfg.tz)
        start, end = clk.day_bounds(date(2026, 10, 25))
        assert end - start == timedelta(hours=25)

    def test_at_converts_local_time(self, cfg) -> None:
        clk = GameClock(cfg.tz)
        assert clk.at(date(2026, 7, 1), time(7, 0)) == datetime(2026, 7, 1, 4, 0, tzinfo=UTC)  # UTC+3 in summer
        assert clk.at(date(2026, 12, 1), time(7, 0)) == datetime(2026, 12, 1, 5, 0, tzinfo=UTC)  # UTC+2 in winter

    def test_naive_now_rejected(self, cfg) -> None:
        with pytest.raises(ValueError):
            GameClock(cfg.tz, now=lambda: datetime(2026, 1, 1)).now()

    def test_iso_week(self) -> None:
        assert iso_week_key(date(2026, 11, 9)) == "2026-W46"


class TestGreeting:
    @pytest.fixture
    def detector(self, cfg) -> GreetingDetector:
        return GreetingDetector(cfg.morning.greeting_patterns)

    @pytest.mark.parametrize(
        "text",
        [
            "Доброго ранку!",
            "доброго ранку всім ☀️",
            "Добрий ранок",
            "З добрим ранком, друзі",
            "Доброе утро",
            "доброе утро!!",
            "С добрым утром",
            "Good morning!",
            "good  morning everyone",
            "Morning!",
            "Guten Morgen",
            "ДОБРОГО РАНКУ",
            "Доброго ранку, Омніє 💛",
        ],
    )
    def test_greetings_detected(self, detector: GreetingDetector, text: str) -> None:
        assert detector.is_greeting(text)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            None,
            "Привіт",
            "Як справи?",
            "Завтра зранку буде заняття",
            "Good evening",
            "Посилання на запис",
            "утро вечера мудренее",
        ],
    )
    def test_other_messages_ignored(self, detector: GreetingDetector, text: str | None) -> None:
        assert not detector.is_greeting(text)


class TestRegistrationValidation:
    @pytest.mark.parametrize("raw", ["Фелікс", "  Лапка  ", "Зоряна-Ніч", "Мар'яна", "Мар’яна", "Rex 2"])
    def test_valid_names(self, raw: str) -> None:
        assert clean_name(raw, 2, 24)

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("Я", "length"),
            ("x" * 25, "length"),
            ("http://spam", "link"),
            ("@name", "link"),
            ("Лис🦊", "chars"),
            ("_hidden", "chars"),
            ("a/b", "link"),
        ],
    )
    def test_invalid_names(self, raw: str, reason: str) -> None:
        with pytest.raises(InvalidName) as exc:
            clean_name(raw, 2, 24)
        assert exc.value.args[0] == reason

    def test_name_whitespace_collapsed(self) -> None:
        assert clean_name("Зоряна    Ніч", 2, 24) == "Зоряна Ніч"

    @pytest.mark.parametrize(("raw", "expected"), [("14.03", (14, 3)), ("1/1", (1, 1)), ("29-02", (29, 2))])
    def test_birthday(self, raw: str, expected: tuple[int, int]) -> None:
        assert parse_birthday(raw) == expected

    @pytest.mark.parametrize("raw", ["31.04", "00.01", "14.13", "березень", "1990-03-14"])
    def test_bad_birthday(self, raw: str) -> None:
        with pytest.raises(InvalidBirthday):
            parse_birthday(raw)


@pytest.mark.parametrize(
    ("raw", "code"),
    [("Світанок", "світанок"), ("  «СВІТАНОК»! ", "світанок"), ("зелёный  лист", "зеленый лист"), ("", "")],
)
def test_normalize_code(raw: str, code: str) -> None:
    assert normalize_code(raw) == code


def test_idempotency_keys_are_stable() -> None:
    day = date(2026, 11, 9)
    assert morning_key(7, day) == "morning:7:2026-11-09"
    assert class_key(3, 7) == "class:3:7"  # the same key for live and recording
    guest, host = visit_keys(7, day)
    assert guest != host and guest.startswith("visit:7:2026-11-09")


class TestAffirmations:
    def test_pick_never_repeats_until_exhausted(self, affirmations: AffirmationCatalog) -> None:
        rng = random.Random(3)
        seen: list[str] = []
        for _ in range(len(affirmations)):
            entry = affirmations.pick(seen, rng)
            assert entry and entry.id not in seen
            seen.append(entry.id)
        assert affirmations.pick(seen, rng) is not None  # new round

    def test_render_law_with_optional_blocks(self, affirmations: AffirmationCatalog) -> None:
        taxi = affirmations.render(affirmations.get("law-029"))
        assert "Закон таксі" in taxi and "Нехай буде на вище благо" in taxi and "У житті" not in taxi
        void = affirmations.render(affirmations.get("law-020"))
        assert "У житті" in void and "💬" not in void

    def test_render_escapes_text(self, affirmations: AffirmationCatalog) -> None:
        html_text = affirmations.render(affirmations.get("aff-001"))
        assert "&lt;завжди&gt;" in html_text and "{closing}" not in html_text

    def test_missing_file_gives_empty_catalog(self, tmp_path: Path) -> None:
        catalog = AffirmationCatalog.load(tmp_path / "nope.yaml")
        assert len(catalog) == 0 and catalog.pick([]) is None


def test_mask_secrets() -> None:
    text = "token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw dsn postgresql+asyncpg://ec:hunter2@db/x"
    masked = mask_secrets(text)
    assert "hunter2" not in masked and "AAHdq" not in masked


def test_all_text_keys_exist(content) -> None:
    """Every literal texts.render("...") key used in the code exists in texts.yaml."""
    src = Path(__file__).resolve().parents[2] / "src"
    keys = set()
    for path in src.rglob("*.py"):
        keys |= set(re.findall(r"render\(\s*\"([a-z_]+(?:\.[a-z0-9_]+)+)\"", path.read_text(encoding="utf-8")))
    dynamic = {f"stages.s{i}" for i in range(1, 5)} | {f"sleep_state.{s.value}" for s in SleepState}
    dynamic |= {f"wake.{s.value}" for s in SleepState if s is not SleepState.AWAKE}
    dynamic |= {f"buttons.{a}" for a in ("pet", "code", "visit", "world", "stats", "settings", "help")}
    dynamic |= {f"commands.{c}" for c in ("pet", "code", "visit", "world", "stats", "settings", "help", "start")}
    dynamic |= {f"register.bad_name_{r}" for r in ("length", "link", "chars")}
    dynamic |= {f"admin.error_{r}" for r in ("amount", "reason", "who", "no_pet", "species", "taken")}
    missing = sorted(k for k in keys | dynamic if not content.texts.has(k))
    assert not missing, missing
    assert len(keys) > 60
