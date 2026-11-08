"""Наскрізний тест бота проти імітації Telegram Bot API (tests/fake_tg.py), без мережі.

    python3 tests/test_bot.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
DATA = Path(tempfile.mkdtemp(prefix="cardbot-"))
os.environ["DATA_DIR"] = str(DATA)
os.environ.pop("ADMIN_IDS", None)

from PIL import Image  # noqa: E402

from fake_tg import FakeTelegram  # noqa: E402
from bot import main as M  # noqa: E402

PORT = 18765
TOKEN = "123456:TEST"
A, B, C = 1111, 2222, 3333
TEXT = "Я відкриваю своє серце присутності Творця. Нехай Божественна Мудрість наповнює мої думки, слова, почуття та *вчинки*."

fake = FakeTelegram(PORT)
passed, failed = 0, []


def check(cond, label):
    global passed
    if cond:
        passed += 1
        print("  ok  ", label)
    else:
        failed.append(label)
        print("  FAIL", label)


async def wait(mark, pred, timeout=15.0, label=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for c in fake.since(mark):
            if pred(c):
                return c
        await asyncio.sleep(0.03)
    raise AssertionError(f"не дочекався: {label}")


async def settle(mark, quiet=0.4, timeout=15.0):
    """Чекає, поки бот перестане робити виклики (кінець обробки)."""
    t0 = time.time()
    last, n = time.time(), fake.mark()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.05)
        if fake.mark() != n:
            n, last = fake.mark(), time.time()
        elif time.time() - last > quiet and n > mark:
            return fake.since(mark)
    return fake.since(mark)


def is_(method, chat=None, text=None):
    def f(c):
        if c["method"] != method:
            return False
        if chat is not None and int(c["params"].get("chat_id", 0)) != chat:
            return False
        if text is not None and text not in (c["params"].get("text") or c["params"].get("caption") or ""):
            return False
        return True
    return f


def buttons(msg):
    rm = msg.get("reply_markup") or {}
    return [b for row in rm.get("inline_keyboard", []) for b in row]


def btn(msg, text_part):
    for b in buttons(msg):
        if text_part in b["text"]:
            return b["callback_data"]
    raise AssertionError(f"кнопки «{text_part}» немає: {[b['text'] for b in buttons(msg)]}")


def current(c):
    """Актуальний стан повідомлення (після редагувань)."""
    r = c["result"]
    return fake.messages.get((r["chat"]["id"], r["message_id"]), r)


def img_size(c):
    key = "photo" if c["method"] == "sendPhoto" else "document"
    return Image.open(io.BytesIO(c["files"][key][1])).size


async def text_card(uid, text=TEXT):
    m = fake.mark()
    fake.send_text(uid, text)
    return await wait(m, lambda c: c["method"] in ("sendPhoto", "sendDocument", "sendMessage") and int(c["params"]["chat_id"]) == uid
                      and c["method"] != "sendMessage" or (is_("sendMessage", uid)(c) and "⚠️" in c["params"]["text"]), label="картка")


async def press(uid, msg, data):
    m = fake.mark()
    fake.press(uid, msg, data)
    await wait(m, is_("answerCallbackQuery"), label=f"answer {data}")
    return await settle(m)


async def menu(uid):
    m = fake.mark()
    fake.send_text(uid, "/menu")
    return current(await wait(m, is_("sendMessage", uid, "Налаштування"), label="меню"))


async def rec_of(cb, uid, c, timeout=5.0):
    """Запис «картинка → текст» з'являється, коли бот отримає відповідь на sendPhoto."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = cb.db.get_render(uid, c["result"]["message_id"])
        if r:
            return r
        await asyncio.sleep(0.02)
    return None


def jpeg(w, h, color=(20, 60, 110)):
    b = io.BytesIO()
    im = Image.new("RGB", (w, h), color)
    for x in range(0, w, 40):
        im.paste((200, 120, 40), (x, 0, x + 8, h))
    im.save(b, "JPEG", quality=90)
    return b.getvalue()


CH, CH2, CH3, CH4, GR = -1001111111111, -1002222222222, -1003333333333, -1004444444444, -1005555555555


def answers(calls):
    return [c for c in calls if c["method"] == "answerCallbackQuery"]


def to_chat(calls, cid, method="sendPhoto"):
    return [c for c in calls if c["method"] == method and int(c["params"]["chat_id"]) == cid]


def alert(calls):
    a = answers(calls)[-1]["params"]
    return a.get("text", ""), a.get("show_alert") in (True, "true", "True")


async def wait_file(cb, uid, c, timeout=5.0):
    """Бот записує file_id картинки одразу після відправки — чекаємо запис."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = cb.db.get_render(uid, c["result"]["message_id"])
        if r and r["file_id"]:
            return r
        await asyncio.sleep(0.02)
    return None


async def scenario_channel(cb, card_call, bm):
    fake.add_channel(CH, "Мій канал", "my_channel", admins={A})
    fake.add_channel(CH2, "Чужий канал", "foreign", bot="left", admins={A})
    fake.add_channel(CH3, "Канал без мене", "", admins=set())
    fake.add_channel(CH4, "Канал без прав", "noperm", bot_post=False, admins={A})
    fake.add_channel(GR, "Моя група", "my_group", ctype="supergroup", admins={A})
    card = card_call["result"]

    print("18. Канал не підключено: кнопки є, публікувати не можна")
    mn = await menu(A)
    check("не підключено" in mn["text"], "меню: канал не підключено")
    check("Опублікувати" in str(buttons(card)), "кнопка «Опублікувати» під листівкою")
    check("Опублікувати останню" in str(buttons(mn)), "кнопка «Опублікувати останню» в меню")
    calls = await press(A, card, "r:pub")
    text, shown = alert(calls)
    check(shown and "підключіть" in text and not to_chat(calls, CH), "без каналу — підказка, нічого не надіслано")
    calls = await press(A, mn, "p:last")
    check("підключіть" in alert(calls)[0], "те саме з меню")

    print("19. Підключення каналу: помилки й успіх")
    m = fake.mark()
    fake.send_text(A, "/channel")
    chm = current(await wait(m, is_("sendMessage", A, "Публікація в канал"), label="екран каналу"))
    await press(A, chm, btn(chm, "Підключити"))
    check(cb.db.state(A)[0] == "chan_connect", "чекаємо канал")

    async def say(text=None, fwd=None, user_fwd=False, expect=""):
        m = fake.mark()
        if fwd:
            fake.forward_from_channel(A, fwd)
        elif user_fwd:
            fake.forward_from_user(A)
        else:
            fake.send_text(A, text)
        await wait(m, is_("sendMessage", A, expect), label=expect)

    await say("щось не те", expect="Надішліть @username")
    await say(user_fwd=True, expect="не з каналу")
    await say(fwd=CH2, expect="не є адміністратором")
    await say("@nonexistent_channel", expect="Не знаходжу")
    await say(fwd=CH4, expect="немає права")
    await say(fwd=CH3, expect="Ви не адміністратор")
    check(cb.channel(A) is None and cb.db.state(A)[0] == "chan_connect", "після помилок канал не підключено, стан лишився")
    await say("@my_channel", expect="Підключено")
    check(cb.channel(A) and cb.channel(A)["id"] == CH and cb.db.state(A)[0] is None, "канал підключено за @username")

    print("20. Публікація кнопкою під листівкою")
    calls = await press(A, card, "r:pub")
    posts = to_chat(calls, CH)
    check(len(posts) == 1, "листівку надіслано в канал")
    check(posts and fake.files[posts[0]["params"]["photo"]] == card_call["files"]["photo"][1], "в канал пішла саме ця картинка")
    check(posts and not posts[0]["params"].get("caption"), "без підпису")
    check(any("Опубліковано" in c["params"].get("text", "") and "https://t.me/my_channel/" in c["params"]["text"]
              for c in calls if c["method"] == "sendMessage"), "користувач отримав посилання на пост")
    check(cb.db.get_render(A, card["message_id"])["published_to"] == CH, "листівку позначено опублікованою в цьому каналі")
    calls = await press(A, card, "r:pub")
    ask = [c for c in calls if is_("sendMessage", A, "ще раз")(c)]
    check(ask and not to_chat(calls, CH), "повторно: запитання «ще раз?», без нової публікації")
    calls = await press(A, current(ask[0]), btn(current(ask[0]), "Так"))
    check(len(to_chat(calls, CH)) == 1 and any(c["method"] == "deleteMessage" for c in calls),
          "після підтвердження: друга публікація, запитання прибрано")

    print("21. Меню → «Опублікувати останню»")
    c = await text_card(A, "Нова листівка для каналу, щоб перевірити публікацію з меню.")
    check(await wait_file(cb, A, c) is not None, "file_id збережено")
    mn = await menu(A)
    check("Мій канал" in mn["text"], "меню показує канал")
    calls = await press(A, mn, btn(mn, "Опублікувати останню"))
    posts = to_chat(calls, CH)
    check(len(posts) == 1 and fake.files[posts[0]["params"]["photo"]] == c["files"]["photo"][1],
          "опубліковано саме останню листівку")

    print("22. Листівка-файл публікується файлом")
    m = fake.mark()
    fake.press(A, c["result"], "r:file")
    d = await wait(m, is_("sendDocument", A))
    check(await wait_file(cb, A, d) is not None and cb.db.get_render(A, d["result"]["message_id"])["kind"] == "document",
          "для файлу збережено file_id і тип")
    calls = await press(A, d["result"], "r:pub")
    check(len(to_chat(calls, CH, "sendDocument")) == 1, "в канал пішов документ")

    print("23. Права змінилися: адмінство знято / бота видалено з каналу")
    # свіжа листівка: остання вже опублікована, і «Опублікувати останню» запитало б «ще раз?» замість перевірки прав
    c = await text_card(A, "Листівка для перевірки прав після змін у каналі.")
    await wait_file(cb, A, c)
    fake.channels[CH]["admins"].discard(A)
    calls = await press(A, mn, "p:last")
    check("більше не адміністратор" in alert(calls)[0] and not to_chat(calls, CH), "не адмін → відмова")
    fake.channels[CH]["admins"].add(A)
    fake.channels[CH]["bot"] = "left"
    calls = await press(A, mn, "p:last")
    check("не має доступу" in alert(calls)[0] and not to_chat(calls, CH), "бота видалено → зрозуміла відмова")
    fake.channels[CH]["bot"] = "administrator"
    calls = await press(A, mn, "p:last")
    check(len(to_chat(calls, CH)) == 1, "права повернулись → знову працює")

    print("24. Інший користувач не публікує у чужий канал")
    c = await text_card(B)
    await wait_file(cb, B, c)
    calls = await press(B, c["result"], "r:pub")
    check("підключіть" in alert(calls)[0] and not to_chat(calls, CH), "B без каналу не публікує")
    m = fake.mark()
    fake.send_text(B, "/channel")
    bch = current(await wait(m, is_("sendMessage", B, "Публікація в канал"), label="екран каналу B"))
    await press(B, bch, "i:chan_connect")
    m = fake.mark()
    fake.forward_from_channel(B, CH)
    await wait(m, is_("sendMessage", B, "Ви не адміністратор"), label="B не адмін каналу")
    check(cb.channel(B) is None and cb.channel(A)["id"] == CH, "B не підключив чужий канал, канал A не зачеплено")
    fake.send_text(B, "/cancel")

    print("25. Група за числовим ID, відключення, підключення пересланим постом з картинкою")
    await press(A, chm, "i:chan_connect")
    m = fake.mark()
    fake.send_text(A, str(GR))
    await wait(m, is_("sendMessage", A, "Підключено"), label="група")
    check(cb.channel(A)["id"] == GR and cb.channel(A)["type"] == "supergroup", "група підключена за ID")
    m = fake.mark()
    fake.send_text(A, "/channel")
    gm = current(await wait(m, is_("sendMessage", A, "Підключено (група)"), label="екран групи"))
    calls = await press(A, card, "r:pub")
    check(len(to_chat(calls, GR)) == 1 and not any("ще раз" in c["params"].get("text", "") for c in calls
                                                   if c["method"] == "sendMessage"),
          "листівка, раніше опублікована в канал, іде в нову групу без запитання")
    calls = await press(A, card, "r:pub")
    check(len(to_chat(calls, GR)) == 0 and any("ще раз" in c["params"].get("text", "") and "Моя група" in c["params"]["text"]
                                               for c in calls if c["method"] == "sendMessage"),
          "повторно в ту саму групу — запитання з її назвою")
    await press(A, gm, "c:off")
    check(cb.channel(A) is None, "відключено")
    await press(A, gm, "i:chan_connect")
    m = fake.mark()
    fake.forward_from_channel(A, CH, photo=True)
    await wait(m, is_("sendMessage", A, "Підключено"), label="підключення пересланим фото")
    check(cb.channel(A)["id"] == CH, "канал підключено пересланим постом із картинкою")

    print("26. Міграція старої бази (без колонок публікації)")
    import sqlite3
    from bot.db import DB
    old = DATA / "old.sqlite3"
    con = sqlite3.connect(old)
    con.executescript("CREATE TABLE renders (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                      "text TEXT NOT NULL, title TEXT, created REAL NOT NULL, PRIMARY KEY (chat_id, message_id));"
                      "INSERT INTO renders VALUES (1, 2, 3, 'старий текст', NULL, 1.0);")
    con.commit()
    con.close()
    db = DB(old)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(renders)")}
    check({"file_id", "kind", "published_to"} <= cols, "колонки додано")
    r = db.get_render(1, 2)
    check(r["text"] == "старий текст" and r["file_id"] is None and r["published_to"] == 0, "старі дані на місці")
    db.add_render(1, 3, 5, "новий", None)
    db.set_render_file(1, 3, "photo", "F1")
    check(db.last_publishable(5)["file_id"] == "F1" and db.last_publishable(3) is None, "нові запити працюють")
    DB(old)   # повторне відкриття не ламається
    check(True, "повторне відкриття без помилок")


async def scenario(app, cb):
    print("1. /start: перший користувач стає адміном, показується меню")
    m = fake.mark()
    fake.send_text(A, "/start")
    calls = await settle(m)
    texts = [c["params"].get("text", "") for c in calls if c["method"] == "sendMessage"]
    check(any("адміністратор" in t for t in texts), "повідомлення про адміна")
    check(any("Налаштування" in t for t in texts), "меню після /start")
    check(cb.is_admin(A), "A — адмін")
    check(cb.st(A)["title"] == "ЕНЕРГОЗАВАНТАЖЕННЯ" and cb.st(A)["size"] == "1280x782", "типові заголовок і розмір")

    print("2. Текст → листівка 1280×782")
    c = await text_card(A)
    check(c["method"] == "sendPhoto", "надіслано фото")
    check(img_size(c) == (1280, 782), f"розмір {img_size(c)}")
    check("🔄 Інший шаблон" in [b["text"] for b in buttons(c["result"])], "кнопки під листівкою")
    rec = await rec_of(cb, A, c)
    check(rec and rec["text"] == TEXT and rec["title"] is None, "текст листівки збережено")
    card = c["result"]
    card_call = c

    print("3. Меню → Розмір → Квадрат")
    mn = await menu(A)
    await press(A, mn, btn(mn, "Розмір"))
    mn = fake.messages[(A, mn["message_id"])]
    check("Розмір" in mn["text"], "екран розміру")
    await press(A, mn, btn(mn, "Квадрат"))
    check(cb.st(A)["size"] == "1080x1080", "розмір збережено")
    c = await text_card(A)
    check(img_size(c) == (1080, 1080), f"листівка {img_size(c)}")

    print("4. Свій розмір + помилка введення")
    mn = fake.messages[(A, mn["message_id"])]
    m = fake.mark()
    fake.press(A, mn, "i:size")
    await wait(m, is_("sendMessage", A, "ШИРИНАxВИСОТА"))
    m = fake.mark()
    fake.send_text(A, "абв")
    await wait(m, is_("sendMessage", A, "⚠️"))
    m = fake.mark()
    fake.send_text(A, "1500 x 900")
    await wait(m, is_("sendMessage", A, "1500×900"))
    check(cb.st(A)["size"] == "1500x900", "свій розмір")
    c = await text_card(A)
    check(img_size(c) == (1500, 900), f"листівка {img_size(c)}")
    await press(A, mn, "s:size:1280x782")

    print("5. Заголовок: змінити, сховати, повернути типовий")
    await press(A, mn, "m:title")
    mn = fake.messages[(A, mn["message_id"])]
    check("ЕНЕРГОЗАВАНТАЖЕННЯ" in mn["text"], "екран заголовка")
    m = fake.mark()
    fake.press(A, mn, btn(mn, "Змінити текст"))
    await wait(m, is_("sendMessage", A, "новий заголовок"))
    m = fake.mark()
    fake.send_text(A, "Світло  всередині")
    await wait(m, is_("sendMessage", A, "Світло всередині"))
    check(cb.st(A)["title"] == "Світло всередині", "заголовок змінено")
    await press(A, mn, "s:title_on")
    check(cb.st(A)["title_on"] is False, "заголовок сховано")
    c = await text_card(A)
    check(c["method"] == "sendPhoto", "листівка без заголовка")
    await press(A, mn, "s:title_reset")
    check(cb.st(A)["title"] == "ЕНЕРГОЗАВАНТАЖЕННЯ" and cb.st(A)["title_on"], "типовий заголовок повернуто")
    m = fake.mark()
    fake.send_text(A, "/title Ранкова практика")
    await wait(m, is_("sendMessage", A, "Ранкова практика"))
    check(cb.st(A)["title"] == "Ранкова практика", "/title з аргументом")
    await press(A, mn, "s:title_reset")

    print("6. Разовий заголовок рядком #")
    c = await text_card(A, "# Вечір\nДякую цьому дню за світло, тепло і людей поруч зі мною.")
    rec = await rec_of(cb, A, c)
    check(rec and rec["title"] == "Вечір" and rec["text"].startswith("Дякую"), "разовий заголовок")
    check(cb.st(A)["title"] == "ЕНЕРГОЗАВАНТАЖЕННЯ", "налаштування не змінились")

    print("7. Кнопки під листівкою")
    before = cb.st(A)["template"]
    m = fake.mark()
    fake.press(A, card, "r:next")
    c = await wait(m, is_("sendPhoto", A))
    check(cb.st(A)["template"] != before, f"наступний шаблон: {cb.st(A)['template']}")
    m = fake.mark()
    fake.press(A, card, "r:pick")
    pick = current(await wait(m, is_("sendMessage", A, "Шаблони")))
    data = btn(pick, "Нічне небо")
    check(data.startswith("tr:night_sky:"), "вибір шаблону з-під листівки")
    m = fake.mark()
    fake.press(A, pick, data)
    c = await wait(m, is_("sendPhoto", A))
    check(cb.st(A)["template"] == "night_sky", "шаблон вибрано й перемальовано")
    m = fake.mark()
    fake.press(A, card, "r:size")
    sz = current(await wait(m, is_("sendMessage", A, "розмірі")))
    m = fake.mark()
    fake.press(A, sz, btn(sz, "Сторіс"))
    c = await wait(m, is_("sendPhoto", A))
    check(img_size(c) == (1080, 1920), f"сторіс {img_size(c)}")
    m = fake.mark()
    fake.press(A, card, "r:file")
    c = await wait(m, is_("sendDocument", A))
    check(c["files"]["document"][0].endswith(".png") and c["files"]["document"][1][:4] == b"\x89PNG", "файлом PNG")
    await press(A, mn, "s:size:1280x782")

    print("8. Надсилати фото + файл JPEG; шрифт, колір, світіння")
    await press(A, mn, "m:send")
    await press(A, mn, "s:send:both")
    await press(A, mn, "s:fmt:jpg")
    await press(A, mn, "s:font:body:Comfortaa-Bold")
    await press(A, mn, "s:font:title:Lobster-Regular")
    await press(A, mn, "s:color:white")
    await press(A, mn, "s:glow")
    s = cb.st(A)
    check((s["send_as"], s["format"], s["font"], s["title_font"], s["color"], s["glow"]) ==
          ("both", "jpg", "Comfortaa-Bold", "Lobster-Regular", "white", False), "налаштування збережено")
    m = fake.mark()
    fake.send_text(A, TEXT)
    calls = await settle(m)
    ph = [c for c in calls if c["method"] == "sendPhoto"]
    dc = [c for c in calls if c["method"] == "sendDocument"]
    check(len(ph) == 1 and len(dc) == 1, "фото + файл")
    check(dc and dc[0]["files"]["document"][0].endswith(".jpg") and dc[0]["files"]["document"][1][:2] == b"\xff\xd8", "файл JPEG")
    check(dc and img_size(dc[0]) == (1280, 782), "файл 1280×782")
    await press(A, mn, "s:font:body:-")
    await press(A, mn, "s:color:-")
    await press(A, mn, "s:glow")
    await press(A, mn, "s:send:photo")

    print("9. Головне меню показує налаштування; приклади шаблонів")
    await press(A, mn, "m:main")
    mn = fake.messages[(A, mn["message_id"])]
    check("1280×782" in mn["text"] and "ЕНЕРГОЗАВАНТАЖЕННЯ" in mn["text"], "текст меню")
    m = fake.mark()
    fake.press(A, mn, "ex:0")
    c = await wait(m, is_("sendMediaGroup", A), timeout=30)
    check(len(c["params"]["media"]) == 10, f"альбом із {len(c['params']['media'])} прикладів на 1-й сторінці")
    m = fake.mark()
    fake.press(A, mn, "ex:1")
    c = await wait(m, is_("sendMediaGroup", A), timeout=30)
    check(len(c["params"]["media"]) == len(cb.store.all()) - 10, f"2-га сторінка: {len(c['params']['media'])} прикладів")
    names = {t.config["name"] for t in cb.store.all()}
    check({"Лимонне сонце", "Кораловий ранок", "Бірюзова лагуна", "Весняна зелень", "Медовий ранок", "Райдужне боке",
           "Помаранчевий світанок", "Квітуча вишня", "Блакитне небо", "Золотий полудень"} <= names
          and len(cb.store.all()) == 18, "10 нових яскравих шаблонів серед 18")

    print("10. Помилки тексту")
    m = fake.mark()
    fake.send_text(A, "слово " * 50)
    c = await wait(m, is_("sendMessage", A, "Забагато слів"))
    check(True, "забагато слів")
    c = await text_card(A, "Світло всередині мене 🌞 сяє щодня")
    check(c["method"] == "sendPhoto" and "⚠️" in (c["params"].get("caption") or ""), "попередження про емодзі")

    print("11. Новий шаблон із фото-файлу: назва → стиль → панель")
    n_before = len(cb.store.all())
    m = fake.mark()
    fake.send_photo(A, jpeg(1800, 1200), as_document=True)
    ask = current(await wait(m, is_("sendMessage", A, "Як назвати")))
    check(cb.db.state(A)[0] == "tpl_name", "чекаємо назву")
    m = fake.mark()
    fake.send_text(A, "Моє море")
    base = current(await wait(m, is_("sendMessage", A, "Стиль тексту")))
    await press(A, base, btn(base, "Морські промені"))
    base = fake.messages[(A, base["message_id"])]
    check("Фон" in base["text"], "екран обробки фону")
    m = fake.mark()
    fake.press(A, base, btn(base, "Темна панель"))
    c = await wait(m, is_("sendPhoto", A))
    ts = cb.store.all()
    new = [t for t in ts if t.config.get("name") == "Моє море"]
    check(len(ts) == n_before + 1 and new, "шаблон створено")
    if new:
        t = new[0]
        check(t.config.get("owner") == A and t.config["overlay"].get("panel"), "власник і панель")
        check(cb.st(A)["template"] == t.id, "новий шаблон вибрано")
        check(t.config["body"]["font"] == "PTSansNarrow-Bold", "стиль із «Морські промені»")
    check(cb.db.state(A)[0] is None and not cb._tmp_path(A).exists(), "стан і тимчасовий файл прибрано")

    print("12. Стиснуте фото + «Без назви»; скасування")
    m = fake.mark()
    fake.send_photo(A, jpeg(1280, 960, (230, 210, 240)))
    ask = current(await wait(m, is_("sendMessage", A, "стиснуте")))
    await press(A, ask, "n:noname")
    ask = fake.messages[(A, ask["message_id"])]
    await press(A, ask, btn(ask, "Старий папір"))
    ask = fake.messages[(A, ask["message_id"])]
    m = fake.mark()
    fake.press(A, ask, "n:ov:light")
    await wait(m, is_("sendPhoto", A))
    check(any(t.config.get("name") == "Моє фото" for t in cb.store.all()), "шаблон «Моє фото»")
    m = fake.mark()
    fake.send_photo(A, jpeg(800, 800))
    await wait(m, is_("sendMessage", A, "Як назвати"))
    m = fake.mark()
    fake.send_text(A, "/cancel")
    await wait(m, is_("sendMessage", A, "Скасовано"))
    check(cb.db.state(A)[0] is None and not cb._tmp_path(A).exists(), "/cancel")
    m = fake.mark()
    fake.send_photo(A, jpeg(200, 200), as_document=True)
    await wait(m, is_("sendMessage", A, "Як назвати"))
    fake.send_text(A, "Маленьке")
    base = current(await wait(m, is_("sendMessage", A, "Стиль тексту")))
    await press(A, base, "n:base:paper")
    m = fake.mark()
    fake.press(A, fake.messages[(A, base["message_id"])], "n:ov:none")
    c = await wait(m, lambda c: c["method"] == "editMessageText" and "Не вдалося" in c["params"]["text"])
    check("400" in c["params"]["text"], "замале фото відхилено")
    m = fake.mark()
    fake.send_photo(A, b"%PDF-1.4", as_document=True, mime="application/pdf")
    await wait(m, is_("sendMessage", A, "Це не зображення"))
    check(True, "не-зображення відхилено")

    print("13. Видалення власного шаблону")
    mn = await menu(A)
    await press(A, mn, "m:tm")
    mn = fake.messages[(A, mn["message_id"])]
    await press(A, mn, btn(mn, "Видалити"))
    mn = fake.messages[(A, mn["message_id"])]
    d = btn(mn, "Моє фото")
    await press(A, mn, d)
    mn = fake.messages[(A, mn["message_id"])]
    check("Видалити шаблон «Моє фото»" in mn["text"], "підтвердження")
    await press(A, mn, btn(mn, "Так"))
    check(not any(t.config.get("name") == "Моє фото" for t in cb.store.all()), "видалено")
    await press(A, mn, "dy:pink_glow")
    check(any(t.id == "pink_glow" for t in cb.store.all()), "вбудований не видаляється")

    print("14. Звичайний користувач: користується, але не додає шаблони")
    c = await text_card(B)
    check(c["method"] == "sendPhoto" and img_size(c) == (1280, 782), "B отримав листівку")
    m = fake.mark()
    fake.send_photo(B, jpeg(900, 900))
    await wait(m, is_("sendMessage", B, "лише адміністратори"))
    check(True, "B не може додати шаблон")
    bm = await menu(B)
    check("Адміністрування" not in str(buttons(bm)), "B не бачить адмін-меню")
    m = fake.mark()
    fake.press(B, bm, "a:mode")
    c = await wait(m, is_("answerCallbackQuery"))
    check(c["params"].get("show_alert") in (True, "true", "True"), "адмін-кнопка B відхилена")
    check(cb.access_mode() == "all", "доступ не змінено")

    print("15. Закритий доступ: запит → схвалення → відхилення")
    mn = await menu(A)
    await press(A, mn, "m:admin")
    mn = fake.messages[(A, mn["message_id"])]
    await press(A, mn, btn(mn, "Бот: усі"))
    check(cb.access_mode() == "allowed", "доступ лише дозволеним")
    m = fake.mark()
    fake.send_text(B, TEXT)
    calls = await settle(m)
    check(any(is_("sendMessage", B, "обмежено")(c) for c in calls), "B отримав відмову")
    req = [c for c in calls if is_("sendMessage", A, "Запит доступу")(c)]
    check(len(req) == 1 and not any(c["method"] == "sendPhoto" for c in calls), "адміна сповіщено, картки немає")
    m = fake.mark()
    fake.send_text(B, TEXT)
    calls = await settle(m)
    check(not any(is_("sendMessage", A, "Запит доступу")(c) for c in calls), "повторно не сповіщає")
    m = fake.mark()
    fake.press(A, current(req[0]), btn(current(req[0]), "Дозволити"))
    await wait(m, is_("sendMessage", B, "відкрито"))
    check("Дозволено" in fake.messages[(A, req[0]["result"]["message_id"])]["text"], "запит позначено")
    c = await text_card(B)
    check(c["method"] == "sendPhoto", "B після схвалення")
    await press(A, mn, "m:users")
    um = fake.messages[(A, mn["message_id"])]
    check("Користувачі" in um["text"] and f"a:no:{B}" in str(buttons(um)), "список користувачів")
    await press(A, um, f"a:no:{B}")
    m = fake.mark()
    fake.send_text(B, TEXT)
    await wait(m, is_("sendMessage", B, "закрито"))
    check(True, "B заборонено")
    m = fake.mark()
    fake.press(A, mn, "i:adduser")
    await wait(m, is_("sendMessage", A, "Telegram ID"))
    m = fake.mark()
    fake.send_text(A, str(B))
    await wait(m, is_("sendMessage", A, "дозволено"))
    c = await text_card(B)
    check(c["method"] == "sendPhoto", "B дозволено за ID")

    print("16. Шаблони можуть додавати всі; адмінство; типові налаштування")
    await press(A, mn, "m:admin")
    await press(A, mn, "a:addmode")
    check(cb.add_mode() == "all", "додавати можуть усі")
    m = fake.mark()
    fake.send_photo(B, jpeg(900, 900))
    await wait(m, is_("sendMessage", B, "Як назвати"))
    check(True, "B може додати шаблон")
    fake.send_text(B, "/cancel")
    await press(A, mn, f"a:adm:{B}")
    check(cb.is_admin(B), "B став адміном")
    await press(A, mn, f"a:adm:{B}")
    check(not cb.is_admin(B), "адмінство знято")
    await press(A, mn, "s:size:1080x1350")
    await press(A, mn, "a:defaults")
    await press(A, mn, "a:mode")
    check(cb.access_mode() == "all", "доступ знову для всіх")
    c = await text_card(C)
    check(img_size(c) == (1080, 1350), "новий користувач отримав типові налаштування адміна")

    print("17. Скидання й закриття меню")
    await press(B, bm, "s:reset")
    check(cb.st(B)["size"] == "1080x1350", "скидання до типових (адмінських)")
    m = fake.mark()
    fake.press(A, mn, "x")
    await wait(m, is_("deleteMessage", A))
    check(True, "меню закрито")

    await scenario_channel(cb, card_call, bm)

    print("27. Налаштування й канал переживають перезапуск")
    cb2 = M.CardBot(DATA)
    check(cb2.st(A) == cb.st(A) and cb2.is_admin(A) and cb2.add_mode() == "all", "SQLite зберіг усе")
    check(cb2.channel(A) == cb.channel(A) and cb2.channel(A)["id"] == CH, "підключений канал збережено")


async def amain():
    fake.start()
    app, cb = M.build_app(TOKEN, DATA, f"http://127.0.0.1:{PORT}/bot", f"http://127.0.0.1:{PORT}/file/bot")
    async with app:
        await M._post_init(app)
        await app.start()
        await app.updater.start_polling(poll_interval=0.0, timeout=1)
        try:
            await scenario(app, cb)
        except Exception:
            traceback.print_exc()
            failed.append("виняток у сценарії")
        finally:
            await app.updater.stop()
            await app.stop()
    fake.stop()
    errors = [c for c in fake.calls if c["method"] == "sendMessage" and "Щось пішло не так" in c["params"].get("text", "")]
    check(not errors, "жодних необроблених помилок у боті")


if __name__ == "__main__":
    t0 = time.time()
    asyncio.run(amain())
    print(f"\n{passed} ok, {len(failed)} fail за {time.time() - t0:.1f} с")
    if failed:
        print("Провалено:", *failed, sep="\n  - ")
        sys.exit(1)
