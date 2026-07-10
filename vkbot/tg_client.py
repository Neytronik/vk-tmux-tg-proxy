"""Telegram клиент через Telethon.

Используется как прокси: бот ВК показывает чаты и сообщения Telegram.
Все операции с Telegram идут через единый asyncio event loop в фоновом потоке.
"""
import os
import asyncio
import threading
from telethon import TelegramClient, functions, types
from telethon.errors import (
    PhoneNumberInvalidError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)


class TgClient:
    """Обёртка над Telethon для доступа к Telegram из синхронного кода."""

    def __init__(self, api_id, api_hash, session_file):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_file = os.path.expanduser(session_file)
        self.client = None
        self._ready = False       # Telegram авторизован
        self._login_state = None  # "need_phone" | "need_code" | "need_password" | "ready"

        # Единый event loop на всё время жизни клиента
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run(self, coro, timeout=30):
        """Выполнить корутину в event loop и вернуть результат."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ── Подключение / авторизация ──────────────────────────────

    def connect(self):
        """Подключить клиент. Возвращает состояние: need_phone / ready."""
        async def _connect():
            if self.client is None:
                self.client = TelegramClient(
                    self.session_file, self.api_id, self.api_hash,
                    use_ipv6=True,
                    timeout=20,
                )
            await self.client.connect()
            return await self.client.is_user_authorized()

        authorized = self._run(_connect())
        if authorized:
            self._ready = True
            self._login_state = "ready"
        else:
            self._login_state = "need_phone"
        return self._login_state

    def send_code(self, phone):
        """Отправить код на номер. Возвращает (ok, message)."""
        async def _send():
            if self.client is None:
                self.client = TelegramClient(
                    self.session_file, self.api_id, self.api_hash,
                    use_ipv6=True, timeout=20,
                )
                await self.client.connect()
            return await self.client.send_code_request(phone)

        try:
            result = self._run(_send())
            self._phone = phone
            self._phone_code_hash = result.phone_code_hash
            self._login_state = "need_code"
            return True, f"Код отправлен на {phone}"
        except PhoneNumberInvalidError:
            return False, "Неверный номер телефона"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def sign_in(self, code):
        """Войти с кодом. Возвращает (ok, message)."""
        async def _sign():
            return await self.client.sign_in(
                phone=self._phone,
                code=code,
                phone_code_hash=self._phone_code_hash,
            )

        try:
            self._run(_sign())
            self._ready = True
            self._login_state = "ready"
            return True, "✅ Вход выполнен!"
        except SessionPasswordNeededError:
            self._login_state = "need_password"
            return False, "Нужна двухфакторная аутентификация. Введите пароль:"
        except PhoneCodeInvalidError:
            return False, "Неверный код. Попробуйте ещё раз."
        except Exception as e:
            return False, f"Ошибка входа: {e}"

    def sign_in_password(self, password):
        """Войти с паролем 2FA. Возвращает (ok, message)."""
        async def _sign_pw():
            return await self.client.sign_in(password=password)

        try:
            self._run(_sign_pw())
            self._ready = True
            self._login_state = "ready"
            return True, "✅ Вход выполнен (2FA)!"
        except Exception as e:
            return False, f"Неверный пароль: {e}"

    @property
    def is_ready(self):
        return self._ready and self._login_state == "ready"

    # ── Чаты ───────────────────────────────────────────────────

    def get_dialogs(self, limit=100):
        """Получить список диалогов.

        Возвращает [(name, chat_id, unread, preview, kind), ...],
        где kind: 'user' | 'group' | 'channel'.
        """
        if not self.is_ready:
            return None, "Не подключен к Telegram"

        async def _get():
            dialogs = await self.client.get_dialogs(limit=limit)
            result = []
            for d in dialogs:
                name = d.name or "Без имени"
                if len(name) > 30:
                    name = name[:28] + "…"
                preview = ""
                if d.message and d.message.message:
                    preview = d.message.message[:50].replace("\n", " ")
                # Определяем тип чата
                if d.is_user:
                    kind = "user"
                elif d.is_channel and not d.is_group:
                    kind = "channel"
                else:
                    kind = "group"
                result.append((name, d.id, d.unread_count, preview, kind))
            return result

        return self._run(_get()), None

    def get_messages(self, chat_id, limit=10, topic_id=None):
        """Получить сообщения: [(msg_id, sender, text, date, is_out, media), ...].
        media — None или dict {kind: 'photo'|'file'|'video', name, size}."""
        if not self.is_ready:
            return None, "Не подключен к Telegram"

        async def _get():
            kwargs = {"limit": limit}
            if topic_id:
                kwargs["reply_to"] = topic_id
            messages = await self.client.get_messages(chat_id, **kwargs)
            result = []
            for m in reversed(messages):
                sender = "Вы" if m.out else "??"
                if not m.out and m.sender:
                    s = m.sender
                    sender = getattr(s, 'first_name', '') or str(s.id)
                text = (m.message or "")[:400]
                media = None
                if m.photo:
                    media = {"kind": "photo", "name": "фото", "size": 0}
                elif m.document:
                    mime = getattr(m.document, "mime_type", "") or ""
                    fname = (m.file.name if m.file and m.file.name else "файл")
                    size = getattr(m.file, "size", 0) if m.file else 0
                    if mime.startswith("video/"):
                        media = {"kind": "video", "name": fname or "видео", "size": size}
                    else:
                        media = {"kind": "file", "name": fname, "size": size}
                if not text and media:
                    text = ""  # текст-подпись может отсутствовать
                result.append((m.id, str(sender), text, m.date, m.out, media))
            return result

        return self._run(_get()), None

    def download_media(self, chat_id, msg_id, dest_dir="/tmp"):
        """Скачать медиа конкретного сообщения. Возвращает путь к файлу или None."""
        if not self.is_ready:
            return None

        async def _dl():
            msgs = await self.client.get_messages(chat_id, ids=msg_id)
            m = msgs if not isinstance(msgs, list) else (msgs[0] if msgs else None)
            if not m or not m.media:
                return None
            return await self.client.download_media(m, file=dest_dir)

        try:
            return self._run(_dl(), timeout=300)
        except Exception:
            return None

    def send_file(self, chat_id, file_path, caption="", topic_id=None):
        """Отправить файл/фото в чат/топик. Возвращает (ok, message)."""
        if not self.is_ready:
            return False, "Не подключен к Telegram"

        async def _send():
            kwargs = {"caption": caption or None}
            if topic_id:
                kwargs["reply_to"] = topic_id
            return await self.client.send_file(int(chat_id), file_path, **kwargs)

        try:
            self._run(_send(), timeout=300)
            return True, "✅ Отправлено"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def send_message(self, chat_id, text, topic_id=None):
        """Отправить сообщение в чат/топик. Возвращает (ok, message)."""
        if not self.is_ready:
            return False, "Не подключен к Telegram"

        async def _send():
            kwargs = {}
            if topic_id:
                kwargs["reply_to"] = topic_id
            return await self.client.send_message(int(chat_id), text, **kwargs)

        try:
            self._run(_send())
            return True, "✅ Отправлено"
        except Exception as e:
            return False, f"Ошибка: {e}"

    # ── Папки (dialog filters) ─────────────────────────────────

    def get_folders(self):
        """Список папок Telegram: [(folder_id, title, set(peer_id)), ...]."""
        if not self.is_ready:
            return None, "Не подключен к Telegram"

        async def _get():
            from telethon.utils import get_peer_id
            res = await self.client(functions.messages.GetDialogFiltersRequest())
            filters = getattr(res, "filters", res)  # новые слои: объект с .filters
            folders = []
            for f in filters:
                if isinstance(f, types.DialogFilterDefault):
                    continue
                title = getattr(f, "title", "")
                if hasattr(title, "text"):   # TextWithEntities в новых слоях
                    title = title.text
                fid = getattr(f, "id", 0)
                peers = set()
                for p in (list(getattr(f, "pinned_peers", []) or []) +
                          list(getattr(f, "include_peers", []) or [])):
                    try:
                        peers.add(get_peer_id(p))
                    except Exception:
                        pass
                folders.append((fid, str(title), peers))
            return folders

        try:
            return self._run(_get()), None
        except Exception as e:
            return None, f"Ошибка папок: {e}"

    # ── Топики форумов ─────────────────────────────────────────

    def is_forum(self, chat_id):
        """Проверить, является ли чат форумом (с топиками)."""
        if not self.is_ready:
            return False

        async def _get():
            e = await self.client.get_entity(chat_id)
            return bool(getattr(e, "forum", False))

        try:
            return self._run(_get())
        except Exception:
            return False

    def get_topics(self, chat_id, limit=100):
        """Список топиков форума: [(topic_id, title, unread_count), ...]."""
        if not self.is_ready:
            return None, "Не подключен к Telegram"

        async def _get():
            peer = await self.client.get_input_entity(chat_id)
            res = await self.client(functions.messages.GetForumTopicsRequest(
                peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=limit,
            ))
            topics = []
            for t in res.topics:
                if isinstance(t, types.ForumTopic):
                    topics.append((t.id, t.title, getattr(t, "unread_count", 0)))
            return topics

        try:
            return self._run(_get()), None
        except Exception as e:
            return None, f"Ошибка топиков: {e}"

    def mark_read(self, chat_id):
        """Отметить чат прочитанным (сбросить непрочитанные)."""
        if not self.is_ready:
            return

        async def _read():
            await self.client.send_read_acknowledge(int(chat_id))

        try:
            self._run(_read(), timeout=10)
        except Exception:
            pass

    def get_entity_name(self, chat_id):
        """Получить имя чата по ID."""
        if not self.is_ready:
            return str(chat_id)

        async def _get():
            entity = await self.client.get_entity(chat_id)
            return getattr(entity, 'first_name', None) or entity.title or str(chat_id)

        try:
            return self._run(_get())
        except Exception:
            return str(chat_id)

    def disconnect(self):
        """Отключить клиент."""
        self._ready = False
        async def _disc():
            if self.client:
                await self.client.disconnect()
        try:
            self._run(_disc(), timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
