"""Telegram-бот: текст → листівка. Усі налаштування через меню бота.

Запуск: BOT_TOKEN=... python -m bot.main
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import re
import time
from pathlib import Path

from telegram import (BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup, InputFile, InputMediaPhoto,
                      LinkPreviewOptions, Message, MessageOriginChannel, Update)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from textcard import render as R
from textcard.store import TemplateStore

from .db import DB

log = logging.getLogger("textcard-bot")

# ------------------------------------------------------------------ конфігурація (лише старт; решта — у меню)

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
ENV_ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}
DEFAULT_TITLE = os.environ.get("DEFAULT_TITLE", "ЕНЕРГОЗАВАНТАЖЕННЯ")
DEFAULT_SIZE = os.environ.get("DEFAULT_SIZE", "1280x782")
DEFAULT_TEMPLATE = os.environ.get("DEFAULT_TEMPLATE", "pink_glow")
MAX_WORDS = int(os.environ.get("MAX_WORDS", "40"))
MAX_CHARS = int(os.environ.get("MAX_CHARS", "400"))
MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", "20"))  # Bot API віддає ботам файли до 20 МБ
RENDER_CONCURRENCY = int(os.environ.get("RENDER_CONCURRENCY", "2"))

SIZES = [
    ("1280x782", "1280×782"),
    ("1280x720", "16:9 · 1280×720"),
    ("1600x978", "Більший · 1600×978"),
    ("1080x1080", "Квадрат · 1080×1080"),
    ("1080x1350", "Пост 4:5 · 1080×1350"),
    ("1080x1920", "Сторіс 9:16 · 1080×1920"),
]

# кольори тексту: (назва, текст, акцент, заголовок, світіння)
COLORS = {
    "": ("як у шаблоні", None, None, None, None),
    "white": ("білий", "#ffffff", "#ffd27a", "#ffffff", "#1a0a24"),
    "cherry": ("вишневий", "#7a0f40", "#4a1670", ["#a3124f", "#6c0a35"], "#ffffff"),
    "violet": ("фіолетовий", "#47267a", "#b0306e", "#5b2d8c", "#ffffff"),
    "navy": ("темно-синій", "#10294f", "#b0306e", "#0d2a5c", "#ffffff"),
    "black": ("чорний", "#1a1a1a", "#8a2f1f", "#1a1a1a", "#ffffff"),
    "gold": ("золотий", ["#fff4d0", "#e6b65a"], "#ffffff", ["#fff3cf", "#d9a441"], "#2a1500"),
}

# обробка фону для нових шаблонів
OVERLAYS = {
    "none": ("Без змін", None),
    "dark": ("Затемнити фон", {"color": "#000000", "opacity": 0.38}),
    "light": ("Освітлити фон", {"color": "#ffffff", "opacity": 0.42}),
    "dpanel": ("Темна панель під текстом", {"panel": {"color": "#000000a6", "padding": 0.035, "radius": 0.03}}),
    "lpanel": ("Світла панель під текстом", {"panel": {"color": "#ffffffc0", "padding": 0.035, "radius": 0.03}}),
}

SEND_AS = {"photo": "фото", "file": "файл", "both": "фото + файл"}
SAMPLE_TEXT = "Я відкриваю своє серце світлу. Нехай *мудрість* наповнює мої думки, слова та вчинки."
PAGE = 10

HELP = (
    "Надішліть текст (10–20 слів) — я зроблю з нього листівку.\n\n"
    "• <code>*слово*</code> — виділити кольором\n"
    "• перший рядок, що починається з <code>#</code>, — заголовок лише для цієї листівки "
    "(<code>#</code> без тексту — без заголовка)\n"
    "• нові рядки зберігаються\n"
    "• фото, надіслане боту, — новий шаблон (фон)\n"
    "• кнопка «📢 Опублікувати» під листівкою надсилає її у ваш Telegram-канал\n\n"
    "/menu — налаштування: шаблон, заголовок, підпис, розмір, шрифт, колір, як надсилати\n"
    "/templates — вибрати шаблон\n"
    "/channel — підключити канал для публікацій\n"
    "/title <i>текст</i> — змінити заголовок\n"
    "/cancel — скасувати введення\n"
    "/id — ваш Telegram ID"
)


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(t, callback_data=d) for t, d in row] for row in rows if row])


class CardBot:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self.db = DB(data_dir / "bot.sqlite3")
        self.store = TemplateStore(data_dir)
        self.tmp = data_dir / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.sem = asyncio.Semaphore(RENDER_CONCURRENCY)
        self.preview_cache: dict[tuple, bytes] = {}
        for a in ENV_ADMINS:
            self.db.ensure_user(a)
            self.db.set_user(a, admin=1, allowed=1)

    # ------------------------------------------------------------ налаштування й доступ

    def defaults(self) -> dict:
        base = {"template": DEFAULT_TEMPLATE, "title": DEFAULT_TITLE, "title_on": True, "footer": "",
                "size": DEFAULT_SIZE, "format": "png", "send_as": "photo", "font": "", "title_font": "",
                "color": "", "glow": True}
        saved = self.db.kv_get("defaults", {})
        return {**base, **{k: v for k, v in saved.items() if k in base}}

    def st(self, uid: int) -> dict:
        return self.db.settings(uid, self.defaults())

    def set_st(self, uid: int, **kw) -> dict:
        s = self.st(uid)
        s.update(kw)
        self.db.save_settings(uid, s)
        return s

    def is_admin(self, uid: int) -> bool:
        row = self.db.user(uid)
        return uid in ENV_ADMINS or bool(row and row["admin"])

    def access_mode(self) -> str:
        return self.db.kv_get("access", "all")         # all | allowed

    def add_mode(self) -> str:
        return self.db.kv_get("add_templates", "admins")  # admins | all

    def can_add(self, uid: int) -> bool:
        return self.is_admin(uid) or self.add_mode() == "all"

    def can_delete(self, uid: int, tid: str) -> bool:
        try:
            t = self.store.get(tid)
        except KeyError:
            return False
        return t.custom and (self.is_admin(uid) or t.config.get("owner") == uid)

    # ------------------------------------------------------------ канал для публікацій (в кожного користувача свій)

    def channel(self, uid: int) -> dict | None:
        return self.db.kv_get(f"channel:{uid}")

    @staticmethod
    def post_link(ch: dict, message_id: int) -> str | None:
        if ch.get("username"):
            return f"https://t.me/{ch['username']}/{message_id}"
        cid = str(ch["id"])
        return f"https://t.me/c/{cid[4:]}/{message_id}" if cid.startswith("-100") else None

    @staticmethod
    def parse_chat_ref(text: str) -> str | int | None:
        """@username, t.me/username, https://t.me/username або числовий ID (-100…) → що передати в get_chat."""
        t = text.strip()
        if re.fullmatch(r"-?\d{5,}", t):
            return int(t)
        m = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/?(?:\d+/?)?", t)
        if m:
            return "@" + m.group(1)
        m = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{3,31})", t)
        return "@" + m.group(1) if m else None

    async def connect_channel(self, ctx, msg: Message, uid: int, ref: str | int) -> bool:
        """Перевіряє канал і права (бота та користувача) і зберігає підключення. True — підключено."""
        how = ("Як підключити:\n1) додайте бота адміністратором каналу з правом «Публікація повідомлень»;\n"
               "2) перешліть мені будь-який пост із каналу або надішліть @username.\n/cancel — скасувати.")
        try:
            chat = await ctx.bot.get_chat(ref)
        except TelegramError:
            await msg.reply_text("⚠️ Не знаходжу такий канал (або бот його не бачить).\n\n" + how)
            return False
        if chat.type not in ("channel", "supergroup", "group"):
            await msg.reply_text("⚠️ Це не канал і не група. Потрібен канал або група Telegram.\n\n" + how)
            return False
        try:
            me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
        except TelegramError:
            me = None
        if not me or me.status not in ("administrator", "creator"):
            await msg.reply_text(f"⚠️ Бот не є адміністратором «{chat.title}».\n\n" + how)
            return False
        if chat.type == "channel" and me.status == "administrator" and not getattr(me, "can_post_messages", False):
            await msg.reply_text(f"⚠️ У бота в «{chat.title}» немає права «Публікація повідомлень». "
                                 "Увімкніть його в правах адміністратора й надішліть канал ще раз.")
            return False
        try:
            who = await ctx.bot.get_chat_member(chat.id, uid)
        except TelegramError:
            who = None
        if not who or who.status not in ("administrator", "creator"):
            await msg.reply_text(f"⚠️ Ви не адміністратор «{chat.title}», тож публікувати туди через бота не можна.")
            return False
        self.db.kv_set(f"channel:{uid}", {"id": chat.id, "title": chat.title or str(chat.id),
                                          "username": chat.username or "", "type": chat.type})
        self.db.set_state(uid, None)
        await msg.reply_text(f"✅ Підключено: «{chat.title}». Тепер під кожною листівкою є кнопка "
                             "«📢 Опублікувати в канал», а в меню — «Опублікувати останню».")
        log.info("user %s підключив канал %s", uid, chat.id)
        return True

    async def on_channel_input(self, ctx, msg: Message, uid: int):
        """Користувач у стані «підключення каналу»: чекаємо пересланий пост, @username, посилання або ID."""
        origin = msg.forward_origin
        if origin is not None:
            if isinstance(origin, MessageOriginChannel):
                await self.connect_channel(ctx, msg, uid, origin.chat.id)
            else:
                await msg.reply_text("Це переслано не з каналу. Перешліть пост саме з каналу або надішліть @username.")
            return
        ref = self.parse_chat_ref(msg.text or "") if msg.text else None
        if ref is None:
            await msg.reply_text("Надішліть @username каналу, посилання t.me/… або перешліть будь-який його пост. "
                                 "/cancel — скасувати.")
            return
        await self.connect_channel(ctx, msg, uid, ref)

    async def publish(self, q, ctx, uid: int, chat_id: int, mid: int, confirmed: bool = False):
        """Надсилає листівку (повідомлення mid у чаті з ботом) у підключений канал."""
        ch = self.channel(uid)
        if not ch:
            return await q.answer("Спершу підключіть канал: Меню → 📢 Канал", show_alert=True)
        rec = self.db.get_render(chat_id, mid)
        if not rec or not rec["file_id"]:
            return await q.answer("Цю листівку вже не можна опублікувати — створіть її знову.", show_alert=True)
        if rec["published_to"] == ch["id"] and not confirmed:   # уже в цьому каналі — спершу перепитуємо
            await q.answer()
            return await ctx.bot.send_message(
                chat_id, f"Цю листівку вже опубліковано в «{esc(ch['title'])}». Опублікувати ще раз?",
                parse_mode=ParseMode.HTML,
                reply_markup=kb([[("📢 Так, ще раз", f"p:yes:{mid}"), ("Ні", "x")]]))
        try:
            who = await ctx.bot.get_chat_member(ch["id"], uid)
            if who.status not in ("administrator", "creator"):
                return await q.answer("Ви більше не адміністратор цього каналу. Перепідключіть канал у меню.",
                                      show_alert=True)
            if rec["kind"] == "document":
                sent = await ctx.bot.send_document(ch["id"], rec["file_id"])
            else:
                sent = await ctx.bot.send_photo(ch["id"], rec["file_id"])
        except (Forbidden, BadRequest) as e:
            log.warning("публікація користувача %s у %s не вдалася: %s", uid, ch["id"], e)
            return await q.answer("Не вдалося опублікувати: бот більше не має доступу до каналу. "
                                  "Перепідключіть канал у меню.", show_alert=True)
        except TelegramError as e:
            log.warning("публікація користувача %s у %s не вдалася: %s", uid, ch["id"], e)
            return await q.answer("Не вдалося опублікувати, спробуйте ще раз.", show_alert=True)
        self.db.mark_published(chat_id, mid, ch["id"])
        await q.answer("✅ Опубліковано")
        link = self.post_link(ch, sent.message_id)
        await ctx.bot.send_message(
            chat_id, f"✅ Опубліковано в «{esc(ch['title'])}»" + (f"\n{link}" if link else ""),
            parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
        log.info("user %s: листівка опублікована в %s", uid, ch["id"])

    async def gate(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
        """Реєструє користувача й перевіряє доступ. Повертає id або None."""
        u = update.effective_user
        if not u or u.is_bot:
            return None
        name = " ".join(x for x in (u.first_name, u.last_name) if x)
        row = self.db.touch_user(u.id, name, u.username or "")
        if not ENV_ADMINS and not self.db.users("admin=1"):
            # перший, хто написав боту, стає адміністратором
            self.db.set_user(u.id, admin=1, allowed=1)
            log.warning("користувач %s став адміністратором (ADMIN_IDS не задано)", u.id)
            await ctx.bot.send_message(u.id, "⭐ Ви — адміністратор цього бота (перший користувач). "
                                             "Керування доступом: /menu → 🛠 Адміністрування.")
            return u.id
        if self.is_admin(u.id) or self.access_mode() == "all" or row["allowed"] == 1:
            return u.id
        if row["allowed"] == -1:
            await self._deny(update, "🔒 Доступ до бота закрито.")
            return None
        await self._deny(update, f"🔒 Доступ до бота обмежено. Ваш ID: <code>{u.id}</code>.\n"
                                 "Запит надіслано адміністратору, я напишу, коли його схвалять.")
        if not row["requested"]:
            self.db.set_user(u.id, requested=1)
            who = esc(name) + (f" (@{esc(u.username)})" if u.username else "") + f", ID <code>{u.id}</code>"
            for a in self.admin_ids():
                try:
                    await ctx.bot.send_message(a, f"🔔 Запит доступу: {who}", parse_mode=ParseMode.HTML,
                                               reply_markup=kb([[("✅ Дозволити", f"a:ok:{u.id}"), ("🚫 Відхилити", f"a:no:{u.id}")]]))
                except (Forbidden, BadRequest) as e:
                    log.info("не вдалося повідомити адміна %s: %s", a, e)
        return None

    def admin_ids(self) -> list[int]:
        return sorted(ENV_ADMINS | {r["id"] for r in self.db.users("admin=1")})

    async def _deny(self, update: Update, text: str):
        if update.callback_query:
            await update.callback_query.answer("Доступ обмежено", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    # ------------------------------------------------------------ рендер

    def template_or_default(self, uid: int):
        s = self.st(uid)
        try:
            return self.store.get(s["template"])
        except KeyError:
            ts = self.store.all()
            t = next((x for x in ts if x.id == DEFAULT_TEMPLATE), ts[0])
            self.set_st(uid, template=t.id)
            return t

    @staticmethod
    def overrides(s: dict) -> dict:
        body, title, footer = {}, {}, {}
        if s["font"]:
            body["font"] = s["font"]
        if s["title_font"]:
            title["font"] = s["title_font"]
        c = COLORS.get(s["color"])
        if c and c[1]:
            _, text, accent, ttl, glow = c
            body.update(color=text, accent_color=accent, glow_color=glow)
            title.update(color=ttl, glow_color=glow)
            footer.update(color=text if isinstance(text, str) else text[1])
        if not s["glow"]:
            for d in (body, title, footer):
                d["glow_color"] = None
        return {"body": body, "title": title, "footer": footer}

    async def render(self, uid: int, text: str, title_override: str | None, *, template_id: str | None = None,
                     size: str | None = None, s: dict | None = None):
        s = s or self.st(uid)
        if R.count_words(text) > MAX_WORDS:
            raise R.RenderError(f"Забагато слів: {R.count_words(text)}, максимум {MAX_WORDS} (найкраще 10–20).")
        if len(text) > MAX_CHARS:
            raise R.RenderError(f"Задовгий текст: {len(text)} символів, максимум {MAX_CHARS}.")
        t = self.store.get(template_id) if template_id else self.template_or_default(uid)
        if title_override is None:
            title = s["title"] if s["title_on"] else None
        else:
            title = title_override or None
        async with self.sem:
            res = await asyncio.to_thread(R.render_card, t.config, t.background, text, title,
                                          s["footer"] or None, size or s["size"], self.overrides(s))
            photo, _ = await asyncio.to_thread(R.encode, res.image, "jpg")
            file = None
            if s["send_as"] != "photo":
                file, _ = await asyncio.to_thread(R.encode, res.image, s["format"])
        return t, res, photo, file

    def card_kb(self) -> InlineKeyboardMarkup:
        return kb([[("🔄 Інший шаблон", "r:next"), ("🖼 Вибрати", "r:pick")],
                   [("📐 Розмір", "r:size"), ("📄 Файлом", "r:file")],
                   [("📢 Опублікувати в канал", "r:pub")]])

    async def send_card(self, ctx, chat_id: int, uid: int, text: str, title_override: str | None,
                        template_id: str | None = None, size: str | None = None, force_file: bool = False):
        s = self.st(uid)
        if force_file:
            s = {**s, "send_as": "file"}
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        try:
            t, res, photo, file = await self.render(uid, text, title_override, template_id=template_id, size=size, s=s)
        except R.RenderError as e:
            await ctx.bot.send_message(chat_id, f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
            return
        caption = ("⚠️ " + "; ".join(res.warnings)) if res.warnings else None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if s["send_as"] in ("photo", "both"):
            m = await ctx.bot.send_photo(chat_id, InputFile(io.BytesIO(photo), filename=f"card-{stamp}.jpg"),
                                         caption=caption, reply_markup=self.card_kb())
            self.db.add_render(chat_id, m.message_id, uid, text, title_override)
            if m.photo:   # file_id потрібен, щоб кнопка «Опублікувати» надіслала в канал саме цю картинку
                self.db.set_render_file(chat_id, m.message_id, "photo", m.photo[-1].file_id)
        if s["send_as"] in ("file", "both"):
            ext = "jpg" if s["format"] == "jpg" else "png"
            m = await ctx.bot.send_document(chat_id, InputFile(io.BytesIO(file), filename=f"card-{t.id}-{stamp}.{ext}"),
                                            caption=caption if s["send_as"] == "file" else None,
                                            reply_markup=self.card_kb() if s["send_as"] == "file" else None)
            self.db.add_render(chat_id, m.message_id, uid, text, title_override)
            if s["send_as"] == "file" and m.document:   # у режимі «фото + файл» публікується фото (воно з кнопками)
                self.db.set_render_file(chat_id, m.message_id, "document", m.document.file_id)
        log.info("user %s: картка %s %s", uid, t.id, size or s["size"])

    # ------------------------------------------------------------ екрани меню

    def t_name(self, tid: str) -> str:
        try:
            return self.store.get(tid).config.get("name", tid)
        except KeyError:
            return tid

    def screen(self, uid: int, name: str, arg: str = "") -> tuple[str, InlineKeyboardMarkup]:
        s = self.st(uid)
        back = [("⬅️ Назад", "m:main")]
        if name == "main":
            t = self.template_or_default(uid)
            title = esc(s["title"]) if s["title_on"] else "<i>не показується</i>"
            ch = self.channel(uid)
            fonts = []
            if s["font"]:
                fonts.append(f"текст {esc(s['font'])}")
            if s["title_font"]:
                fonts.append(f"заголовок {esc(s['title_font'])}")
            text = ("⚙️ <b>Налаштування</b>\n\n"
                    f"🖼 Шаблон: <b>{esc(t.config.get('name', t.id))}</b>\n"
                    f"🏷 Заголовок: <b>{title}</b>\n"
                    f"✍️ Підпис: {esc(s['footer']) or '—'}\n"
                    f"📐 Розмір: {s['size'].replace('x', '×')}\n"
                    f"🔤 Шрифт: {', '.join(fonts) or 'як у шаблоні'}\n"
                    f"🎨 Колір: {COLORS.get(s['color'], COLORS[''])[0]} · ✨ світіння: {'так' if s['glow'] else 'ні'}\n"
                    f"📤 Надсилати: {SEND_AS[s['send_as']]}"
                    + (f" ({s['format'].upper()})" if s["send_as"] != "photo" else "")
                    + f"\n📢 Канал: {('<b>' + esc(ch['title']) + '</b>') if ch else '<i>не підключено</i>'}"
                    + "\n\nНадішліть текст (10–20 слів) — отримаєте листівку.")
            rows = [[("🖼 Шаблон", "m:tpl:0"), ("🏷 Заголовок", "m:title")],
                    [("✍️ Підпис", "m:footer"), ("📐 Розмір", "m:size")],
                    [("🔤 Шрифт", "m:font:body"), ("🎨 Колір", "m:color")],
                    [(f"✨ Світіння: {'так' if s['glow'] else 'ні'}", "s:glow"), ("📤 Надсилати", "m:send")],
                    [("📢 Канал", "m:chan"), ("📢 Опублікувати останню", "p:last")],
                    [("🗂 Шаблони: додати / видалити", "m:tm")]]
            if self.is_admin(uid):
                rows.append([("🛠 Адміністрування", "m:admin")])
            rows.append([("↩️ Скинути все", "m:reset"), ("✖️ Закрити", "x")])
            return text, kb(rows)

        if name in ("tpl", "pick"):
            # pick — вибір із-під готової листівки (arg = "<сторінка>:<id повідомлення>")
            page, _, mid = arg.partition(":")
            page = int(page or 0)
            ts = self.store.all()
            pages = max(1, (len(ts) + PAGE - 1) // PAGE)
            page = min(max(0, page), pages - 1)
            cur = s["template"]
            btns = []
            for t in ts[page * PAGE:(page + 1) * PAGE]:
                label = ("✅ " if t.id == cur else "") + t.config.get("name", t.id) + (" 📷" if t.custom else "")
                btns.append((label, f"tr:{t.id}:{mid}" if name == "pick" else f"t:{t.id}"))
            rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
            nav = []
            pre = f"m:{name}:"
            if page > 0:
                nav.append(("◀️", f"{pre}{page - 1}:{mid}" if name == "pick" else f"{pre}{page - 1}"))
            if page < pages - 1:
                nav.append(("▶️", f"{pre}{page + 1}:{mid}" if name == "pick" else f"{pre}{page + 1}"))
            rows.append(nav)
            rows.append([("👁 Приклади", f"ex:{page}")] + ([] if name == "pick" else back))
            hint = "Виберіть шаблон — листівку буде перемальовано." if name == "pick" else "Виберіть шаблон. 📷 — додані фото."
            return f"🖼 <b>Шаблони</b> ({len(ts)})\n{hint}", kb(rows)

        if name == "title":
            shown = "так" if s["title_on"] else "ні"
            text = (f"🏷 <b>Заголовок</b>\n\n<b>{esc(s['title']) or '—'}</b>\nПоказувати: {shown}\n"
                    f"Шрифт заголовка: {esc(s['title_font']) or 'як у шаблоні'}\n\n"
                    "Разовий заголовок: почніть повідомлення рядком <code># Заголовок</code>.")
            rows = [[("✏️ Змінити текст", "i:title"), (f"👁 Показувати: {shown}", "s:title_on")],
                    [(f"↩️ «{DEFAULT_TITLE[:30]}»", "s:title_reset"), ("🔤 Шрифт заголовка", "m:font:title")],
                    back]
            return text, kb(rows)

        if name == "footer":
            text = f"✍️ <b>Підпис внизу листівки</b>\n\n{esc(s['footer']) or '— немає —'}\n\nНаприклад, сайт або ім'я."
            return text, kb([[("✏️ Змінити", "i:footer"), ("🗑 Прибрати", "s:footer_clear")], back])

        if name == "size":
            rows = [[(("✅ " if s["size"] == v else "") + label, f"s:size:{v}")] for v, label in SIZES]
            rows.append([("✏️ Свій розмір", "i:size")])
            rows.append(back)
            return f"📐 <b>Розмір</b>\nЗараз: {s['size'].replace('x', '×')}", kb(rows)

        if name == "font":
            which = "title" if arg == "title" else "body"
            key = "title_font" if which == "title" else "font"
            cur = s[key]
            fonts = R.list_fonts()
            btns = [(("✅ " if cur == "" else "") + "як у шаблоні", f"s:font:{which}:-")]
            btns += [(("✅ " if cur == f else "") + f.replace("-", " "), f"s:font:{which}:{f}") for f in fonts]
            rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
            rows.append([("⬅️ Назад", "m:title" if which == "title" else "m:main")])
            label = "заголовка" if which == "title" else "тексту"
            return f"🔤 <b>Шрифт {label}</b>\nУсі шрифти з українською абеткою.", kb(rows)

        if name == "color":
            btns = [(("✅ " if s["color"] == k else "") + v[0], f"s:color:{k or '-'}") for k, v in COLORS.items()]
            rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
            rows.append([(f"✨ Світіння: {'так' if s['glow'] else 'ні'}", "s:glow:color")])
            rows.append(back)
            return ("🎨 <b>Колір тексту</b>\nСвітлий текст — для темних фонів, темний — для світлих. "
                    "Акцентний колір (для <code>*слів*</code>) і світіння підбираються в пару."), kb(rows)

        if name == "send":
            rows = [[(("✅ " if s["send_as"] == k else "") + v, f"s:send:{k}") for k, v in SEND_AS.items()],
                    [(("✅ " if s["format"] == f else "") + f.upper(), f"s:fmt:{f}") for f in ("png", "jpg")],
                    back]
            return ("📤 <b>Як надсилати</b>\nФото Telegram стискає. Файл — без втрат, у вибраному форматі "
                    "(PNG — найкраща якість, JPEG — менший розмір)."), kb(rows)

        if name == "chan":
            ch = self.channel(uid)
            if ch:
                kind = "канал" if ch.get("type", "channel") == "channel" else "група"
                at = f" · @{esc(ch['username'])}" if ch.get("username") else ""
                text = (f"📢 <b>Публікація</b>\n\nПідключено ({kind}): <b>{esc(ch['title'])}</b>{at}\n\n"
                        "Кнопка «📢 Опублікувати в канал» під листівкою надсилає саме цю картинку, а «Опублікувати "
                        "останню» в меню — вашу останню листівку. Перед кожною публікацією перевіряю, що ви й бот "
                        "досі адміністратори.")
                rows = [[("📢 Опублікувати останню", "p:last")],
                        [("🔄 Інший канал", "i:chan_connect"), ("🔌 Відключити", "c:off")], back]
            else:
                text = ("📢 <b>Публікація в канал</b>\n\nКанал не підключено.\n\n"
                        "1) Додайте бота адміністратором свого каналу з правом «Публікація повідомлень».\n"
                        "2) Натисніть «Підключити» і перешліть мені будь-який пост із каналу "
                        "(або надішліть @username).\n\n"
                        "Підключити можна лише канал, де адміністратор ви самі.")
                rows = [[("➕ Підключити канал", "i:chan_connect")], back]
            return text, kb(rows)

        if name == "tm":
            rows = []
            if self.can_add(uid):
                rows.append([("➕ Додати шаблон (фото)", "i:addtpl")])
            mine = [t for t in self.store.all() if self.can_delete(uid, t.id)]
            if mine:
                rows.append([(f"🗑 Видалити… ({len(mine)})", "m:del")])
            rows.append([("👁 Приклади всіх шаблонів", "ex:0")])
            rows.append(back)
            add = ("Надішліть боту фото (краще <b>файлом</b>, без стиснення) — з нього вийде новий шаблон."
                   if self.can_add(uid) else "Додавати шаблони можуть лише адміністратори.")
            return f"🗂 <b>Шаблони</b>\n{add}", kb(rows)

        if name == "del":
            mine = [t for t in self.store.all() if self.can_delete(uid, t.id)]
            rows = [[("🗑 " + t.config.get("name", t.id), f"dt:{t.id}")] for t in mine[:40]]
            rows.append([("⬅️ Назад", "m:tm")])
            return "🗑 <b>Видалити шаблон</b>\nВбудовані шаблони не видаляються.", kb(rows)

        if name == "deltpl":
            return (f"Видалити шаблон «{esc(self.t_name(arg))}»?",
                    kb([[("🗑 Так, видалити", f"dy:{arg}"), ("Ні", "m:del")]]))

        if name == "reset":
            return ("Скинути всі ваші налаштування до типових?",
                    kb([[("↩️ Так, скинути", "s:reset"), ("Ні", "m:main")]]))

        if name == "admin" and self.is_admin(uid):
            acc = "усі" if self.access_mode() == "all" else "лише дозволені"
            add = "усі" if self.add_mode() == "all" else "лише адміни"
            n = len(self.db.users("allowed=1 AND admin=0"))
            text = ("🛠 <b>Адміністрування</b>\n\n"
                    f"Хто користується ботом: <b>{acc}</b>\n"
                    f"Хто додає шаблони: <b>{add}</b>\n"
                    f"Дозволених користувачів: {n}\n\n"
                    "«Мої налаштування → типові»: нові користувачі отримають ваші шаблон, заголовок, розмір тощо.")
            rows = [[(f"👥 Бот: {acc}", "a:mode"), (f"🗂 Шаблони: {add}", "a:addmode")],
                    [("👤 Користувачі", "m:users"), ("➕ Дозволити за ID", "i:adduser")],
                    [("⭐ Мої налаштування → типові", "a:defaults")],
                    back]
            return text, kb(rows)

        if name == "users" and self.is_admin(uid):
            rows = []
            for r in self.db.users("id != ?", (uid,))[:25]:
                if r["id"] in ENV_ADMINS:
                    continue
                mark = "⭐" if r["admin"] else ("✅" if r["allowed"] == 1 else "🚫" if r["allowed"] == -1 else "⏳")
                label = f"{mark} {r['name'] or r['username'] or r['id']}"[:40]
                rows.append([(label, "noop"),
                             ("🚫" if r["allowed"] == 1 else "✅", f"a:{'no' if r['allowed'] == 1 else 'ok'}:{r['id']}"),
                             ("☆" if r["admin"] else "⭐", f"a:adm:{r['id']}")])
            rows.append([("⬅️ Назад", "m:admin")])
            return ("👤 <b>Користувачі</b> (останні 25)\n⭐ адмін · ✅ дозволено · ⏳ без рішення · 🚫 заборонено\n"
                    "Кнопки: дозволити/заборонити, зробити адміном / зняти."), kb(rows)

        if name == "newbase":
            ts = self.store.all()
            btns = [(t.config.get("name", t.id), f"n:base:{t.id}") for t in ts[:30]]
            rows = [btns[i:i + 2] for i in range(0, len(btns), 2)] + [[("✖️ Скасувати", "n:cancel")]]
            return ("🎨 <b>Стиль тексту</b> — як у якому шаблоні?\n"
                    "Для темного фото беріть шаблон зі світлим текстом (Нічне небо, Захід сонця, Морські промені), "
                    "для світлого — з темним (Рожеве сяйво, Лавандові хмари, Блакитне небо, Лимонне сонце, "
                    "Старий папір)."), kb(rows)

        if name == "newov":
            rows = [[(v[0], f"n:ov:{k}")] for k, v in OVERLAYS.items()] + [[("✖️ Скасувати", "n:cancel")]]
            return "🌓 <b>Фон</b>: обробити фото, щоб текст краще читався?", kb(rows)

        return self.screen(uid, "main")

    # ------------------------------------------------------------ команди

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        self.db.set_state(uid, None)
        await update.message.reply_text(
            "👋 Привіт! Я роблю листівки з текстом.\n\n" + HELP, parse_mode=ParseMode.HTML)
        text, markup = self.screen(uid, "main")
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_help(self, update: Update, ctx):
        if await self.gate(update, ctx) is None:
            return
        await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)

    async def cmd_menu(self, update: Update, ctx):
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        text, markup = self.screen(uid, "main")
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_templates(self, update: Update, ctx):
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        text, markup = self.screen(uid, "tpl", "0")
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_title(self, update: Update, ctx):
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        arg = " ".join(ctx.args or []).strip()
        if arg:
            await self.set_title(update.message, uid, arg)
        else:
            await self.prompt(update.message, uid, "title")

    async def cmd_channel(self, update: Update, ctx):
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        text, markup = self.screen(uid, "chan")
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_cancel(self, update: Update, ctx):
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        self.db.set_state(uid, None)
        self._drop_tmp(uid)
        await update.message.reply_text("Скасовано.")

    async def cmd_id(self, update: Update, ctx):
        u = update.effective_user
        await update.message.reply_text(f"Ваш ID: <code>{u.id}</code>", parse_mode=ParseMode.HTML)

    # ------------------------------------------------------------ введення тексту

    PROMPTS = {
        "title": "✏️ Надішліть новий заголовок (до 80 символів). /cancel — скасувати.",
        "footer": "✏️ Надішліть підпис (до 100 символів), напр. сайт або ім'я. /cancel — скасувати.",
        "size": "✏️ Надішліть розмір у пікселях: <code>ШИРИНАxВИСОТА</code>, напр. <code>1500x900</code> (200…4096). /cancel — скасувати.",
        "tpl_photo": "📷 Надішліть фото для нового шаблону — краще <b>файлом</b> (📎 → Файл), тоді Telegram його не стискає. "
                     "Мінімум 400×400 px. /cancel — скасувати.",
        "adduser": "✏️ Надішліть Telegram ID користувача (число). Свій ID людина бачить командою /id. /cancel — скасувати.",
        "chan_connect": "📢 Перешліть мені будь-який пост зі свого каналу або надішліть @username каналу "
                        "(бот має бути його адміністратором з правом «Публікація повідомлень»). /cancel — скасувати.",
    }

    async def prompt(self, msg: Message, uid: int, state: str, data: dict | None = None):
        self.db.set_state(uid, state, data)
        await msg.reply_text(self.PROMPTS[state], parse_mode=ParseMode.HTML)

    async def set_title(self, msg: Message, uid: int, title: str):
        title = " ".join(title.split())[:80]
        self.set_st(uid, title=title, title_on=True)
        self.db.set_state(uid, None)
        await msg.reply_text(f"🏷 Заголовок: <b>{esc(title)}</b>", parse_mode=ParseMode.HTML)

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        msg = update.message
        text = msg.text or ""
        state, data = self.db.state(uid)
        if state == "chan_connect":
            return await self.on_channel_input(ctx, msg, uid)
        if state == "title":
            return await self.set_title(msg, uid, text)
        if state == "footer":
            self.set_st(uid, footer=" ".join(text.split())[:100])
            self.db.set_state(uid, None)
            return await msg.reply_text(f"✍️ Підпис: {esc(self.st(uid)['footer'])}", parse_mode=ParseMode.HTML)
        if state == "size":
            try:
                w, h = R.parse_size(text.strip().replace(" ", ""), DEFAULT_SIZE)
            except R.RenderError as e:
                return await msg.reply_text(f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
            self.set_st(uid, size=f"{w}x{h}")
            self.db.set_state(uid, None)
            return await msg.reply_text(f"📐 Розмір: {w}×{h}")
        if state == "adduser" and self.is_admin(uid):
            t = text.strip()
            if not t.lstrip("-").isdigit():
                return await msg.reply_text("Потрібне число — Telegram ID. /cancel — скасувати.")
            self.db.ensure_user(int(t))
            self.db.set_user(int(t), allowed=1)
            self.db.set_state(uid, None)
            return await msg.reply_text(f"✅ Користувачу {t} дозволено користуватися ботом.")
        if state == "tpl_name":
            data["name"] = " ".join(text.split())[:60]
            self.db.set_state(uid, "tpl_base", data)
            t, m = self.screen(uid, "newbase")
            return await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
        if state in ("tpl_photo", "tpl_base", "tpl_ov"):
            return await msg.reply_text("Зараз я чекаю на фото / вибір кнопкою. /cancel — скасувати додавання шаблону.")

        # звичайний текст → листівка
        body, title = text, None
        lines = text.strip().split("\n")
        if lines and lines[0].lstrip().startswith("#"):
            title = lines[0].strip().lstrip("#").strip()
            body = "\n".join(lines[1:]).strip()
        if not body.strip():
            return await msg.reply_text("Після заголовка потрібен текст листівки.")
        await self.send_card(ctx, msg.chat_id, uid, body, title)

    # ------------------------------------------------------------ нові шаблони з фото

    def _tmp_path(self, uid: int) -> Path:
        return self.tmp / f"{uid}.img"

    def _drop_tmp(self, uid: int):
        self._tmp_path(uid).unlink(missing_ok=True)

    async def on_image(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        msg = update.message
        if self.db.state(uid)[0] == "chan_connect":   # пересланий із каналу пост із картинкою
            return await self.on_channel_input(ctx, msg, uid)
        if not self.can_add(uid):
            return await msg.reply_text("Додавати шаблони можуть лише адміністратори.")
        if msg.photo:
            f = msg.photo[-1]
            compressed = True
        else:
            f = msg.document
            compressed = False
            if not (f.mime_type or "").startswith("image/"):
                return await msg.reply_text("Це не зображення. Для шаблону надішліть фото або картинку файлом.")
        if f.file_size and f.file_size > MAX_UPLOAD_MB * 1024 * 1024:
            return await msg.reply_text(f"Файл завеликий: максимум {MAX_UPLOAD_MB:g} МБ.")
        tg_file = await f.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        self._tmp_path(uid).write_bytes(data)
        self.db.set_state(uid, "tpl_name", {})
        note = ("\n\nℹ️ Фото стиснуте Telegram (до 1280 px). Для сторіс і великих розмірів краще надіслати файлом."
                if compressed else "")
        await msg.reply_text("📷 Фото отримав. Як назвати шаблон? Надішліть назву або натисніть кнопку." + note,
                             reply_markup=kb([[("Без назви", "n:noname"), ("✖️ Скасувати", "n:cancel")]]))

    async def finish_template(self, q, ctx, uid: int, data: dict):
        path = self._tmp_path(uid)
        if not path.is_file():
            self.db.set_state(uid, None)
            return await q.edit_message_text("Фото загубилося, надішліть його ще раз.")
        try:
            t = await asyncio.to_thread(self.store.create, data.get("name") or "Моє фото", path.read_bytes(), data["base"])
            cfg = dict(t.config)
            cfg["owner"] = uid
            ov = OVERLAYS.get(data.get("ov", "none"), OVERLAYS["none"])[1]
            if ov:
                cfg["overlay"] = ov
            t = self.store.update(t.id, cfg)
        except (R.RenderError, KeyError) as e:
            self.db.set_state(uid, None)
            self._drop_tmp(uid)
            return await q.edit_message_text(f"⚠️ Не вдалося створити шаблон: {e}")
        self._drop_tmp(uid)
        self.db.set_state(uid, None)
        self.set_st(uid, template=t.id)
        await q.edit_message_text(f"✅ Шаблон «{t.config['name']}» додано і вибрано.")
        last = self.db.last_render(uid)
        text = last["text"] if last else SAMPLE_TEXT
        await self.send_card(ctx, q.message.chat_id, uid, text, last["title"] if last else None)
        log.info("user %s додав шаблон %s", uid, t.id)

    # ------------------------------------------------------------ кнопки

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        data = q.data or ""
        parts = data.split(":")
        kind = parts[0]
        toast = None

        async def show(name: str, arg: str = ""):
            text, markup = self.screen(uid, name, arg)
            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    # меню під фото не редагується як текст — шлемо нове повідомлення
                    await ctx.bot.send_message(q.message.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)

        if kind == "noop":
            return await q.answer()
        if kind == "x":
            await q.answer()
            try:
                await q.message.delete()
            except BadRequest:
                await q.edit_message_reply_markup(None)
            return
        if kind == "m":
            await q.answer()
            return await show(parts[1], ":".join(parts[2:]))
        if kind == "i":
            await q.answer()
            what = parts[1]
            if what == "addtpl":
                if not self.can_add(uid):
                    return await q.message.reply_text("Додавати шаблони можуть лише адміністратори.")
                return await self.prompt(q.message, uid, "tpl_photo")
            if what == "adduser" and not self.is_admin(uid):
                return
            return await self.prompt(q.message, uid, what)

        if kind == "s":
            key = parts[1]
            s = self.st(uid)
            back = "main"
            if key == "glow":
                self.set_st(uid, glow=not s["glow"])
                back = "color" if len(parts) > 2 else "main"
            elif key == "title_on":
                self.set_st(uid, title_on=not s["title_on"])
                back = "title"
            elif key == "title_reset":
                self.set_st(uid, title=DEFAULT_TITLE, title_on=True)
                back, toast = "title", f"Заголовок: {DEFAULT_TITLE}"
            elif key == "footer_clear":
                self.set_st(uid, footer="")
                back, toast = "footer", "Підпис прибрано"
            elif key == "size":
                self.set_st(uid, size=parts[2])
                back, toast = "size", f"Розмір {parts[2].replace('x', '×')}"
            elif key == "font":
                which, val = parts[2], parts[3]
                if val != "-" and val not in R.list_fonts():
                    return await q.answer("Немає такого шрифту", show_alert=True)
                self.set_st(uid, **{("title_font" if which == "title" else "font"): "" if val == "-" else val})
                await q.answer("Збережено")
                return await show("font", which)
            elif key == "color":
                val = "" if parts[2] == "-" else parts[2]
                if val in COLORS:
                    self.set_st(uid, color=val)
                back = "color"
            elif key == "send" and parts[2] in SEND_AS:
                self.set_st(uid, send_as=parts[2])
                back = "send"
            elif key == "fmt" and parts[2] in ("png", "jpg"):
                self.set_st(uid, format=parts[2])
                back = "send"
            elif key == "reset":
                self.db.save_settings(uid, {})
                toast = "Налаштування скинуто"
            await q.answer(toast or "Збережено")
            return await show(back)

        if kind == "t":
            tid = parts[1]
            try:
                t = self.store.get(tid)
            except KeyError:
                return await q.answer("Шаблон не знайдено", show_alert=True)
            self.set_st(uid, template=tid)
            await q.answer(f"Шаблон: {t.config.get('name', tid)}")
            if q.message and q.message.photo:   # кнопка під превʼю нового шаблону
                return
            return await show("main")

        if kind == "c":
            if parts[1] == "off":
                self.db.kv_del(f"channel:{uid}")
                await q.answer("Канал відключено")
                return await show("chan")
            return await q.answer()

        if kind == "p":
            chat_id = q.message.chat_id
            if parts[1] == "last":
                last = self.db.last_publishable(uid)
                if not last:
                    return await q.answer("Ще немає листівки для публікації: спершу надішліть текст.", show_alert=True)
                return await self.publish(q, ctx, uid, last["chat_id"], last["message_id"])
            if parts[1] == "yes" and len(parts) > 2 and parts[2].isdigit():
                await self.publish(q, ctx, uid, chat_id, int(parts[2]), confirmed=True)
                try:
                    await q.message.delete()   # запитання «ще раз?» більше не потрібне
                except TelegramError:
                    pass
                return
            return await q.answer()

        if kind == "ex":
            await q.answer("Готую приклади…")
            return await self.send_examples(ctx, q.message.chat_id, uid, int(parts[1]))

        if kind == "dt":
            if not self.can_delete(uid, parts[1]):
                return await q.answer("Цей шаблон видалити не можна", show_alert=True)
            await q.answer()
            return await show("deltpl", parts[1])
        if kind == "dy":
            tid = parts[1]
            if not self.can_delete(uid, tid):
                return await q.answer("Цей шаблон видалити не можна", show_alert=True)
            name = self.t_name(tid)
            self.store.delete(tid)
            await q.answer(f"Видалено: {name}")
            return await show("del")

        if kind == "n":
            state, sdata = self.db.state(uid)
            what = parts[1]
            if what == "cancel":
                self.db.set_state(uid, None)
                self._drop_tmp(uid)
                await q.answer("Скасовано")
                return await q.edit_message_text("Додавання шаблону скасовано.")
            if what == "noname" and state == "tpl_name":
                sdata["name"] = ""
                self.db.set_state(uid, "tpl_base", sdata)
                await q.answer()
                return await show("newbase")
            if what == "base" and state == "tpl_base":
                sdata["base"] = parts[2]
                self.db.set_state(uid, "tpl_ov", sdata)
                await q.answer()
                return await show("newov")
            if what == "ov" and state == "tpl_ov" and parts[2] in OVERLAYS:
                sdata["ov"] = parts[2]
                await q.answer("Створюю шаблон…")
                return await self.finish_template(q, ctx, uid, sdata)
            return await q.answer("Ця кнопка вже не активна", show_alert=True)

        if kind == "a":
            if not self.is_admin(uid):
                return await q.answer("Лише для адміністраторів", show_alert=True)
            act = parts[1]
            if act == "mode":
                self.db.kv_set("access", "allowed" if self.access_mode() == "all" else "all")
                await q.answer("Доступ змінено")
                return await show("admin")
            if act == "addmode":
                self.db.kv_set("add_templates", "all" if self.add_mode() == "admins" else "admins")
                await q.answer("Збережено")
                return await show("admin")
            if act == "defaults":
                s = self.st(uid)
                self.db.kv_set("defaults", s)
                return await q.answer("Ваші налаштування стали типовими для нових користувачів", show_alert=True)
            target = int(parts[2])
            if target in ENV_ADMINS:
                return await q.answer("Цей адмін заданий у ADMIN_IDS", show_alert=True)
            row = self.db.user(target)
            if act in ("ok", "no"):
                self.db.ensure_user(target)
                self.db.set_user(target, allowed=1 if act == "ok" else -1, requested=1)
                if act == "no":
                    self.db.set_user(target, admin=0)
                await q.answer("Дозволено" if act == "ok" else "Заборонено")
                try:
                    await ctx.bot.send_message(target, "✅ Доступ до бота відкрито! Надішліть текст або /start."
                                               if act == "ok" else "🔒 Адміністратор відхилив запит доступу.")
                except (Forbidden, BadRequest):
                    pass
                if q.message.text and q.message.text.startswith("🔔"):
                    return await q.edit_message_text(q.message.text + ("\n✅ Дозволено" if act == "ok" else "\n🚫 Відхилено"))
                return await show("users")
            if act == "adm" and row:
                self.db.set_user(target, admin=0 if row["admin"] else 1, allowed=1)
                await q.answer("Збережено")
                return await show("users")
            return await q.answer()

        if kind in ("r", "tr", "sr"):
            return await self.on_card_button(q, ctx, uid, parts)

        await q.answer()

    async def on_card_button(self, q, ctx, uid: int, parts: list[str]):
        chat_id = q.message.chat_id
        kind = parts[0]
        mid = q.message.message_id if kind == "r" else int(parts[2] if kind == "tr" else parts[-1])
        rec = self.db.get_render(chat_id, mid)
        if not rec:
            return await q.answer("Текст цієї листівки вже не збережено — надішліть його ще раз.", show_alert=True)
        text, title = rec["text"], rec["title"]
        if kind == "r":
            act = parts[1]
            if act == "next":
                ts = self.store.all()
                cur = self.st(uid)["template"]
                ids = [t.id for t in ts]
                nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
                self.set_st(uid, template=nxt)
                await q.answer(self.t_name(nxt))
                return await self.send_card(ctx, chat_id, uid, text, title)
            if act == "pick":
                await q.answer()
                t, m = self.screen(uid, "pick", f"0:{mid}")
                return await ctx.bot.send_message(chat_id, t, parse_mode=ParseMode.HTML, reply_markup=m)
            if act == "size":
                await q.answer()
                s = self.st(uid)
                rows = [[(("✅ " if s["size"] == v else "") + label, f"sr:{v}:{mid}")] for v, label in SIZES]
                return await ctx.bot.send_message(chat_id, "📐 У якому розмірі перемалювати?", reply_markup=kb(rows))
            if act == "file":
                await q.answer("Надсилаю файлом…")
                return await self.send_card(ctx, chat_id, uid, text, title, force_file=True)
            if act == "pub":
                return await self.publish(q, ctx, uid, chat_id, mid)
        if kind == "tr":
            tid = parts[1]
            try:
                self.store.get(tid)
            except KeyError:
                return await q.answer("Шаблон не знайдено", show_alert=True)
            self.set_st(uid, template=tid)
            await q.answer(self.t_name(tid))
            return await self.send_card(ctx, chat_id, uid, text, title)
        if kind == "sr":
            self.set_st(uid, size=parts[1])
            await q.answer(parts[1].replace("x", "×"))
            return await self.send_card(ctx, chat_id, uid, text, title)

    async def send_examples(self, ctx, chat_id: int, uid: int, page: int):
        ts = self.store.all()[page * PAGE:(page + 1) * PAGE]
        if not ts:
            return
        s = self.st(uid)
        title = s["title"] if s["title_on"] else None
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        media = []
        for t in ts:
            key = (t.id, t.mtime, title)
            if key not in self.preview_cache:
                async with self.sem:
                    res = await asyncio.to_thread(R.render_card, t.config, t.background, SAMPLE_TEXT, title, None, "800x489")
                    self.preview_cache[key], _ = await asyncio.to_thread(R.encode, res.image, "jpg")
            media.append(InputMediaPhoto(self.preview_cache[key], caption=t.config.get("name", t.id)))
        if len(media) == 1:
            await ctx.bot.send_photo(chat_id, media[0].media, caption=media[0].caption)
        else:
            await ctx.bot.send_media_group(chat_id, media)
        t, m = self.screen(uid, "tpl", str(page))
        await ctx.bot.send_message(chat_id, t, parse_mode=ParseMode.HTML, reply_markup=m)

    async def on_other(self, update: Update, ctx):
        uid = await self.gate(update, ctx)
        if uid is None:
            return
        if self.db.state(uid)[0] == "chan_connect":   # пересланий із каналу пост без тексту/картинки (відео тощо)
            return await self.on_channel_input(ctx, update.message, uid)
        await update.message.reply_text("Надішліть текст для листівки або /help.")

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE):
        log.error("помилка обробки оновлення", exc_info=ctx.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await ctx.bot.send_message(update.effective_chat.id, "⚠️ Щось пішло не так. Спробуйте ще раз або /cancel.")
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------ збірка застосунку

async def _post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("menu", "Налаштування"),
        BotCommand("templates", "Вибрати шаблон"),
        BotCommand("channel", "Канал для публікацій"),
        BotCommand("title", "Змінити заголовок"),
        BotCommand("help", "Як користуватися"),
        BotCommand("cancel", "Скасувати введення"),
        BotCommand("start", "Почати"),
    ])
    me = await app.bot.get_me()
    log.info("бот @%s запущено", me.username)


def build_app(token: str, data_dir: Path = DATA_DIR, base_url: str | None = None,
              base_file_url: str | None = None) -> tuple[Application, CardBot]:
    cb = CardBot(data_dir)
    b = (ApplicationBuilder().token(token).concurrent_updates(8).post_init(_post_init)
         .connect_timeout(15).read_timeout(40).write_timeout(90).media_write_timeout(120).pool_timeout(15))
    if base_url:
        b = b.base_url(base_url).base_file_url(base_file_url or base_url.replace("/bot", "/file/bot"))
    app = b.build()
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cb.cmd_start, filters=private))
    app.add_handler(CommandHandler("help", cb.cmd_help, filters=private))
    app.add_handler(CommandHandler(["menu", "settings"], cb.cmd_menu, filters=private))
    app.add_handler(CommandHandler("templates", cb.cmd_templates, filters=private))
    app.add_handler(CommandHandler("channel", cb.cmd_channel, filters=private))
    app.add_handler(CommandHandler("title", cb.cmd_title, filters=private))
    app.add_handler(CommandHandler("cancel", cb.cmd_cancel, filters=private))
    app.add_handler(CommandHandler("id", cb.cmd_id, filters=private))
    app.add_handler(MessageHandler(private & (filters.PHOTO | filters.Document.ALL), cb.on_image))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, cb.on_text))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, cb.on_other))
    app.add_handler(CallbackQueryHandler(cb.on_callback))
    app.add_error_handler(cb.on_error)
    return app, cb


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx пише URL з токеном
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Задайте BOT_TOKEN (токен від @BotFather)")
    app, _ = build_app(token, DATA_DIR, os.environ.get("TELEGRAM_API_URL") or None,
                       os.environ.get("TELEGRAM_FILE_URL") or None)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
