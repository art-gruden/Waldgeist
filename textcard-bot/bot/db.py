"""SQLite: налаштування користувачів, доступ, стан діалогу, зв'язок «картинка → текст»."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL DEFAULT '',
    username   TEXT    NOT NULL DEFAULT '',
    settings   TEXT    NOT NULL DEFAULT '{}',
    state      TEXT,
    state_data TEXT    NOT NULL DEFAULT '{}',
    allowed    INTEGER NOT NULL DEFAULT 0,   -- 1 дозволено, -1 відхилено
    admin      INTEGER NOT NULL DEFAULT 0,
    requested  INTEGER NOT NULL DEFAULT 0,
    created    REAL    NOT NULL,
    seen       REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS renders (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    title      TEXT,             -- NULL: з налаштувань; '' — без заголовка; інакше разовий заголовок
    created    REAL    NOT NULL,
    file_id    TEXT,             -- file_id картинки в Telegram (лише для повідомлення з кнопками картки): для публікації в канал
    kind       TEXT,             -- 'photo' | 'document'
    published_to INTEGER NOT NULL DEFAULT 0, -- id каналу, куди листівку опубліковано востаннє (0 — ще ні)
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS renders_user ON renders(user_id, created);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

RENDERS_PER_USER = 200


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Старі бази (без колонок публікації) доповнюються на місці — дані користувачів зберігаються."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(renders)")}
        for col, ddl in (("file_id", "TEXT"), ("kind", "TEXT"), ("published_to", "INTEGER NOT NULL DEFAULT 0")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE renders ADD COLUMN {col} {ddl}")

    def _x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    # ---------- kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._x("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self._x("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))

    def kv_del(self, key: str) -> None:
        self._x("DELETE FROM kv WHERE key=?", (key,))

    # ---------- users
    def touch_user(self, uid: int, name: str, username: str) -> sqlite3.Row:
        now = time.time()
        with self.lock:
            self._x("INSERT INTO users(id, name, username, created, seen) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, seen=excluded.seen",
                    (uid, name[:100], username[:64], now, now))
            return self.user(uid)

    def user(self, uid: int) -> sqlite3.Row | None:
        return self._x("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def users(self, where: str = "1", args=()) -> list[sqlite3.Row]:
        return self._x(f"SELECT * FROM users WHERE {where} ORDER BY seen DESC", args).fetchall()

    def set_user(self, uid: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), uid))

    def ensure_user(self, uid: int) -> None:
        now = time.time()
        self._x("INSERT OR IGNORE INTO users(id, created, seen) VALUES(?, ?, ?)", (uid, now, now))

    def settings(self, uid: int, defaults: dict) -> dict:
        row = self.user(uid)
        stored = json.loads(row["settings"]) if row else {}
        return {**defaults, **{k: v for k, v in stored.items() if k in defaults}}

    def save_settings(self, uid: int, settings: dict) -> None:
        self._x("UPDATE users SET settings=? WHERE id=?", (json.dumps(settings, ensure_ascii=False), uid))

    def set_state(self, uid: int, state: str | None, data: dict | None = None) -> None:
        self._x("UPDATE users SET state=?, state_data=? WHERE id=?",
                (state, json.dumps(data or {}, ensure_ascii=False), uid))

    def state(self, uid: int) -> tuple[str | None, dict]:
        row = self.user(uid)
        if not row:
            return None, {}
        return row["state"], json.loads(row["state_data"] or "{}")

    # ---------- renders
    def add_render(self, chat_id: int, message_id: int, user_id: int, text: str, title: str | None) -> None:
        with self.lock:
            self._x("INSERT OR REPLACE INTO renders(chat_id, message_id, user_id, text, title, created) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (chat_id, message_id, user_id, text, title, time.time()))
            self._x("DELETE FROM renders WHERE user_id=? AND rowid NOT IN "
                    "(SELECT rowid FROM renders WHERE user_id=? ORDER BY created DESC LIMIT ?)",
                    (user_id, user_id, RENDERS_PER_USER))

    def get_render(self, chat_id: int, message_id: int) -> sqlite3.Row | None:
        return self._x("SELECT * FROM renders WHERE chat_id=? AND message_id=?", (chat_id, message_id)).fetchone()

    def last_render(self, user_id: int) -> sqlite3.Row | None:
        return self._x("SELECT * FROM renders WHERE user_id=? ORDER BY created DESC LIMIT 1", (user_id,)).fetchone()

    # ---------- публікація в канал
    def set_render_file(self, chat_id: int, message_id: int, kind: str, file_id: str) -> None:
        self._x("UPDATE renders SET kind=?, file_id=? WHERE chat_id=? AND message_id=?",
                (kind, file_id, chat_id, message_id))

    def last_publishable(self, user_id: int) -> sqlite3.Row | None:
        """Остання листівка користувача, яку можна опублікувати (є file_id)."""
        return self._x("SELECT * FROM renders WHERE user_id=? AND file_id IS NOT NULL "
                       "ORDER BY created DESC, rowid DESC LIMIT 1", (user_id,)).fetchone()

    def mark_published(self, chat_id: int, message_id: int, channel_id: int) -> None:
        self._x("UPDATE renders SET published_to=? WHERE chat_id=? AND message_id=?", (channel_id, chat_id, message_id))
