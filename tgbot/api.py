"""Обёртка Telegram Bot API (long polling).

Поддерживает:
- getUpdates (long poll) — приём сообщений и нажатий inline-кнопок
- sendMessage / editMessageText / deleteMessage — с inline-клавиатурой
- answerCallbackQuery, sendChatAction (typing)
- HTML parse_mode + <pre> для моноширинного вывода TUI
"""
import html
import json
import time
import threading
import requests


class TgBotError(Exception):
    def __init__(self, code, description):
        self.code = code
        self.description = description
        super().__init__(f"Telegram Bot error {code}: {description}")


class TgBotApi:
    """Клиент Telegram Bot API."""

    def __init__(self, token, rate_limit_delay=0.05):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.rate_limit_delay = rate_limit_delay
        self._last = 0
        self._lock = threading.Lock()
        # Постоянная сессия с пулом соединений — переиспользуем TCP/TLS,
        # чтобы не платить за новый connect на каждый запрос.
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
        self._session.mount("https://", adapter)

    def _rate_limit(self):
        with self._lock:
            now = time.time()
            gap = now - self._last
            if gap < self.rate_limit_delay:
                time.sleep(self.rate_limit_delay - gap)
            self._last = time.time()

    def _call(self, method, params=None, timeout=20, _retry=True):
        self._rate_limit()
        # (connect, read): короткий connect, чтобы перемежающаяся блокировка
        # Telegram не вешала запрос на 35с; read — под длинный опрос.
        to = (8, timeout)
        # Ретраим ТОЛЬКО быстрые сбои соединения (connect/пул) — повтор часто
        # выручает при флапе Telegram. На read-таймаут не ретраим: мы уже
        # прождали полный timeout, повтор просто утроит ожидание.
        last_err = None
        for attempt in range(3):
            try:
                resp = self._session.post(f"{self.base}/{method}", data=params or {}, timeout=to)
                data = resp.json()
                break
            except json.JSONDecodeError:
                raise TgBotError(-1, "Некорректный ответ")
            except (requests.exceptions.ConnectTimeout,
                    requests.exceptions.ConnectionError) as e:
                last_err = e
                time.sleep(0.6 * (attempt + 1))
            except requests.RequestException as e:
                # read-timeout и прочее — без повторов
                raise TgBotError(-1, f"Сеть: {e}")
        else:
            raise TgBotError(-1, f"Сеть: {last_err}")

        if not data.get("ok"):
            code = data.get("error_code", -1)
            # 429 Too Many Requests — честно ждём retry_after и пробуем один раз
            if code == 429 and _retry:
                retry = data.get("parameters", {}).get("retry_after", 1)
                time.sleep(min(retry, 20))
                return self._call(method, params, timeout, _retry=False)
            raise TgBotError(code, data.get("description", "?"))
        return data.get("result")

    # ── Валидация / информация ─────────────────────────────────

    def get_me(self):
        return self._call("getMe")

    def validate(self):
        try:
            me = self.get_me()
            return True, f"@{me.get('username')} (id={me.get('id')})"
        except TgBotError as e:
            return False, f"{e.description}"

    # ── Приём обновлений ───────────────────────────────────────

    def get_updates(self, offset=None, timeout=25):
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            params["offset"] = offset
        try:
            return self._call("getUpdates", params, timeout=timeout + 10) or []
        except TgBotError:
            return []

    # ── Отправка / редактирование ──────────────────────────────

    def send(self, chat_id, text, keyboard=None, html_mode=False, no_preview=True):
        """Отправить сообщение. Возвращает message_id или 0."""
        params = {"chat_id": chat_id, "text": text[:4096]}
        if html_mode:
            params["parse_mode"] = "HTML"
        if no_preview:
            params["disable_web_page_preview"] = "true"
        if keyboard is not None:
            params["reply_markup"] = json.dumps(keyboard)
        try:
            res = self._call("sendMessage", params)
            return res.get("message_id", 0) if res else 0
        except TgBotError as e:
            # Фолбэк: если HTML не распарсился — шлём как обычный текст
            if html_mode and "parse" in e.description.lower():
                params.pop("parse_mode", None)
                params["text"] = text[:4096]
                try:
                    res = self._call("sendMessage", params)
                    return res.get("message_id", 0) if res else 0
                except TgBotError:
                    return 0
            raise

    def edit(self, chat_id, message_id, text, keyboard=None, html_mode=False):
        """Редактировать сообщение. Возвращает True/False."""
        params = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096],
                  "disable_web_page_preview": "true"}
        if html_mode:
            params["parse_mode"] = "HTML"
        if keyboard is not None:
            params["reply_markup"] = json.dumps(keyboard)
        try:
            self._call("editMessageText", params)
            return True
        except TgBotError as e:
            d = e.description.lower()
            if "not modified" in d:
                return True   # ничего не изменилось — не ошибка
            return False

    def delete(self, chat_id, message_id):
        try:
            self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
            return True
        except TgBotError:
            return False

    def answer_callback(self, callback_id, text=None):
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text[:200]
        try:
            self._call("answerCallbackQuery", params)
        except TgBotError:
            pass

    def typing(self, chat_id):
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TgBotError:
            pass

    def set_commands(self, commands):
        """Задать меню команд (показывается в клиенте Telegram)."""
        try:
            self._call("setMyCommands", {"commands": json.dumps(commands)})
        except TgBotError:
            pass


# ── Хелперы ─────────────────────────────────────────────────────

def pre_block(text):
    """Обернуть в <pre> для моноширинного вывода (TUI ровный). Экранирует HTML."""
    return f"<pre>{html.escape(text)}</pre>"


def ikb(rows):
    """Собрать inline-клавиатуру из рядов кнопок.
    rows — список рядов; кнопка — (text, callback_data) или dict{text,data}."""
    kb = []
    for row in rows:
        krow = []
        for btn in row:
            if isinstance(btn, dict):
                krow.append({"text": btn["text"], "callback_data": btn["data"]})
            else:
                krow.append({"text": btn[0], "callback_data": btn[1]})
        kb.append(krow)
    return {"inline_keyboard": kb}
