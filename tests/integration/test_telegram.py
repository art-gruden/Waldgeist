"""Telegram workflows end to end: real dispatcher, routers, middlewares and database,
with the Telegram Bot API replaced by a recording mock session."""

from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import (
    AnswerCallbackQuery,
    GetChatMember,
    GetMe,
    SendMessage,
    SendPhoto,
    SetMessageReaction,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    FSInputFile,
    Message,
    PhotoSize,
    Update,
    User,
)

from enchanted_companions.app import build_dispatcher
from enchanted_companions.container import build_container
from enchanted_companions.db.repositories import Pets
from enchanted_companions.scheduler import jobs
from enchanted_companions.settings import Settings
from tests.integration.helpers import ADMIN_TG, ORACLE_TG

GROUP = -1001234567890
BOT_ID = 4242
_ids = itertools.count(1000)

# a valid 1x1 JPEG
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a"
    "1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc400"
    "1f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01"
    "020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a343536"
    "3738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798"
    "999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3"
    "f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.non_members: set[int] = set()
        self.blocked: set[int] = set()

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):  # noqa: ASYNC109
        self.calls.append(method)
        if isinstance(method, SendMessage | SendPhoto):
            if method.chat_id in self.blocked:
                raise TelegramForbiddenError(method=method, message="Forbidden: bot can't initiate conversation")
            chat = Chat(id=method.chat_id, type="private" if int(method.chat_id) > 0 else "supergroup")
            mid = next(_ids)
            if isinstance(method, SendPhoto):
                return Message(
                    message_id=mid,
                    date=datetime.now(UTC),
                    chat=chat,
                    caption=method.caption,
                    photo=[PhotoSize(file_id=f"file-{mid}", file_unique_id=f"u-{mid}", width=1, height=1)],
                )
            return Message(message_id=mid, date=datetime.now(UTC), chat=chat, text=method.text)
        if isinstance(method, GetChatMember):
            user = User(id=method.user_id, is_bot=False, first_name="x")
            if method.user_id in self.non_members:
                return ChatMemberLeft(user=user)
            return ChatMemberMember(user=user)
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Forest", username="forest_bot")
        return True

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs) -> AsyncGenerator[bytes, None]:  # pragma: no cover
        raise NotImplementedError
        yield b""

    # -------------------------------------------------------------- helpers

    def sent(self, chat_id: int | None = None) -> list[SendMessage | SendPhoto]:
        return [
            c
            for c in self.calls
            if isinstance(c, SendMessage | SendPhoto) and (chat_id is None or int(c.chat_id) == chat_id)
        ]

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [(c.text if isinstance(c, SendMessage) else c.caption) or "" for c in self.sent(chat_id)]

    def of(self, kind: type) -> list:
        return [c for c in self.calls if isinstance(c, kind)]


def user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name=f"U{uid}", username=f"user{uid}")


def message_update(uid: int, text: str, chat_id: int | None = None) -> Update:
    chat = Chat(id=chat_id or uid, type="private" if (chat_id or uid) > 0 else "supergroup")
    return Update(
        update_id=next(_ids),
        message=Message(message_id=next(_ids), date=datetime.now(UTC), chat=chat, from_user=user(uid), text=text),
    )


def callback_update(uid: int, data: str, chat_id: int | None = None) -> Update:
    chat = Chat(id=chat_id or uid, type="private" if (chat_id or uid) > 0 else "supergroup")
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Forest")
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)),
            from_user=user(uid),
            chat_instance="ci",
            data=data,
            message=Message(message_id=next(_ids), date=datetime.now(UTC), chat=chat, from_user=bot_user, text="…"),
        ),
    )


@pytest.fixture
def assets(tmp_path: Path) -> Path:
    root = tmp_path / "images"
    for rel in ("fox/stage-1.jpg", "fox/sleep.jpg", "locations/heart.jpg"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(TINY_JPEG)
    return root


@pytest.fixture
async def tg(sm, clock, cfg, content, affirmations, assets):
    settings = Settings(
        bot_token="123456:TEST-token-not-real-0000000000000000",
        database_url="sqlite+aiosqlite://",
        group_chat_id=GROUP,
        admin_tg_ids=[ADMIN_TG],
        assets_dir=assets,
        log_dir=assets / "logs",
    )
    session = MockSession()
    bot = Bot(settings.bot_token.get_secret_value(), session=session, default=DefaultBotProperties(parse_mode="HTML"))
    c = build_container(settings, cfg, content, affirmations, sm, bot, clock=clock)
    c.bot_username = "forest_bot"
    dp = build_dispatcher(c, throttle_interval=0.0)

    async def feed(update: Update) -> None:
        await dp.feed_update(bot, update)

    yield c, session, feed


async def register_via_bot(feed, uid: int, slug: str, name: str) -> None:
    await feed(message_update(uid, "/start"))
    await feed(callback_update(uid, f"sp:{slug}"))
    await feed(message_update(uid, name))
    await feed(callback_update(uid, "rg:skip_birthday"))


# ------------------------------------------------------------------- tests


async def test_registration_flow(tg) -> None:
    c, session, feed = tg
    await feed(message_update(1, "/start"))
    picker = session.of(SendMessage)[-1]
    assert "Обери свого компаньйона" in picker.text and len(picker.reply_markup.inline_keyboard) == 8

    await feed(callback_update(1, "sp:fox"))
    assert "Як назвеш" in session.texts(1)[-1]
    await feed(message_update(1, "Я"))  # too short
    assert "від 2 до 24" in session.texts(1)[-1]
    await feed(message_update(1, "Фелікс"))
    await feed(callback_update(1, "rg:skip_birthday"))

    async with c.sm() as s:
        pet = await Pets.by_owner_tg(s, 1)
    assert pet and pet.name == "Фелікс" and pet.species_id == "fox"
    photos = session.of(SendPhoto)
    assert photos and isinstance(photos[-1].photo, FSInputFile) and "Фелікс" in photos[-1].caption
    assert any("Фелікс" in t for t in session.texts(GROUP))  # welcome in the group

    await feed(message_update(1, "/pet"))  # second time: cached file_id, no upload
    assert isinstance(session.of(SendPhoto)[-1].photo, str)  # Telegram file_id from media_cache


async def test_taken_species_and_non_member(tg) -> None:
    c, session, feed = tg
    await register_via_bot(feed, 1, "fox", "Фелікс")
    await feed(message_update(2, "/start"))
    await feed(callback_update(2, "sp:fox"))
    alert = session.of(AnswerCallbackQuery)[-1]
    assert alert.show_alert and "вже має друга" in alert.text

    session.non_members.add(3)
    await feed(message_update(3, "/start"))
    assert "лише для учасників" in session.texts(3)[-1]


async def test_bad_callback_data_is_ignored(tg) -> None:
    c, session, feed = tg
    await feed(callback_update(1, "v:not-a-number"))
    await feed(callback_update(1, "garbage"))
    assert session.of(AnswerCallbackQuery)  # answered quietly, nothing else
    assert not session.sent()


async def test_morning_message_counted_once_with_reaction(tg) -> None:
    c, session, feed = tg
    await register_via_bot(feed, 1, "fox", "Фелікс")
    c.clock.set_local(2026, 11, 9, 7, 30)
    await feed(message_update(1, "Доброго ранку всім! ☀️", chat_id=GROUP))
    await feed(message_update(1, "доброго ранку ще раз", chat_id=GROUP))
    await feed(message_update(1, "Що сьогодні задали?", chat_id=GROUP))
    reactions = session.of(SetMessageReaction)
    assert len(reactions) == 1 and reactions[0].reaction[0].emoji == c.cfg.morning.counted_reaction
    async with c.sm() as s:
        assert (await Pets.by_owner_tg(s, 1)).energy_total == 10


async def test_unregistered_greeting_gets_one_invite_or_reaction(tg) -> None:
    c, session, feed = tg
    c.clock.set_local(2026, 11, 9, 8, 0)
    await feed(message_update(5, "Good morning!", chat_id=GROUP))
    await feed(message_update(5, "Good morning!", chat_id=GROUP))
    invites = [t for t in session.texts(5) if "компаньйон" in t]
    assert len(invites) == 1

    session.blocked.add(6)
    await feed(message_update(6, "Доброе утро", chat_id=GROUP))
    assert session.of(SetMessageReaction)[-1].reaction[0].emoji == c.cfg.morning.invite_reaction


async def test_morning_card_job_once_and_button(tg) -> None:
    c, session, feed = tg
    await register_via_bot(feed, 1, "fox", "Фелікс")
    c.clock.set_local(2026, 11, 9, 7, 0)
    await jobs.morning_card(c)
    await jobs.morning_card(c)
    cards = [p for p in session.of(SendPhoto) if int(p.chat_id) == GROUP]
    assert len(cards) == 1 and cards[0].reply_markup.inline_keyboard[0][0].callback_data == "mh:1"

    await feed(callback_update(1, "mh:1", chat_id=GROUP))
    toast = session.of(AnswerCallbackQuery)[-1]
    assert "+10" in toast.text
    await feed(callback_update(1, "mh:1", chat_id=GROUP))
    assert "вже привітався" in session.of(AnswerCallbackQuery)[-1].text

    c.clock.set_local(2026, 11, 9, 12, 30)
    await jobs.morning_summary(c)
    await jobs.morning_summary(c)
    summaries = [t for t in session.texts(GROUP) if "1 компаньйон" in t or "компаньйонів" in t]
    assert len(summaries) == 1


async def test_card_not_posted_after_window(tg) -> None:
    c, session, feed = tg
    c.clock.set_local(2026, 11, 9, 13, 0)
    await jobs.morning_card(c)
    assert not session.of(SendPhoto)


async def test_admin_creates_class_and_member_redeems_code(tg) -> None:
    c, session, feed = tg
    await register_via_bot(feed, 1, "fox", "Фелікс")
    c.clock.set_local(2026, 11, 9, 10, 0)

    await feed(message_update(1, "/new_class"))  # not an admin: no wizard
    assert "Дата заняття" not in session.texts(1)[-1]

    for text in ("/new_class", "12.11", "19:00", "Карма і вибір", "Світанок"):
        await feed(message_update(ADMIN_TG, text))
    assert "Створити заняття?" in session.texts(ADMIN_TG)[-1]
    await feed(callback_update(ADMIN_TG, "nc:1"))
    assert "створено" in session.texts(ADMIN_TG)[-1]

    c.clock.set_local(2026, 11, 11, 19, 5)
    await jobs.classes_tick(c)
    assert any("Завтра" in t for t in session.texts(GROUP))

    c.clock.set_local(2026, 11, 12, 19, 45)
    await feed(message_update(1, "/code Світанок"))
    assert "+30" in session.texts(1)[-1]
    await feed(message_update(1, "/code світанок"))
    assert "вже зараховано" in session.texts(1)[-1]

    c.clock.set_local(2026, 11, 12, 21, 0)
    await jobs.classes_tick(c)
    assert any("прийшло 1" in t for t in session.texts(GROUP))


async def test_admin_grant_and_chatid(tg) -> None:
    c, session, feed = tg
    await register_via_bot(feed, 1, "fox", "Фелікс")
    await feed(message_update(ADMIN_TG, "/grant @user1 160 подарунок за допомогу"))
    assert "160" in session.texts(ADMIN_TG)[-1]
    assert any("рівня 2" in t for t in session.texts(1))  # level-up congratulation
    await feed(message_update(ADMIN_TG, "/chatid", chat_id=GROUP))
    assert str(GROUP) in session.texts(GROUP)[-1]
    await feed(message_update(1, "/chatid", chat_id=GROUP))
    assert len([t for t in session.texts(GROUP) if "ID цього чату" in t]) == 1


async def test_visit_oracle_and_nobody_home(tg) -> None:
    c, session, feed = tg
    await register_via_bot(feed, ORACLE_TG, "unicorn", "Світанка")
    await register_via_bot(feed, 1, "fox", "Фелікс")
    await register_via_bot(feed, 2, "owl", "Сова")
    async with c.sm() as s:
        oracle = await Pets.by_owner_tg(s, ORACLE_TG)
        owl = await Pets.by_owner_tg(s, 2)
    c.clock.set_local(2026, 11, 9, 10, 0)

    await feed(message_update(1, "/visit"))
    keyboard = session.of(SendMessage)[-1].reply_markup.inline_keyboard
    labels = [b.text for row in keyboard for b in row]
    assert any("✨" in label and "Світанка" in label for label in labels) and not any("Фелікс" in x for x in labels)

    await feed(callback_update(1, f"v:{oracle.id}"))
    guest_texts = session.texts(1)
    assert any("Світанка" in t for t in guest_texts)
    assert any(("Закон" in t) or ("Афірмація" in t) for t in guest_texts[-2:])
    assert any("+20" in t for t in session.texts(ORACLE_TG))

    # Фелікс is away now: Сова finds nobody at home
    await feed(callback_update(2, f"v:{oracle.id}"))  # Сова visits the oracle first
    c.clock.advance(minutes=5)
    await feed(message_update(2, "/visit"))
    assert "вже ходив" in session.texts(2)[-1]
    async with c.sm() as s:
        fox = await Pets.by_owner_tg(s, 1)
    c.clock.set_local(2026, 11, 10, 10, 0)
    await feed(callback_update(1, f"v:{owl.id}"))
    c.clock.advance(minutes=10)
    await feed(callback_update(2, f"v:{fox.id}"))
    assert "Вдома нікого не було" in session.texts(2)[-1]


async def test_sleeping_pet_wakes_on_interaction(tg) -> None:
    c, session, feed = tg
    await register_via_bot(feed, 1, "fox", "Фелікс")
    c.clock.advance(days=9)
    await feed(message_update(1, "/pet"))
    texts = session.texts(1)
    assert any("З поверненням" in t for t in texts[-2:])
    await feed(message_update(1, "/pet"))
    assert not any("З поверненням" in t for t in session.texts(1)[-1:])


async def test_world_stats_help(tg) -> None:
    c, session, feed = tg
    await register_via_bot(feed, 1, "fox", "Фелікс")
    await feed(message_update(1, "/world"))
    assert "Мешканців: <b>1</b>" in session.texts(1)[-1]
    await feed(message_update(1, "/stats"))
    assert "Ранків" in session.texts(1)[-1]
    await feed(message_update(1, "/help"))
    assert "+20" in session.texts(1)[-1] and "+30" in session.texts(1)[-1]
    await feed(message_update(1, "щось незрозуміле"))
    assert "зрозумів" in session.texts(1)[-1] or "відповіді" in session.texts(1)[-1]


async def test_scheduler_has_all_jobs(tg) -> None:
    from enchanted_companions.scheduler.setup import build_scheduler

    c, _, _ = tg
    scheduler = build_scheduler(c)
    assert {j.id for j in scheduler.get_jobs()} == {
        "morning_card",
        "morning_summary",
        "sleep_nightly",
        "classes_tick",
        "heartbeat",
    }


async def test_error_in_handler_is_reported_softly(tg, monkeypatch) -> None:
    c, session, feed = tg
    await register_via_bot(feed, 1, "fox", "Фелікс")

    async def boom(*args, **kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(c.community, "stats", boom)
    await feed(message_update(1, "/stats"))
    assert "заплуталося" in session.texts(1)[-1]


async def test_catch_up_after_restart(tg) -> None:
    c, session, feed = tg
    c.clock.set_local(2026, 11, 9, 9, 15)  # restarted mid-morning: postcard is still useful
    await jobs.catch_up(c)
    assert len([p for p in session.of(SendPhoto) if int(p.chat_id) == GROUP]) == 1
    c.clock.set_local(2026, 11, 9, 20, 0)  # evening: no late morning summary
    await jobs.catch_up(c)
    assert not [t for t in session.texts(GROUP) if "прокинулося" in t or "неспішно" in t]
