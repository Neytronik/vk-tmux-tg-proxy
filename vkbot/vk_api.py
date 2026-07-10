"""Обёртка для VK API.

Поддерживает:
- Отправку сообщений с клавиатурой
- Редактирование сообщений (для watch mode)
- Long Poll для приёма событий
- Рейт-лимитинг (потокобезопасный)
- Валидацию токена
"""
import json
import time
import random
import threading
import requests

API_VERSION = "5.199"
API_BASE = "https://api.vk.com/method"


class VkApiError(Exception):
    """Ошибка VK API."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"VK API error {code}: {message}")


class VkApi:
    """Клиент для VK API."""

    def __init__(self, token, rate_limit_delay=0.4):
        self.token = token
        self.rate_limit_delay = rate_limit_delay
        self._last_call = 0
        self._lock = threading.Lock()

    def _rate_limit(self):
        """Выдержать паузу между запросами (потокобезопасно)."""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self.rate_limit_delay:
                time.sleep(self.rate_limit_delay - elapsed)
            self._last_call = time.time()

    def _call(self, method, params=None):
        """Вызвать метод VK API."""
        self._rate_limit()

        url = f"{API_BASE}/{method}"
        data = {
            "access_token": self.token,
            "v": API_VERSION,
        }
        if params:
            data.update(params)

        try:
            resp = requests.post(url, data=data, timeout=30)
            resp.raise_for_status()
            result = resp.json()
        except requests.RequestException as e:
            raise VkApiError(-1, f"Сетевая ошибка: {e}")
        except json.JSONDecodeError:
            raise VkApiError(-1, f"Некорректный ответ: {resp.text[:200]}")

        if "error" in result:
            err = result["error"]
            raise VkApiError(err.get("error_code", -1), err.get("error_msg", "Unknown"))

        return result.get("response", result)

    def validate_token(self):
        """Проверить валидность токена через запрос информации о группе.
        Возвращает (ok, info_string).
        """
        try:
            # Пробуем получить информацию о токене
            resp = self._call("groups.getById", {"fields": "name"})
            if resp and isinstance(resp, list) and len(resp) > 0:
                group = resp[0]
                name = group.get("name", "неизвестно")
                gid = group.get("id", "?")
                return True, f"✅ Токен валиден. Группа: {name} (id={gid})"
            return True, "✅ Токен валиден"
        except VkApiError as e:
            return False, f"❌ Ошибка VK API [{e.code}]: {e.message}"
        except Exception as e:
            return False, f"❌ Ошибка проверки: {e}"

    # ── Long Poll ──────────────────────────────────────────────

    def get_long_poll_server(self, group_id):
        """Получить сервер для Long Poll."""
        return self._call("groups.getLongPollServer", {"group_id": group_id})

    def long_poll(self, server, key, ts, wait=25):
        """Один цикл опроса Long Poll.
        Возвращает (events, new_ts) или (None, fail_code) при ошибке.
        """
        url = server
        params = {
            "act": "a_check",
            "key": key,
            "ts": ts,
            "wait": wait,
        }
        try:
            resp = requests.get(url, params=params, timeout=wait + 10)
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            return [], ts  # Сетевая ошибка — просто пробуем снова

        if "failed" in data:
            return None, data["failed"]  # Сервер требует переподключения

        return data.get("updates", []), data.get("ts", ts)

    # ── Сообщения ──────────────────────────────────────────────

    def send_message(self, peer_id, message, keyboard=None, attachment=None):
        """Отправить сообщение. Возвращает message_id (int) или 0 при ошибке."""
        params = {
            "peer_id": peer_id,
            "message": message[:4096],
            "random_id": random.randint(-2147483648, 2147483647),
            "dont_parse_links": 0,
        }
        if keyboard:
            params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
        if attachment:
            params["attachment"] = attachment

        result = self._call("messages.send", params)

        # messages.send возвращает response в одном из форматов:
        # - целое число (message_id) — основной случай
        # - dict с ключом message_id (при некоторых настройках группы)
        # - список из dict'ов (при массовой рассылке, не наш случай)
        if isinstance(result, int):
            return result
        if isinstance(result, dict):
            return result.get("message_id", 0)
        if isinstance(result, list) and result:
            return result[0].get("message_id", 0) if isinstance(result[0], dict) else 0
        return 0

    def get_audio_transcript(self, message_id):
        """Вернуть (state, text) расшифровки голосового в сообщении.
        state: 'done' | 'in_progress' | None. Использует встроенную
        речь-в-текст ВКонтакте (никакого своего STT)."""
        try:
            res = self._call("messages.getById", {"message_ids": message_id})
            items = res.get("items", []) if isinstance(res, dict) else []
            for it in items:
                for att in it.get("attachments", []):
                    if att.get("type") == "audio_message":
                        am = att.get("audio_message", {})
                        return am.get("transcript_state"), am.get("transcript")
        except Exception:
            pass
        return None, None

    def edit_message(self, peer_id, message_id, message, keyboard=None):
        """Редактировать сообщение (для watch mode)."""
        params = {
            "peer_id": peer_id,
            "message_id": message_id,
            "message": message[:4096],
            "dont_parse_links": 0,
        }
        if keyboard:
            params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
        return self._call("messages.edit", params)

    def delete_message(self, peer_id, message_id=None, cmid=None):
        """Удалить сообщение.

        message_id — глобальный id (для наших отправленных сообщений).
        cmid — conversation_message_id (для входящих сообщений юзера).
        """
        params = {
            "peer_id": peer_id,
            "delete_for_all": 1,
        }
        if cmid is not None:
            params["cmids"] = cmid
        else:
            params["message_ids"] = message_id
        return self._call("messages.delete", params)

    def set_typing(self, peer_id):
        """Показать индикатор «… печатает» (живёт ~10с или до отправки сообщения).
        Заменяет спам «Загружаю…» на нативную реакцию."""
        try:
            return self._call("messages.setActivity", {"peer_id": peer_id, "type": "typing"})
        except Exception:
            return None

    def send_message_event_answer(self, event_id, user_id, peer_id, event_data=None):
        """Ответить на callback-событие (inline keyboard)."""
        params = {
            "event_id": event_id,
            "user_id": user_id,
            "peer_id": peer_id,
        }
        if event_data:
            params["event_data"] = json.dumps(event_data, ensure_ascii=False)
        return self._call("messages.sendMessageEventAnswer", params)

    # ── Информация ─────────────────────────────────────────────

    def get_user_info(self, user_ids):
        """Получить информацию о пользователях."""
        if isinstance(user_ids, (list, tuple)):
            user_ids = ",".join(str(uid) for uid in user_ids)
        return self._call("users.get", {"user_ids": user_ids})

    # ── Загрузка медиа ─────────────────────────────────────────

    @staticmethod
    def _guard_file(file_path):
        """Убедиться, что файл существует и не пустой (иначе VK вернёт битый ответ)."""
        import os
        if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) < 16:
            raise VkApiError(-1, "Пустой или отсутствующий файл")

    def upload_photo(self, peer_id, file_path):
        """Загрузить фото для сообщения. Возвращает строку вложения photo{owner}_{id}."""
        self._guard_file(file_path)
        # 1. Сервер загрузки
        server = self._call("photos.getMessagesUploadServer", {"peer_id": peer_id})
        upload_url = server["upload_url"]
        # 2. Загрузка файла
        with open(file_path, "rb") as f:
            resp = requests.post(upload_url, files={"photo": f}, timeout=120)
        up = resp.json()
        if not up.get("photo") or up.get("photo") == "[]":
            raise VkApiError(-1, f"Сервер VK не принял фото: {str(up)[:150]}")
        # 3. Сохранение
        saved = self._call("photos.saveMessagesPhoto", {
            "photo": up["photo"], "server": up["server"], "hash": up["hash"],
        })
        item = saved[0] if isinstance(saved, list) else saved
        return f"photo{item['owner_id']}_{item['id']}"

    def upload_doc(self, peer_id, file_path, title=None):
        """Загрузить документ/файл для сообщения. Возвращает doc{owner}_{id}."""
        import os
        self._guard_file(file_path)
        title = title or os.path.basename(file_path)
        server = self._call("docs.getMessagesUploadServer", {"type": "doc", "peer_id": peer_id})
        upload_url = server["upload_url"]
        with open(file_path, "rb") as f:
            resp = requests.post(upload_url, files={"file": (title, f)}, timeout=300)
        up = resp.json()
        if not up.get("file"):
            raise VkApiError(-1, f"Сервер VK не принял файл: {str(up)[:150]}")
        saved = self._call("docs.save", {"file": up["file"], "title": title})
        # docs.save возвращает {type, doc:{owner_id,id}} или {doc:...}
        doc = saved.get("doc") if isinstance(saved, dict) else None
        if not doc and isinstance(saved, list):
            doc = saved[0].get("doc", saved[0])
        return f"doc{doc['owner_id']}_{doc['id']}"

    def upload_video(self, peer_id, file_path, title=None):
        """Загрузить видео (в т.ч. кружок) — VK покажет проигрываемым.
        Возвращает строку вложения video{owner}_{id} или None при неудаче."""
        import os
        title = title or os.path.basename(file_path)
        try:
            self._guard_file(file_path)
            # 1. Адрес для загрузки (group-токен: видео уходит в сообщество)
            save = self._call("video.save", {"name": title[:128], "is_private": 1,
                                             "wallpost": 0})
            upload_url = save["upload_url"]
            owner_id = save.get("owner_id")
            vid = save.get("video_id")
            if not upload_url or owner_id is None or vid is None:
                return None
            # 2. Заливаем файл (проверяем, что сервер принял)
            with open(file_path, "rb") as f:
                r = requests.post(upload_url, files={"video_file": (title, f)}, timeout=300)
            try:
                if r.json().get("size") == 0:
                    return None
            except Exception:
                pass  # некоторые ответы video без JSON — не критично
            return f"video{owner_id}_{vid}"
        except Exception:
            return None

    def upload_voice(self, peer_id, file_path):
        """Загрузить голосовое сообщение (VK покажет как voice). Возвращает doc{owner}_{id}.
        Файл должен быть .ogg (opus) — как отдаёт Telegram."""
        self._guard_file(file_path)
        server = self._call("docs.getMessagesUploadServer",
                            {"type": "audio_message", "peer_id": peer_id})
        upload_url = server["upload_url"]
        with open(file_path, "rb") as f:
            resp = requests.post(upload_url, files={"file": ("voice.ogg", f)}, timeout=120)
        up = resp.json()
        if not up.get("file"):
            raise VkApiError(-1, f"Сервер VK не принял голосовое: {str(up)[:150]}")
        saved = self._call("docs.save", {"file": up["file"]})
        doc = saved.get("audio_message") or saved.get("doc") if isinstance(saved, dict) else None
        if not doc and isinstance(saved, list):
            doc = saved[0].get("doc", saved[0])
        return f"doc{doc['owner_id']}_{doc['id']}"


# ── Клавиатуры ──────────────────────────────────────────────────

# Константы цветов кнопок VK
PRIMARY = "primary"       # синий
SECONDARY = "secondary"   # белый/серый
POSITIVE = "positive"     # зелёный
NEGATIVE = "negative"     # красный


def make_keyboard(buttons, one_time=False, inline=False):
    """Создать объект клавиатуры VK.

    buttons — список рядов, каждый ряд — список кнопок.
    Каждая кнопка — dict с: label, color, payload (опционально).
    """
    rows = []
    for row in buttons:
        btn_row = []
        for btn in row:
            action = {
                "type": "text",
                "label": btn["label"],
            }
            if btn.get("payload"):
                action["payload"] = json.dumps(
                    {"cmd": btn["payload"]}, ensure_ascii=False
                )
            vk_btn = {
                "action": action,
                "color": btn.get("color", PRIMARY),
            }
            btn_row.append(vk_btn)
        rows.append(btn_row)

    keyboard = {
        "one_time": one_time,
        "buttons": rows,
    }
    if inline:
        keyboard["inline"] = True
    return keyboard


def make_tg_menu_keyboard():
    """Меню для обычных пользователей — только Telegram."""
    return make_keyboard([
        [
            {"label": "✈️ Открыть Telegram", "color": PRIMARY, "payload": "/tg"},
        ],
        [
            {"label": "🔔 Уведомления", "color": SECONDARY, "payload": "/tg watch"},
            {"label": "🔀 Аккаунт", "color": SECONDARY, "payload": "/tg accounts"},
        ],
        [
            {"label": "🔑 Войти", "color": SECONDARY, "payload": "/tg login"},
            {"label": "ℹ️ Помощь", "color": SECONDARY, "payload": "/help"},
        ],
    ], one_time=False)


def make_main_keyboard(session_name=None, is_admin=False, has_projects=False):
    """Главная клавиатура — зависит от того, есть ли активная сессия."""
    if session_name:
        rows = [
            [
                {"label": "📺 Вывод", "color": PRIMARY, "payload": "/o"},
                {"label": "👁 Следить", "color": PRIMARY, "payload": "/watch"},
            ],
            [
                {"label": "📝 Команда", "color": SECONDARY, "payload": "/s"},
                {"label": "⏎ Enter", "color": SECONDARY, "payload": "/e"},
            ],
            [
                {"label": "⛔ Ctrl+C", "color": NEGATIVE, "payload": "/c"},
                {"label": "🚪 Ctrl+D", "color": NEGATIVE, "payload": "/d"},
            ],
            [
                {"label": "🤖 Claude", "color": POSITIVE, "payload": "/claude"},
                {"label": "🧠 DeepClaude", "color": POSITIVE, "payload": "/dcc"},
            ],
        ]
        if has_projects:
            rows.append([{"label": "📂 Мои проекты", "color": PRIMARY, "payload": "/projects"}])
        rows += [
            [
                {"label": "✈️ Telegram", "color": PRIMARY, "payload": "/tg"},
                {"label": "🔄 Сессии", "color": SECONDARY, "payload": "/ls"},
            ],
            [
                {"label": "🔌 Откл.", "color": SECONDARY, "payload": "/detach"},
                {"label": "🗑 Удалить", "color": NEGATIVE, "payload": "/kill"},
            ],
        ]
        return make_keyboard(rows, one_time=False)
    else:
        rows = [
            [
                {"label": "✈️ Telegram", "color": PRIMARY, "payload": "/tg"},
                {"label": "🤖 Claude", "color": POSITIVE, "payload": "/claude"},
            ],
            [
                {"label": "📋 Сессии", "color": SECONDARY, "payload": "/ls"},
                {"label": "➕ Новая", "color": POSITIVE, "payload": "/new"},
            ],
        ]
        if has_projects:
            rows.append([{"label": "📂 Мои проекты", "color": PRIMARY, "payload": "/projects"}])
        rows.append([
            {"label": "⏰ Отложить", "color": SECONDARY, "payload": "/in"},
            {"label": "ℹ️ Помощь", "color": SECONDARY, "payload": "/help"},
        ])
        if is_admin:
            rows.append([{"label": "👑 Админка", "color": SECONDARY, "payload": "/admin"}])
        return make_keyboard(rows, one_time=False)


def make_sessions_keyboard(sessions, current=None):
    """Клавиатура со списком сессий для подключения."""
    buttons = []
    row = []
    for s in sessions:
        marker = "▶ " if s == current else ""
        row.append({
            "label": f"{marker}{s}",
            "color": POSITIVE if s == current else PRIMARY,
            "payload": f"/attach {s}",
        })
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Кнопки действий + навигация (всегда есть выход)
    if sessions:
        buttons.append([
            {"label": "➕ Новая", "color": POSITIVE, "payload": "/new"},
            {"label": "🗑 Удалить", "color": NEGATIVE, "payload": "/kill"},
        ])
    else:
        buttons.append([
            {"label": "➕ Новая", "color": POSITIVE, "payload": "/new"},
            {"label": "🤖 Claude", "color": POSITIVE, "payload": "/claude"},
        ])
    buttons.append([
        {"label": "✈️ Telegram", "color": PRIMARY, "payload": "/tg"},
        {"label": "🏠 Меню", "color": PRIMARY, "payload": "/menu"},
    ])
    return make_keyboard(buttons, one_time=False)


def make_kill_keyboard(sessions):
    """Клавиатура для выбора сессии на удаление (с выходом — без тупика)."""
    buttons = []
    row = []
    for s in sessions:
        row.append({
            "label": f"🗑 {s}",
            "color": NEGATIVE,
            "payload": f"/kill {s}",
        })
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    # Всегда есть выход
    buttons.append([
        {"label": "🔄 Сессии", "color": SECONDARY, "payload": "/ls"},
        {"label": "🏠 Меню", "color": PRIMARY, "payload": "/menu"},
    ])
    return make_keyboard(buttons, one_time=False)


def make_watch_keyboard():
    """Пульт управления сессией/Claude Code: стрелки, спецклавиши, навигация.
    Показывается во время watch — когда вы «внутри» сессии."""
    return make_keyboard([
        [
            {"label": "⬆️", "color": SECONDARY, "payload": "/up"},
            {"label": "⏎ Enter", "color": POSITIVE, "payload": "/e"},
            {"label": "⎋ Esc", "color": SECONDARY, "payload": "/esc"},
        ],
        [
            {"label": "⬅️", "color": SECONDARY, "payload": "/left"},
            {"label": "⬇️", "color": SECONDARY, "payload": "/down"},
            {"label": "➡️", "color": SECONDARY, "payload": "/right"},
        ],
        [
            {"label": "⇥ Tab", "color": SECONDARY, "payload": "/tab"},
            {"label": "⇧⇥ Shift+Tab", "color": SECONDARY, "payload": "/btab"},
            {"label": "⛔ Ctrl+C", "color": NEGATIVE, "payload": "/c"},
        ],
        [
            {"label": "🔄 Обновить", "color": PRIMARY, "payload": "/o"},
            {"label": "🏠 Меню", "color": PRIMARY, "payload": "/menu"},
            {"label": "🛑 Стоп", "color": NEGATIVE, "payload": "/unwatch"},
        ],
    ], one_time=False)


def make_notify_keyboard(current_state):
    """Клавиатура для настройки уведомлений об ошибках."""
    on_color = POSITIVE if current_state else SECONDARY
    off_color = NEGATIVE if not current_state else SECONDARY
    on_label = "🔔 ВКЛ" if current_state else "🔔 Вкл"
    off_label = "🔕 ВЫКЛ" if not current_state else "🔕 Выкл"

    return make_keyboard([
        [
            {"label": on_label, "color": on_color, "payload": "/notify on"},
            {"label": off_label, "color": off_color, "payload": "/notify off"},
        ],
        [{"label": "🏠 Меню", "color": PRIMARY, "payload": "/menu"}],
    ], one_time=False)
