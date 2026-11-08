"""Мінімальна імітація Telegram Bot API (лише те, що використовує бот) для наскрізних тестів без мережі."""
from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

BOT_USER = {"id": 999000, "is_bot": True, "first_name": "Card", "username": "card_test_bot"}


class FakeTelegram:
    def __init__(self, port: int):
        self.port = port
        self.updates: list[dict] = []
        self.calls: list[dict] = []          # усі виклики бота: {"method", "params", "files", "result"}
        self.files: dict[str, bytes] = {}
        self.messages: dict[tuple[int, int], dict] = {}
        self.channels: dict[int, dict] = {}   # id → {"title", "username", "type", "bot": status, "bot_post": bool, "admins": set}
        self._uid = itertools.count(1)
        self._mid = itertools.count(1000)
        self._fid = itertools.count(1)
        self.lock = threading.Lock()
        self.app = self._make_app()
        self.server = None

    # ---------------- сервер
    def _make_app(self) -> FastAPI:
        app = FastAPI()

        @app.api_route("/bot{token}/{method}", methods=["GET", "POST"])
        async def api(token: str, method: str, request: Request):
            params, files = {}, {}
            ctype = request.headers.get("content-type", "")
            if "json" in ctype:
                params = await request.json()
            elif ctype:
                form = await request.form()
                for k, v in form.multi_items():
                    if hasattr(v, "read"):
                        files[k] = (v.filename, await v.read())
                    else:
                        params[k] = v
            params.update(dict(request.query_params))
            for k in ("reply_markup", "media", "commands", "allowed_updates"):
                if isinstance(params.get(k), str):
                    try:
                        params[k] = json.loads(params[k])
                    except ValueError:
                        pass
            if method == "getUpdates":
                return JSONResponse({"ok": True, "result": await self._get_updates(params)})
            try:
                result = self._handle(method, params, files)
            except KeyError as e:
                return JSONResponse({"ok": False, "error_code": 400, "description": f"Bad Request: {e}"}, 400)
            except PermissionError as e:
                return JSONResponse({"ok": False, "error_code": 403, "description": f"Forbidden: {e}"}, 403)
            with self.lock:
                self.calls.append({"method": method, "params": params, "files": files, "result": result})
            return JSONResponse({"ok": True, "result": result})

        @app.get("/file/bot{token}/{path:path}")
        async def file(token: str, path: str):
            fid = path.split("/")[-1]
            return Response(self.files[fid], media_type="application/octet-stream")

        return app

    def start(self):
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        th = threading.Thread(target=self.server.run, daemon=True)
        th.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("fake telegram не стартував")

    def stop(self):
        if self.server:
            self.server.should_exit = True

    async def _get_updates(self, params):
        offset = int(params.get("offset") or 0)
        deadline = time.time() + min(float(params.get("timeout") or 0), 1.0)
        while True:
            with self.lock:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
                ready = list(self.updates)
            if ready or time.time() >= deadline:
                return ready
            await asyncio.sleep(0.02)

    # ---------------- методи
    def _msg(self, chat_id: int, extra: dict) -> dict:
        mid = next(self._mid)
        ch = self.channels.get(int(chat_id))
        chat = ({"id": int(chat_id), "type": ch["type"], "title": ch["title"],
                 **({"username": ch["username"]} if ch["username"] else {})}
                if ch else {"id": int(chat_id), "type": "private"})
        m = {"message_id": mid, "date": int(time.time()), "chat": chat, "from": BOT_USER, **extra}
        self.messages[(int(chat_id), mid)] = m
        return m

    # ---------------- канали й групи
    def add_channel(self, cid: int, title: str, username: str = "", ctype: str = "channel", bot: str = "administrator",
                    bot_post: bool = True, admins=()):
        self.channels[cid] = {"title": title, "username": username, "type": ctype, "bot": bot, "bot_post": bot_post,
                              "admins": set(admins)}

    def _find_channel(self, ref) -> tuple[int, dict]:
        for cid, ch in self.channels.items():
            if str(ref) == str(cid) or (isinstance(ref, str) and ref.startswith("@") and ch["username"]
                                        and ref[1:].lower() == ch["username"].lower()):
                return cid, ch
        raise KeyError("chat not found")

    def _member(self, cid: int, ch: dict, uid: int) -> dict:
        user = BOT_USER if uid == BOT_USER["id"] else self._user(uid, "Тест")
        if uid == BOT_USER["id"]:
            status, post = ch["bot"], ch["bot_post"]
        else:
            status, post = ("administrator" if uid in ch["admins"] else "member"), True
        if status == "administrator":
            return {"status": "administrator", "user": user, "can_be_edited": False, "is_anonymous": False,
                    "can_manage_chat": True, "can_delete_messages": True, "can_manage_video_chats": True,
                    "can_restrict_members": True, "can_promote_members": False, "can_change_info": True,
                    "can_invite_users": True, "can_post_stories": False, "can_edit_stories": False,
                    "can_delete_stories": False, **({"can_post_messages": True} if post else {"can_post_messages": False})}
        if status == "creator":
            return {"status": "creator", "user": user, "is_anonymous": False}
        if status == "left":
            return {"status": "left", "user": user}
        return {"status": "member", "user": user}

    def _file(self, data: bytes) -> str:
        fid = f"F{next(self._fid)}"
        self.files[fid] = data
        return fid

    def _handle(self, method: str, p: dict, files: dict):
        chat = p.get("chat_id")
        rm = {"reply_markup": p["reply_markup"]} if p.get("reply_markup") else {}
        if method == "getMe":
            return BOT_USER
        if method in ("deleteWebhook", "setMyCommands", "sendChatAction", "answerCallbackQuery"):
            return True
        if method == "sendMessage":
            return self._msg(chat, {"text": p["text"], **rm})
        if method == "editMessageText":
            m = self.messages[(int(chat), int(p["message_id"]))]
            m.update(text=p["text"], **rm)
            if not rm:
                m.pop("reply_markup", None)
            return m
        if method == "editMessageReplyMarkup":
            m = self.messages[(int(chat), int(p["message_id"]))]
            m.pop("reply_markup", None)
            m.update(rm)
            return m
        if method == "deleteMessage":
            self.messages.pop((int(chat), int(p["message_id"])), None)
            return True
        if method == "getChat":
            cid, ch = self._find_channel(p["chat_id"])
            # справжній getChat повертає ChatFullInfo: accent_color_id і max_reaction_count є завжди
            return {"id": cid, "type": ch["type"], "title": ch["title"], "accent_color_id": 0, "max_reaction_count": 11,
                    **({"username": ch["username"]} if ch["username"] else {})}
        if method == "getChatMember":
            cid, ch = self._find_channel(p["chat_id"])
            return self._member(cid, ch, int(p["user_id"]))
        if method in ("sendPhoto", "sendDocument"):
            key = "photo" if method == "sendPhoto" else "document"
            if key in files:
                name, data = files[key]
                fid = self._file(data)
            else:                       # повторне надсилання за file_id
                fid, name = p[key], "reused"
                if fid not in self.files:
                    raise KeyError("wrong file identifier")
            if int(chat) in self.channels:
                ch = self.channels[int(chat)]
                if ch["bot"] != "administrator" or (ch["type"] == "channel" and not ch["bot_post"]):
                    raise PermissionError("bot is not allowed to post")
            if key == "photo":
                return self._msg(chat, {"photo": [{"file_id": fid, "file_unique_id": "u" + fid, "width": 90, "height": 90}],
                                        **({"caption": p["caption"]} if p.get("caption") else {}), **rm})
            return self._msg(chat, {"document": {"file_id": fid, "file_unique_id": "u" + fid, "file_name": name},
                                    **({"caption": p["caption"]} if p.get("caption") else {}), **rm})
        if method == "sendMediaGroup":
            out = []
            for item in p["media"]:
                ref = item["media"].removeprefix("attach://")
                fid = self._file(files[ref][1])
                out.append(self._msg(chat, {"photo": [{"file_id": fid, "file_unique_id": "u" + fid, "width": 90, "height": 90}],
                                            "caption": item.get("caption", "")}))
            return out
        if method == "getFile":
            fid = p["file_id"]
            return {"file_id": fid, "file_unique_id": "u" + fid, "file_size": len(self.files[fid]), "file_path": f"docs/{fid}"}
        raise KeyError(f"method {method} not implemented")

    # ---------------- дії «користувача»
    def _user(self, uid: int, name: str):
        return {"id": uid, "is_bot": False, "first_name": name, "username": f"user{uid}"}

    def push(self, upd: dict):
        with self.lock:
            upd["update_id"] = next(self._uid)
            self.updates.append(upd)

    def send_text(self, uid: int, text: str, name: str = "Тест"):
        msg = {"message_id": next(self._mid), "date": int(time.time()), "chat": {"id": uid, "type": "private", "first_name": name},
               "from": self._user(uid, name), "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        self.push({"message": msg})

    def forward_from_channel(self, uid: int, cid: int, text: str = "пост із каналу", name: str = "Тест", photo: bool = False):
        """Користувач пересилає боту пост із каналу cid (текстовий або з картинкою)."""
        ch = self.channels.get(cid, {"title": "?", "username": ""})
        msg = {"message_id": next(self._mid), "date": int(time.time()), "chat": {"id": uid, "type": "private", "first_name": name},
               "from": self._user(uid, name),
               "forward_origin": {"type": "channel", "date": int(time.time()), "message_id": 5,
                                  "chat": {"id": cid, "type": "channel", "title": ch["title"],
                                           **({"username": ch["username"]} if ch["username"] else {})}}}
        if photo:
            fid = self._file(b"fake-photo")
            msg["photo"] = [{"file_id": fid, "file_unique_id": "u" + fid, "width": 100, "height": 100, "file_size": 10}]
        else:
            msg["text"] = text
        self.push({"message": msg})

    def forward_from_user(self, uid: int, text: str = "привіт", name: str = "Тест"):
        msg = {"message_id": next(self._mid), "date": int(time.time()), "chat": {"id": uid, "type": "private", "first_name": name},
               "from": self._user(uid, name), "text": text,
               "forward_origin": {"type": "user", "date": int(time.time()), "sender_user": self._user(777, "Хтось")}}
        self.push({"message": msg})

    def send_photo(self, uid: int, data: bytes, as_document=False, mime="image/jpeg", name="Тест"):
        fid = self._file(data)
        msg = {"message_id": next(self._mid), "date": int(time.time()), "chat": {"id": uid, "type": "private"},
               "from": self._user(uid, name)}
        if as_document:
            msg["document"] = {"file_id": fid, "file_unique_id": "u" + fid, "file_name": "photo.jpg", "mime_type": mime, "file_size": len(data)}
        else:
            msg["photo"] = [{"file_id": fid, "file_unique_id": "u" + fid, "width": 1280, "height": 960, "file_size": len(data)}]
        self.push({"message": msg})

    def press(self, uid: int, message: dict, data: str, name: str = "Тест"):
        self.push({"callback_query": {"id": str(time.time_ns()), "from": self._user(uid, name), "chat_instance": "ci",
                                      "data": data, "message": message}})

    # ---------------- перевірки
    def since(self, n: int) -> list[dict]:
        with self.lock:
            return self.calls[n:]

    def mark(self) -> int:
        with self.lock:
            return len(self.calls)
