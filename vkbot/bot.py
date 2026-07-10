"""Главный модуль бота.

Обрабатывает команды, управляет состоянием, запускает Long Poll.
"""
import time
import threading
import re
import json
import sys
import traceback
from collections import deque

from .config import load_config, save_config
from .vk_api import (
    VkApi,
    VkApiError,
    make_keyboard,
    make_main_keyboard,
    make_tg_menu_keyboard,
    make_sessions_keyboard,
    make_kill_keyboard,
    make_watch_keyboard,
    make_notify_keyboard,
)
from .tmux_handler import (
    list_sessions,
    session_exists,
    get_output,
    send_keys,
    send_control_key,
    create_session,
    kill_session,
    detect_errors,
    highlight_errors,
    detect_session_state,
    format_output,
    clean_pane,
)
from .state_manager import save_state, load_state
from .scheduler import (
    Scheduler, ScheduledTask, _parse_at_time, _parse_in_time,
    _fmt_time, parse_pipeline_args,
)
from .tg_client import TgClient


# Транслитерация кириллица → латиница для поиска чатов
_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit(text):
    """Привести строку к латинице в нижнем регистре (для нечёткого поиска)."""
    text = text.lower()
    return "".join(_TRANSLIT_MAP.get(ch, ch) for ch in text)


# Рендер вывода теперь общий — в tmux_handler (переиспользуется Telegram-ботом).
# Оставляем алиас _clean_pane для существующих тестов.
_clean_pane = clean_pane


class VkTmuxBot:
    """Основной класс бота."""

    def __init__(self):
        self.config = None
        self.vk = None
        self._lock = threading.Lock()
        self.current_sessions = {}
        self.watching_sessions = {}
        self.pending_new_session = {}
        self.pending_input = {}
        self.error_notify = {}
        self._watch_threads = {}
        self._running = False
        self.scheduler = Scheduler(execute_callback=self._execute_scheduled_task)
        self.tg_clients = {}          # {user_id: TgClient} — свой Telegram у каждого
        self.tg_state = {}            # {user_id: {view, chat_id, chat_name, ...}}
        self.tg_live = {}             # {user_id: {chat_id, stop}} — живой режим чата
        self._tg_live_threads = {}    # {user_id: Thread}
        self.tg_favorites = {}        # {user_id: set(chat_id)} — избранные чаты
        self.tg_muted = {}            # {user_id: set(chat_id)} — исключённые из непрочитанных
        self.tg_watch = {}            # {user_id: {interval, stop, seen}} — глоб. уведомления
        self._tg_watch_threads = {}   # {user_id: Thread}
        self.tg_watch_cfg = {}        # {user_id: interval_sec} — сохранённые настройки
        self.tg_open_chat = {}        # {user_id: {chat_id, topic_id}} — открытый чат (персист)
        self._tg_vk_sent = {}         # {user_id: deque(msg_id)} — что отправлено ИЗ VK (чтобы не дублировать)
        self._tg_fav_last = {}        # {user_id: {chat_id: last_pushed_id}} — дедуп пуша избранного
        self._tg_catchup_ts = {}      # {user_id: ts} — троттлинг догоняющего опроса
        self._tg_fav_lock = threading.Lock()  # атомарный claim диапазона пуша избранного
        self._transcribe_sem = threading.Semaphore(3)  # не более 3 опросов расшифровки разом
        self._tg_lock = threading.Lock()  # защита избранного/мутов/настроек
        self._clients_lock = threading.Lock()  # защита создания TG-клиентов
        self._persist_lock = threading.Lock()  # защита записи JSON-файлов
        self.users = {}               # динамические пользователи {id: {tmux, name}}
        self.pending_admin = {}       # {user_id: "mode"} — ввод в админке
        self.tg_accounts = {}         # {user_id: {"active": slug, "accounts": {slug: label}}}
        self._load_favorites()
        self._load_open_chats()
        self._load_accounts()

    # ── Роли и доступ ────────────────────────────────────────────

    def _is_allowed(self, user_id):
        """Есть ли у пользователя доступ к боту вообще."""
        allowed = set(self.config["vk"].get("allowed_user_ids", []))
        allowed |= set(self.users.keys())
        # Пустой список = доступ всем (для лёгкого старта)
        if not allowed and not self.config["vk"].get("admin_ids"):
            return True
        return user_id in allowed or self._is_admin(user_id)

    def _is_admin(self, user_id):
        """Админ — полный доступ (tmux, Claude, планировщик, админка)."""
        return user_id in set(self.config["vk"].get("admin_ids", []))

    def _can_tmux(self, user_id):
        """Может ли пользователь управлять сервером (tmux/Claude/планировщик)."""
        if self._is_admin(user_id):
            return True
        u = self.users.get(user_id)
        return bool(u and u.get("tmux"))

    # ── Потокобезопасные хелперы ─────────────────────────────────

    def _get_session(self, user_id):
        with self._lock:
            return self.current_sessions.get(user_id)

    def _set_session(self, user_id, name):
        with self._lock:
            self.current_sessions[user_id] = name

    def _del_session(self, user_id):
        with self._lock:
            return self.current_sessions.pop(user_id, None)

    def _get_watch(self, user_id):
        with self._lock:
            return self.watching_sessions.get(user_id)

    def _set_watch(self, user_id, watch_info):
        with self._lock:
            self.watching_sessions[user_id] = watch_info

    def _del_watch(self, user_id):
        with self._lock:
            return self.watching_sessions.pop(user_id, None)

    def _save_state(self):
        with self._lock:
            save_state(
                dict(self.current_sessions),
                dict(self.watching_sessions),
            )

    # ── Запуск / остановка ──────────────────────────────────────

    def start(self):
        """Запустить бота."""
        print("🚀 VK Tmux Bot запускается...\n")

        # Загружаем конфиг
        self.config = load_config()
        if not self.config:
            print("❌ Не удалось загрузить конфигурацию.")
            print(f"   Отредактируйте: ~/.vk-tmux-bot/config.json")
            return False

        # Загружаем динамических пользователей (добавленных админом)
        from .config import load_users
        self.users = load_users()

        # Создаём API клиент
        self.vk = VkApi(
            self.config["vk"]["group_token"],
            rate_limit_delay=self.config["bot"].get("rate_limit_delay", 0.4),
        )

        # Проверяем токен
        print("🔍 Проверяю токен...")
        ok, info = self.vk.validate_token()
        print(f"   {info}")
        if not ok:
            print("❌ Невалидный токен. Проверьте group_token в конфиге.")
            return False

        # Восстанавливаем состояние
        cur, watch = load_state()
        self.current_sessions = cur
        self.watching_sessions = watch

        print(f"✅ Конфигурация загружена")
        print(f"   Группа: {self.config['vk']['group_id']}")
        allowed = self.config['vk']['allowed_user_ids']
        if allowed:
            print(f"   Разрешённые пользователи: {allowed}")
        else:
            print(f"   Доступ открыт всем (белый список пуст)")
        print(f"   Сессий сохранено: {len(self.current_sessions)}")
        print(f"   Watch-сессий: {len(self.watching_sessions)}")
        print()

        # Возобновляем watch (tmux) и уведомления (telegram)
        self._resume_watch()
        self._resume_watches()
        self._resume_tg_sessions()  # восстановить открытые TG-чаты после ребута

        # Запускаем планировщик
        self.scheduler.start()

        # Запускаем Long Poll
        self._running = True
        print("🎯 Бот слушает сообщения (Long Poll)...\n")
        self._long_poll_loop()

        return True

    def stop(self):
        """Остановить бота."""
        print("\n🛑 Остановка бота...")
        self._running = False

        # Останавливаем все watch-потоки
        with self._lock:
            for uid in list(self.watching_sessions.keys()):
                ws = self.watching_sessions.get(uid)
                if ws:
                    ws["stop"] = True

        # Останавливаем планировщик
        self.scheduler.stop()

        # Отключаем все Telegram-клиенты (закрываем event-loop потоки)
        with self._clients_lock:
            clients = list(self.tg_clients.values())
            self.tg_clients.clear()
        for c in clients:
            try:
                c.disconnect()
            except Exception:
                pass

        self._save_state()
        print("✅ Состояние сохранено")

    # ── Long Poll ────────────────────────────────────────────────

    def _long_poll_loop(self):
        """Основной цикл Long Poll."""
        group_id = self.config["vk"]["group_id"]

        while self._running:
            try:
                lp_data = self.vk.get_long_poll_server(group_id)
                server = lp_data["server"]
                key = lp_data["key"]
                ts = lp_data["ts"]
                wait = self.config["bot"].get("long_poll_wait", 25)

                print(f"🔌 Long Poll подключён (ts={ts})")

                while self._running:
                    updates, new_ts = self.vk.long_poll(server, key, ts, wait=wait)

                    if updates is None:
                        # Серверная ошибка — переподключаемся
                        fail_code = new_ts
                        reasons = {1: "история утеряна", 2: "ключ истёк", 3: "информация утеряна"}
                        print(f"⚠️ Long Poll: {reasons.get(fail_code, f'ошибка {fail_code}')}, переподключение...")
                        break

                    ts = new_ts
                    for event in updates:
                        try:
                            self._handle_event(event)
                        except Exception as e:
                            print(f"⚠️ Ошибка обработки события: {e}")

            except VkApiError as e:
                print(f"❌ Ошибка VK API: {e}")
                if e.code == 5:  # token invalid
                    print("   Токен недействителен. Остановка.")
                    self._running = False
                    break
                time.sleep(5)
            except Exception as e:
                print(f"❌ Ошибка Long Poll: {e}")
                time.sleep(5)

    def _handle_event(self, event):
        """Обработать одно событие от Long Poll."""
        etype = event.get("type")

        if etype == "message_new":
            msg = event.get("object", {}).get("message", {})
            self._handle_message(msg)
        elif etype == "message_event":
            obj = event.get("object", {})
            self._handle_callback(obj)

    # ── Обработка сообщений ──────────────────────────────────────

    def _handle_message(self, msg):
        """Обработать входящее сообщение."""
        user_id = msg.get("from_id", 0)
        peer_id = msg.get("peer_id", 0)
        msg_id = msg.get("conversation_message_id", 0)
        text = msg.get("text", "").strip()
        payload = msg.get("payload", "")
        attachments = msg.get("attachments", []) or []

        # Проверка авторизации
        if not self._is_allowed(user_id):
            self.vk.send_message(
                peer_id,
                f"⛔ Нет доступа к боту.\nВаш VK ID: {user_id}\n"
                f"Попросите администратора добавить вас.")
            return

        # Вложения в активном TG-чате → проксируем в Telegram (фото/файлы)
        if attachments and user_id in self.tg_live:
            self.vk.set_typing(peer_id)
            self._delete_msg(peer_id, msg_id)
            self._tg_send_media(peer_id, user_id, attachments, text)
            return

        # Извлекаем команду из payload (кнопки)
        is_button = False
        if payload:
            try:
                payload_data = json.loads(payload)
                cmd = payload_data.get("cmd", "")
                if cmd:
                    text = cmd
                    is_button = True
            except (json.JSONDecodeError, TypeError):
                pass

        # Кнопка ИЛИ слэш-команда во время ожидания ввода — отменяем ожидание
        # и выполняем команду (иначе она улетит как текст ответа/поиска)
        if is_button or text.startswith("/"):
            self.pending_new_session.pop(user_id, None)
            self.pending_input.pop(user_id, None)
            self.pending_admin.pop(user_id, None)

        # Ввод в админке (добавление пользователя)
        if not is_button and user_id in self.pending_admin:
            mode = self.pending_admin.pop(user_id)
            if mode == "adduser":
                self._do_adduser(peer_id, user_id, text)
            self._delete_msg(peer_id, msg_id)
            return

        # Режимы ожидания ввода (только для обычного текста, не кнопок)
        if not is_button and user_id in self.pending_new_session:
            self._create_session_from_input(peer_id, user_id, text)
            self._delete_msg(peer_id, msg_id)
            return

        if not is_button and user_id in self.pending_input:
            mode = self.pending_input[user_id]
            if mode == "send":
                self._send_command_input(peer_id, user_id, text)
            elif mode.startswith("tg_"):
                self._tg_handle_input(peer_id, user_id, text)
            self._delete_msg(peer_id, msg_id)
            return

        # «//команда» → слэш-команда уходит В СЕССИЮ (например //model в Claude),
        # а не боту. Работает только в активной сессии с доступом.
        if text.startswith("//") and self._get_session(user_id) and self._can_tmux(user_id):
            self.vk.set_typing(peer_id)
            self._send_as_command(peer_id, user_id, text[1:])  # шлём "/model"
            self._delete_msg(peer_id, msg_id)
            return

        # Команды
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0][1:]
            args = parts[1] if len(parts) > 1 else ""
            self._handle_command(cmd, args, peer_id, user_id, msg_id)
        elif user_id in self.tg_live:
            # В активном TG-чате — текст уходит собеседнику (как в мессенджере)
            self._delete_msg(peer_id, msg_id)
            self._tg_send(peer_id, user_id, text)
        elif self._get_session(user_id) is not None and self._can_tmux(user_id):
            # Нет / — отправляем как команду в активную tmux-сессию
            self._send_as_command(peer_id, user_id, text)
            self._delete_msg(peer_id, msg_id)
        # Иначе: просто текст без контекста — показываем help
        elif text and not text.startswith("/"):
            # Если сессия осталась у юзера без доступа — снимаем её
            if self._get_session(user_id) is not None and not self._can_tmux(user_id):
                self._del_session(user_id)
            self._cmd_help(peer_id, user_id, "")

    def _handle_callback(self, obj):
        """Обработать callback от inline-кнопки."""
        user_id = obj.get("user_id", 0)
        peer_id = obj.get("peer_id", 0)
        event_id = obj.get("event_id", "")
        payload = obj.get("payload", {})

        # Подтверждаем получение
        try:
            self.vk.send_message_event_answer(event_id, user_id, peer_id)
        except Exception:
            pass

        # Извлекаем команду
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                return

        cmd = payload.get("cmd", "")
        if cmd and cmd.startswith("/"):
            parts = cmd.split(maxsplit=1)
            cmd_name = parts[0][1:]
            args = parts[1] if len(parts) > 1 else ""
            self._handle_command(cmd_name, args, peer_id, user_id)

    # ── Роутинг команд ───────────────────────────────────────────

    # Команды управления сервером — только для админа / пользователей с tmux-доступом
    _SERVER_CMDS = {
        "ls", "sessions", "сессии", "new", "новая", "run", "attach", "подключить",
        "kill", "удалить", "delete", "o", "output", "вывод", "watch", "смотреть",
        "unwatch", "стоп", "s", "send", "отправить", "e", "enter", "c", "d",
        "session", "сессия", "detach", "откл", "claude", "клод", "dcc", "дкк",
        "in", "через", "at", "в", "tasks", "задачи", "cancel", "отмена",
        "projects", "проекты", "proj", "pj",
        # клавиши пульта
        "up", "down", "left", "right", "esc", "escape", "tab", "btab",
        "space", "bspace", "pgup", "pgdn", "home", "end",
    }
    _ADMIN_CMDS = {"admin", "админ", "users", "adduser", "grant", "revoke"}

    def _handle_command(self, cmd, args, peer_id, user_id, msg_id=0):
        """Маршрутизация команд с русскими и английскими алиасами."""
        print(f"📥 /{cmd} {args} от user={user_id}")

        # Гейтинг доступа: серверные команды — только с tmux-доступом
        if cmd in self._SERVER_CMDS and not self._can_tmux(user_id):
            self.vk.send_message(
                peer_id,
                "🔒 Управление сервером доступно только администратору.\n"
                "Вам доступен Telegram: /tg")
            return
        if cmd in self._ADMIN_CMDS and not self._is_admin(user_id):
            self.vk.send_message(peer_id, "🔒 Только для администратора.")
            return

        # «… печатает» вместо спама «Загружаю…» для команд, что грузят контент
        if cmd in ("tg", "тг", "o", "output", "вывод", "attach", "подключить",
                   "claude", "клод", "dcc", "дкк", "ls", "sessions", "сессии"):
            self.vk.set_typing(peer_id)

        # Команды, которые отправляют текст в tmux — удаляем сообщение юзера
        TMUX_CMDS = {"s", "send", "отправить", "e", "enter", "c", "d"}

        commands = {
            # Помощь
            "start": self._cmd_help,
            "help": self._cmd_help,
            "помощь": self._cmd_help,
            "хелп": self._cmd_help,
            # Меню
            "menu": self._cmd_menu,
            "меню": self._cmd_menu,
            # Клавиши пульта (тихие — watch показывает результат)
            "up": lambda p, u, a: self._send_session_key(p, u, "up"),
            "down": lambda p, u, a: self._send_session_key(p, u, "down"),
            "left": lambda p, u, a: self._send_session_key(p, u, "left"),
            "right": lambda p, u, a: self._send_session_key(p, u, "right"),
            "esc": lambda p, u, a: self._send_session_key(p, u, "esc"),
            "escape": lambda p, u, a: self._send_session_key(p, u, "esc"),
            "tab": lambda p, u, a: self._send_session_key(p, u, "tab"),
            "btab": lambda p, u, a: self._send_session_key(p, u, "btab"),
            "space": lambda p, u, a: self._send_session_key(p, u, "space"),
            "bspace": lambda p, u, a: self._send_session_key(p, u, "bspace"),
            "pgup": lambda p, u, a: self._send_session_key(p, u, "pgup"),
            "pgdn": lambda p, u, a: self._send_session_key(p, u, "pgdn"),
            "home": lambda p, u, a: self._send_session_key(p, u, "home"),
            "end": lambda p, u, a: self._send_session_key(p, u, "end"),
            # Сессии
            "ls": self._cmd_ls,
            "sessions": self._cmd_ls,
            "сессии": self._cmd_ls,
            "new": self._cmd_new,
            "новая": self._cmd_new,
            "run": self._cmd_new,       # alias: /run = создать и запустить
            "attach": self._cmd_attach,
            "подключить": self._cmd_attach,
            "kill": self._cmd_kill,
            "удалить": self._cmd_kill,
            "delete": self._cmd_kill,
            "session": self._cmd_session,
            "сессия": self._cmd_session,
            "detach": self._cmd_detach,
            "откл": self._cmd_detach,
            "claude": self._cmd_claude,
            "клод": self._cmd_claude,
            "dcc": self._cmd_dcc,
            "дкк": self._cmd_dcc,
            "projects": self._cmd_projects,
            "проекты": self._cmd_projects,
            "proj": self._cmd_proj,
            "pj": self._cmd_pj,
            # Вывод
            "o": self._cmd_output,
            "output": self._cmd_output,
            "вывод": self._cmd_output,
            "watch": self._cmd_watch,
            "смотреть": self._cmd_watch,
            "unwatch": self._cmd_unwatch,
            "стоп": self._cmd_unwatch,
            # Управление
            "s": self._cmd_send,
            "send": self._cmd_send,
            "отправить": self._cmd_send,
            "e": self._cmd_enter,
            "enter": self._cmd_enter,
            "c": self._cmd_ctrl_c,
            "d": self._cmd_ctrl_d,
            # Настройки
            "notify": self._cmd_notify,
            "уведомления": self._cmd_notify,
            # Telegram прокси
            "tg": self._cmd_tg,
            "тг": self._cmd_tg,
            # Планировщик
            "at": self._cmd_at,
            "в": self._cmd_at,
            "in": self._cmd_in,
            "через": self._cmd_in,
            "tasks": self._cmd_tasks,
            "задачи": self._cmd_tasks,
            "cancel": self._cmd_cancel,
            "отмена": self._cmd_cancel,
            # Админка
            "admin": self._cmd_admin,
            "админ": self._cmd_admin,
            "adduser": self._cmd_adduser,
            "grant": self._cmd_grant,
            "revoke": self._cmd_revoke,
            "users": self._cmd_admin,
        }

        # Обработка tg-подкоманд из кнопок
        if cmd == "tg" and args:
            sub = args.split(maxsplit=1)[0].lower()
            sub_args = args.split(maxsplit=1)[1] if " " in args else ""
            if sub == "open":
                try:
                    self._tg_show_messages(peer_id, user_id, int(sub_args.strip()))
                except ValueError:
                    self.vk.send_message(peer_id, "❌ Неверный ID чата")
                return
            elif sub == "refresh":
                try:
                    self._tg_show_messages(peer_id, user_id, int(sub_args.strip()))
                except ValueError:
                    self.vk.send_message(peer_id, "❌ Неверный ID чата")
                return
            elif sub == "reply":
                try:
                    a = sub_args.split()
                    chat_id = int(a[0])
                    tid = int(a[1]) if len(a) > 1 else None
                    st = self.tg_state.get(user_id, {})
                    st["chat_id"] = chat_id
                    st["topic_id"] = tid  # сбрасываем/устанавливаем топик явно
                    st["view"] = "chat"
                    self.tg_state[user_id] = st
                    self._tg_send(peer_id, user_id, "")
                except (ValueError, IndexError):
                    self.vk.send_message(peer_id, "❌ Неверный ID чата")
                return
            elif sub in ("fav", "unfav"):
                if sub_args.strip():
                    try:
                        cid = int(sub_args.strip())
                        self._tg_set_fav(user_id, cid, add=(sub == "fav"))
                        name = self._tg_chat_display_name(user_id, cid)
                        icon = "⭐ добавлен в избранное" if sub == "fav" else "☆ убран из избранного"
                        self._tg_toggle_confirm(peer_id, user_id, cid, f"{name} — {icon}")
                    except ValueError:
                        self.vk.send_message(peer_id, "❌ Неверный ID чата")
                else:
                    self._tg_show_chats(peer_id, user_id, page=0, favorites=True)
                return
            elif sub in ("mute", "unmute"):
                try:
                    cid = int(sub_args.strip())
                    self._tg_set_muted(user_id, cid, add=(sub == "mute"))
                    name = self._tg_chat_display_name(user_id, cid)
                    icon = ("🔕 исключён из непрочитанных" if sub == "mute"
                            else "🔔 возвращён в непрочитанные")
                    self._tg_toggle_confirm(peer_id, user_id, cid, f"{name} — {icon}")
                except ValueError:
                    self.vk.send_message(peer_id, "❌ Неверный ID чата")
                return
            elif sub == "fpage":
                try:
                    self._tg_show_chats(peer_id, user_id, page=int(sub_args.strip()), favorites=True)
                except ValueError:
                    self._tg_show_chats(peer_id, user_id, page=0, favorites=True)
                return
            elif sub in ("folders", "папки"):
                self._tg_show_folders(peer_id, user_id)
                return
            elif sub == "folder":
                # /tg folder <fid> [page]
                a = sub_args.split()
                try:
                    fid = int(a[0]); pg = int(a[1]) if len(a) > 1 else 0
                    self._tg_show_chats(peer_id, user_id, page=pg, folder_id=fid)
                except (ValueError, IndexError):
                    self._tg_show_folders(peer_id, user_id)
                return
            elif sub == "topics":
                try:
                    self._tg_show_topics(peer_id, user_id, int(sub_args.strip()))
                except ValueError:
                    self.vk.send_message(peer_id, "❌ Неверный ID чата")
                return
            elif sub == "topic":
                # /tg topic <chat_id> <topic_id>
                a = sub_args.split()
                try:
                    cid = int(a[0]); tid = int(a[1])
                    self._tg_show_messages(peer_id, user_id, cid, topic_id=tid)
                except (ValueError, IndexError):
                    self.vk.send_message(peer_id, "❌ Неверный топик")
                return
            elif sub == "watch":
                # /tg watch <seconds|off>
                self._tg_watch_set(peer_id, user_id, sub_args.strip())
                return
            elif sub in ("logout", "выход"):
                self._tg_logout(peer_id, user_id)
                return
            elif sub in ("accounts", "аккаунты"):
                self._tg_show_accounts(peer_id, user_id)
                return
            elif sub == "account":
                self._tg_switch_account(peer_id, user_id, sub_args.strip())
                return
            elif sub in ("addaccount", "newaccount"):
                self._tg_add_account(peer_id, user_id)
                return
            elif sub == "page":
                try:
                    self._tg_show_chats(peer_id, user_id, page=int(sub_args.strip()))
                except ValueError:
                    self._tg_show_chats(peer_id, user_id, page=0)
                return
            elif sub == "upage":
                try:
                    self._tg_show_chats(peer_id, user_id, page=int(sub_args.strip()), only_unread=True)
                except ValueError:
                    self._tg_show_chats(peer_id, user_id, page=0, only_unread=True)
                return
            elif sub == "back":
                self._tg_show_chats(peer_id, user_id, page=0)
                return

        handler = commands.get(cmd)
        if handler:
            try:
                handler(peer_id, user_id, args)
                # Удаляем сообщение юзера для tmux-команд (имитация прямого ввода)
                if cmd in TMUX_CMDS and msg_id:
                    self._delete_msg(peer_id, msg_id)
            except Exception as e:
                print(f"❌ Ошибка в команде /{cmd}: {e}")
                traceback.print_exc()
                self.vk.send_message(peer_id, f"❌ Ошибка при выполнении команды: {e}")
        else:
            self.vk.send_message(
                peer_id,
                f"❌ Неизвестная команда: /{cmd}\nИспользуйте /help для списка команд"
            )

    def _delete_msg(self, peer_id, cmid):
        """Удалить входящее сообщение юзера по conversation_message_id."""
        if not cmid:
            return
        try:
            self.vk.delete_message(peer_id, cmid=cmid)
        except Exception:
            pass

    # ── Команды ──────────────────────────────────────────────────

    def _cmd_help(self, peer_id, user_id, args):
        """Показать справку (зависит от роли)."""
        tg_help = """✈️ TELEGRAM (кнопка «Telegram» или /tg)
  Как настоящий мессенджер:
  • открыл чат → просто пиши, уходит собеседнику
  • ответы приходят сами (живая лента)
  • фото и файлы — в обе стороны

  /tg — чаты (личные → группы → каналы)
  /tg unread — непрочитанные
  /tg find <имя> — поиск (рус/лат)
  /tg watch — 🔔 уведомления по таймеру
  /tg folders — 📁 папки · 🌳 форумы по топикам
  /tg accounts — 🔀 несколько аккаунтов
  ⭐ избранное · 🔕 исключить"""

        if not self._can_tmux(user_id):
            msg = "🤖 VK ↔ Telegram\n\n" + tg_help + "\n\n💡 /menu — кнопки"
            self.vk.send_message(peer_id, msg, keyboard=make_tg_menu_keyboard())
            return

        msg = f"""🤖 VK Control Bot — сервер и Telegram

━━━━━━━━━━━━━━━━━━
{tg_help}

━━━━━━━━━━━━━━━━━━
🖥 СЕССИИ TMUX / CLAUDE CODE
  /ls — список (тап = подключиться + пульт)
  /new <имя> [команда] — создать (+ запуск)
  /claude — Claude Code · /dcc — DeepClaude
  /attach · /kill · /detach

🎮 Пульт (кнопки под выводом): стрелки ⬆⬇⬅➡,
  ⏎ Enter, ⎋ Esc, ⇥ Tab, ⇧⇥ Shift+Tab, ⛔ Ctrl+C
  — навигация по меню Claude (/resume, /model и т.п.)

⌨️ Ввод в сессию (когда подключён):
  • просто пиши текст → уходит в сессию
  • //model, //resume — слэш-команды В Claude
    (одиночный / — это команды бота)

━━━━━━━━━━━━━━━━━━
⏰ ПЛАНИРОВЩИК
  /in 5m сессия команда — через N минут
  /at 14:30 сессия команда — ко времени
  пайплайн: /in 5m s | cmd1 | cmd2 | 30s
  /tasks · /cancel <id>"""
        if self._is_admin(user_id):
            msg += "\n\n👑 АДМИН: /admin — управление доступом"
        kb = make_main_keyboard(self._get_session(user_id), is_admin=self._is_admin(user_id),
                                has_projects=bool(self._projects()))
        self.vk.send_message(peer_id, msg, keyboard=kb)

    def _cmd_menu(self, peer_id, user_id, args):
        """Показать клавиатуру (зависит от роли)."""
        # Пользователи без tmux-доступа — только Telegram-меню
        if not self._can_tmux(user_id):
            kb = make_tg_menu_keyboard()
            self.vk.send_message(
                peer_id,
                "✈️ Меню Telegram\n\nОткройте чаты и общайтесь прямо из VK:",
                keyboard=kb)
            return
        session = self._get_session(user_id)
        kb = make_main_keyboard(session, is_admin=self._is_admin(user_id),
                                has_projects=bool(self._projects()))
        if session:
            text = f"⚡ Панель управления — сессия: «{session}»"
        else:
            text = "⚡ Панель управления"
        self.vk.send_message(peer_id, text, keyboard=kb)

    def _cmd_ls(self, peer_id, user_id, args):
        """Список сессий с кнопками для подключения."""
        sessions = list_sessions()
        current = self._get_session(user_id)

        if not sessions:
            self.vk.send_message(
                peer_id,
                "Нет запущенных tmux сессий.\n\nСоздайте новую: /new <имя>",
                keyboard=make_sessions_keyboard([], current),
            )
            return

        lines = ["📋 Активные сессии:"]
        for s in sessions:
            marker = "▶ " if s == current else "  • "
            lines.append(f"{marker}{s}")

        self.vk.send_message(
            peer_id,
            "\n".join(lines) + "\n\nНажмите на сессию чтобы подключиться:",
            keyboard=make_sessions_keyboard(sessions, current),
        )

    def _cmd_new(self, peer_id, user_id, args):
        """Создать новую сессию.

        /new <имя> [команда] — создать сессию и опционально выполнить команду.
        /run <имя> <команда> — alias.
        """
        if args:
            parts = args.split(maxsplit=1)
            name = parts[0].strip()
            command = parts[1].strip() if len(parts) > 1 else None
            self._create_session(peer_id, user_id, name, command)
        else:
            self.pending_new_session[user_id] = True
            self.vk.send_message(
                peer_id,
                "➕ Новая tmux сессия\n\n"
                "Напишите имя сессии (латиница, цифры, - и _)\n"
                "Можно сразу с командой: имя команда\n"
                "Например: build npm run build",
            )

    def _create_session_from_input(self, peer_id, user_id, text):
        """Создать сессию из текстового ввода. Поддерживает 'имя команда'."""
        self.pending_new_session.pop(user_id, None)
        text = text.strip()

        # Разбираем: имя [команда]
        parts = text.split(maxsplit=1)
        name = parts[0]
        command = parts[1] if len(parts) > 1 else None

        if not name or not re.match(r"^[a-zA-Z0-9_-]+$", name):
            self.vk.send_message(
                peer_id,
                "❌ Некорректное имя. Только латиница, цифры, - и _."
            )
            return

        self._create_session(peer_id, user_id, name, command)

    def _create_tmux(self, name, work_dir=None):
        """Создать tmux-сессию с настройками из конфига (узкая ширина под TUI).
        work_dir переопределяет папку старта (напр. для проектов)."""
        t = self.config["tmux"]
        return create_session(name, work_dir=work_dir or t.get("work_dir"),
                              width=t.get("term_width"), height=t.get("term_height"))

    def _create_session(self, peer_id, user_id, name, command=None):
        """Создать tmux сессию, подключиться, и опционально выполнить команду."""
        if session_exists(name):
            self.vk.send_message(
                peer_id,
                f"❌ Сессия «{name}» уже существует.\nИспользуйте /attach {name}"
            )
            return

        self.vk.send_message(peer_id, f"⏳ Создаю сессию «{name}»...")

        if self._create_tmux(name):
            self._set_session(user_id, name)
            self._save_state()
            kb = make_main_keyboard(name)

            if command:
                # Небольшая пауза чтобы shell инициализировался
                time.sleep(0.4)
                # Отправляем команду
                if send_keys(name, command, press_enter=True):
                    # Получаем вывод
                    time.sleep(0.5)
                    output = get_output(name, self.config["tmux"]["output_lines"])
                    formatted = format_output(name, output)
                    self.vk.send_message(
                        peer_id,
                        f"✅ Сессия «{name}» создана. Команда выполнена:\n\n{formatted}",
                        keyboard=kb,
                    )
                else:
                    self.vk.send_message(
                        peer_id,
                        f"✅ Сессия «{name}» создана, но команда не отправлена.",
                        keyboard=kb,
                    )
            else:
                self.vk.send_message(
                    peer_id,
                    f"✅ Сессия «{name}» создана и готова к работе!\nОтправьте команду: /s ls -la",
                    keyboard=kb,
                )
            print(f"✅ user={user_id} создал сессию: {name}" + (f" + команда: {command}" if command else ""))
        else:
            self.vk.send_message(
                peer_id,
                "❌ Не удалось создать сессию. Проверьте, что tmux установлен: tmux -V"
            )

    def _cmd_attach(self, peer_id, user_id, args):
        """Подключиться к сессии и сразу включить watch (живой вывод)."""
        if not args:
            self._cmd_ls(peer_id, user_id, "")
            return

        name = args.strip()
        if not session_exists(name):
            self.vk.send_message(
                peer_id,
                f"❌ Сессия «{name}» не найдена.\nИспользуйте /ls для списка."
            )
            return

        self._set_session(user_id, name)
        self._save_state()
        # Сразу включаем watch — живой вывод + детект простоя
        if self._get_watch(user_id):
            self._get_watch(user_id)["stop"] = True
            self._del_watch(user_id)
            self._watch_threads.pop(user_id, None)
        self._cmd_watch(peer_id, user_id, "")
        print(f"✅ user={user_id} подключился к: {name}")

    def _cmd_kill(self, peer_id, user_id, args):
        """Удалить сессию."""
        sessions = list_sessions()
        if not sessions:
            self.vk.send_message(peer_id, "Нет запущенных сессий.")
            return

        if args:
            name = args.strip()
            if not session_exists(name):
                self.vk.send_message(peer_id, f"❌ Сессия «{name}» не найдена.")
                return

            # Отключаем пользователя если он был на этой сессии
            current = self._get_session(user_id)
            if current == name:
                self._del_session(user_id)

            # Останавливаем watch если был
            ws = self._get_watch(user_id)
            if ws and ws.get("session") == name:
                ws["stop"] = True
                self._del_watch(user_id)

            if kill_session(name):
                self._save_state()
                print(f"🗑 user={user_id} удалил сессию: {name}")
                # Показываем ОБНОВЛЁННЫЙ список (не застрявшие кнопки).
                remaining = list_sessions()
                if remaining:
                    self.vk.send_message(
                        peer_id, f"✅ «{name}» удалена. Ещё удалить?",
                        keyboard=make_kill_keyboard(remaining))
                else:
                    self.vk.send_message(
                        peer_id, f"✅ «{name}» удалена. Сессий больше нет.",
                        keyboard=make_main_keyboard(None, is_admin=self._is_admin(user_id)))
            else:
                self.vk.send_message(peer_id, f"❌ Не удалось удалить сессию «{name}».")
        else:
            self.vk.send_message(
                peer_id,
                "🗑 Выберите сессию для удаления:",
                keyboard=make_kill_keyboard(sessions),
            )

    def _cmd_output(self, peer_id, user_id, args):
        """Показать вывод текущей сессии."""
        session = self._get_session(user_id)
        if not session:
            self.vk.send_message(
                peer_id,
                "⚠️ Нет активной сессии.\nИспользуйте /attach <имя> или /ls.",
            )
            return

        if not session_exists(session):
            self.vk.send_message(peer_id, f"❌ Сессия «{session}» больше не существует.")
            self._del_session(user_id)
            self._save_state()
            return

        output = get_output(session, self.config["tmux"]["output_lines"])
        formatted = format_output(session, output)

        # Уведомления об ошибках
        if self.error_notify.get(user_id, False):
            errors = detect_errors(output)
            if errors:
                err_lines = "\n".join(f"❌ {e[:150]}" for e in errors[:3])
                self.vk.send_message(peer_id, f"🚨 Обнаружены ошибки:\n{err_lines}")

        kb = make_main_keyboard(session)
        self.vk.send_message(peer_id, formatted, keyboard=kb)

    def _cmd_watch(self, peer_id, user_id, args):
        """Запустить режим автообновления вывода."""
        session = self._get_session(user_id)
        if not session:
            self.vk.send_message(peer_id, "⚠️ Нет активной сессии. Используйте /attach <имя>.")
            return

        if self._get_watch(user_id):
            self.vk.send_message(peer_id, "⚠️ Уже в режиме наблюдения. /unwatch для остановки.")
            return

        if not session_exists(session):
            self.vk.send_message(peer_id, f"❌ Сессия «{session}» больше не существует.")
            self._del_session(user_id)
            return

        output = get_output(session, self.config["tmux"]["output_lines"])
        formatted = format_output(session, output)
        kb = make_watch_keyboard()

        mid = self.vk.send_message(peer_id, formatted, keyboard=kb)
        if not mid:
            self.vk.send_message(peer_id, "❌ Не удалось начать watch (VK не вернул id сообщения).")
            return

        self._set_watch(user_id, {
            "session": session,
            "message_id": mid,
            "peer_id": peer_id,
            "stop": False,
        })
        self._save_state()
        print(f"👁 user={user_id} начал watch: {session}")

        self._start_watch_thread(user_id)

    def _cmd_unwatch(self, peer_id, user_id, args):
        """Остановить автообновление."""
        ws = self._get_watch(user_id)
        if not ws:
            self.vk.send_message(peer_id, "⚠️ Не в режиме наблюдения.")
            return

        ws["stop"] = True
        self._del_watch(user_id)
        self._watch_threads.pop(user_id, None)  # иначе повторный /watch не стартует
        self._save_state()

        self.vk.send_message(peer_id, "✅ Режим наблюдения остановлен.")
        print(f"👁 user={user_id} остановил watch")

    def _cmd_send(self, peer_id, user_id, args):
        """Отправить команду в сессию."""
        if args:
            self._send_text(peer_id, user_id, args)
        else:
            self.pending_input[user_id] = "send"
            self.vk.send_message(peer_id, "📝 Напишите команду для отправки в сессию:")

    def _send_command_input(self, peer_id, user_id, text):
        """Отправить команду из режима ожидания."""
        self.pending_input.pop(user_id, None)
        self._send_text(peer_id, user_id, text)

    def _send_as_command(self, peer_id, user_id, text):
        """Отправить текст как команду в активную сессию."""
        self._send_text(peer_id, user_id, text)

    def _send_text(self, peer_id, user_id, text):
        """Отправить текст в активную сессию tmux."""
        session = self._get_session(user_id)
        if not session:
            self.vk.send_message(peer_id, "⚠️ Нет активной сессии. Используйте /attach <имя>.")
            return

        if not session_exists(session):
            self.vk.send_message(peer_id, f"❌ Сессия «{session}» больше не существует.")
            self._del_session(user_id)
            return

        # При активном watch подтверждение не шлём — лента сама покажет ввод
        watching = self._get_watch(user_id) is not None
        # Короткий текст — сразу с Enter, длинный — сначала текст
        if len(text) <= 80:
            if send_keys(session, text, press_enter=True):
                if not watching:
                    self.vk.send_message(peer_id, f"✅ {text[:100]}")
            else:
                self.vk.send_message(peer_id, "❌ Не удалось отправить команду.")
        else:
            if send_keys(session, text, press_enter=False):
                time.sleep(0.3)
                send_keys(session, "", press_enter=True)
                if not watching:
                    self.vk.send_message(peer_id, f"✅ {text[:80]}…")
            else:
                self.vk.send_message(peer_id, "❌ Не удалось отправить команду.")

    # tmux-имена клавиш для пульта управления
    _KEY_MAP = {
        "e": "Enter", "enter": "Enter",
        "esc": "Escape", "escape": "Escape",
        "tab": "Tab", "btab": "BTab",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "space": "Space", "bspace": "BSpace",
        "pgup": "PageUp", "pgdn": "PageDown", "home": "Home", "end": "End",
    }

    def _send_session_key(self, peer_id, user_id, cmd):
        """Отправить именованную клавишу в сессию (ТИХО — watch покажет результат)."""
        session = self._get_session(user_id)
        if not session:
            self.vk.send_message(peer_id, "⚠️ Нет активной сессии. /ls")
            return
        key = self._KEY_MAP.get(cmd)
        if not key:
            return
        self.vk.set_typing(peer_id)
        ok = send_control_key(session, key)
        # Если watch не активен — подтверждаем разово (иначе молчим, лента обновится)
        if ok and not self._get_watch(user_id):
            self.vk.send_message(peer_id, f"✅ {key}")
        elif not ok:
            self.vk.send_message(peer_id, f"❌ Не удалось отправить {key}")

    def _cmd_enter(self, peer_id, user_id, args):
        self._send_session_key(peer_id, user_id, "e")

    def _cmd_key(self, peer_id, user_id, args, _cmd):
        self._send_session_key(peer_id, user_id, _cmd)

    def _cmd_ctrl_c(self, peer_id, user_id, args):
        """Ctrl+C в сессии (тихо при watch)."""
        session = self._get_session(user_id)
        if not session:
            self.vk.send_message(peer_id, "⚠️ Нет активной сессии.")
            return
        self.vk.set_typing(peer_id)
        ok = send_control_key(session, "C-c")
        if ok and not self._get_watch(user_id):
            self.vk.send_message(peer_id, "⛔ Ctrl+C")
        elif not ok:
            self.vk.send_message(peer_id, "❌ Не удалось отправить Ctrl+C.")

    def _cmd_ctrl_d(self, peer_id, user_id, args):
        """Ctrl+D в сессии."""
        session = self._get_session(user_id)
        if not session:
            self.vk.send_message(peer_id, "⚠️ Нет активной сессии.")
            return
        if send_control_key(session, "C-d") and not self._get_watch(user_id):
            self.vk.send_message(peer_id, "🚪 Ctrl+D")

    def _cmd_session(self, peer_id, user_id, args):
        """Показать текущую сессию."""
        session = self._get_session(user_id)
        if session:
            sessions = list_sessions()
            if session in sessions:
                self.vk.send_message(peer_id, f"📍 Активная сессия: «{session}»")
            else:
                self.vk.send_message(peer_id, f"⚠️ Сессия «{session}» не существует. Отключена.")
                self._del_session(user_id)
                self._save_state()
        else:
            self.vk.send_message(peer_id, "⚠️ Нет активной сессии. Используйте /attach <имя>.")

    def _cmd_detach(self, peer_id, user_id, args):
        """Отключиться от текущей сессии (не удаляя её)."""
        session = self._get_session(user_id)
        if not session:
            self.vk.send_message(peer_id, "⚠️ Нет активной сессии.")
            return

        self._del_session(user_id)
        self._save_state()
        kb = make_main_keyboard(None)
        self.vk.send_message(
            peer_id,
            f"🔌 Отключены от «{session}». Сессия продолжает работать.\n"
            f"Подключиться снова: /attach {session}",
            keyboard=kb,
        )
        print(f"🔌 user={user_id} отключился от: {session}")

    def _cmd_claude(self, peer_id, user_id, args):
        """Запустить/подключиться к Claude Code (сессия 'claude') + watch."""
        cmd = self.config.get("claude", {}).get("command", "claude")
        self._launch_ai(peer_id, user_id, "claude", cmd, "🤖 Claude Code")

    def _cmd_dcc(self, peer_id, user_id, args):
        """Запустить/подключиться к DeepClaude (сессия 'dcc') + watch."""
        cmd = self.config.get("claude", {}).get("deepclaude_command", "dcc")
        self._launch_ai(peer_id, user_id, "dcc", cmd, "🧠 DeepClaude")

    def _launch_ai(self, peer_id, user_id, session_name, command, label, work_dir=None):
        """Общий запуск AI-сессии: подключиться если есть, иначе создать+запустить.
        В обоих случаях — сразу watch (живой вывод + детект простоя).
        work_dir — папка старта (для проектов)."""
        self.vk.set_typing(peer_id)  # «печатает» вместо текста-заглушки
        if session_exists(session_name):
            self._set_session(user_id, session_name)
            self._save_state()
        else:
            if not self._create_tmux(session_name, work_dir=work_dir):
                self.vk.send_message(peer_id, "❌ Не удалось создать сессию.")
                return
            self._set_session(user_id, session_name)
            self._save_state()
            time.sleep(0.3)
            send_keys(session_name, command, press_enter=True)
            time.sleep(1.0)
            print(f"{label}: user={user_id} запустил ({command})")
        # Включаем watch — живая лента вывода
        if self._get_watch(user_id):
            self._get_watch(user_id)["stop"] = True
            self._del_watch(user_id)
            self._watch_threads.pop(user_id, None)
        self._cmd_watch(peer_id, user_id, "")

    # ── Мои проекты (конфиг-driven) ───────────────────────────
    def _projects(self):
        """Валидные проекты из конфига: [{name, path, session}, ...]."""
        out = []
        for p in (self.config.get("tmux", {}).get("projects") or []):
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip()
            path = str(p.get("path") or "").strip()
            if not name or not path:
                continue
            sess = re.sub(r"[^a-zA-Z0-9_-]", "-", str(p.get("session") or name).strip())
            out.append({"name": name, "path": path, "session": sess})
        return out

    def _cmd_projects(self, peer_id, user_id, args):
        """Список проектов кнопками."""
        projs = self._projects()
        if not projs:
            self.vk.send_message(
                peer_id,
                "📂 Мои проекты не настроены.\nДобавьте в конфиг секцию "
                "tmux.projects (name + path).",
                keyboard=make_keyboard([[{"label": "🏠 Меню", "color": "primary", "payload": "/menu"}]]))
            return
        rows, row = [], []
        for i, p in enumerate(projs):
            row.append({"label": f"📁 {p['name']}"[:40], "color": "primary", "payload": f"/proj {i}"})
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([{"label": "🏠 Меню", "color": "secondary", "payload": "/menu"}])
        self.vk.send_message(peer_id, "📂 Мои проекты — выберите:",
                             keyboard=make_keyboard(rows))

    def _cmd_proj(self, peer_id, user_id, args):
        """Экран проекта: кнопки Claude / DeepClaude / Терминал."""
        projs = self._projects()
        try:
            idx = int((args or "").strip())
        except ValueError:
            idx = -1
        if idx < 0 or idx >= len(projs):
            self._cmd_projects(peer_id, user_id, "")
            return
        p = projs[idx]
        running = " · ▶ запущена" if session_exists(p["session"]) else ""
        kb = make_keyboard([
            [{"label": "🤖 Claude", "color": "positive", "payload": f"/pj {idx} claude"},
             {"label": "🧠 DeepClaude", "color": "positive", "payload": f"/pj {idx} dcc"}],
            [{"label": "🖥 Терминал", "color": "secondary", "payload": f"/pj {idx} sh"}],
            [{"label": "⬅ Проекты", "color": "secondary", "payload": "/projects"},
             {"label": "🏠 Меню", "color": "primary", "payload": "/menu"}],
        ])
        self.vk.send_message(
            peer_id, f"📁 {p['name']}{running}\n{p['path']}\n\nЧто запустить в этой папке?",
            keyboard=kb)

    def _cmd_pj(self, peer_id, user_id, args):
        """Запуск в проекте: /pj <idx> <claude|dcc|sh>."""
        parts = (args or "").split()
        projs = self._projects()
        try:
            idx = int(parts[0]); kind = parts[1] if len(parts) > 1 else "sh"
        except (ValueError, IndexError):
            self._cmd_projects(peer_id, user_id, "")
            return
        if idx < 0 or idx >= len(projs):
            self._cmd_projects(peer_id, user_id, "")
            return
        p = projs[idx]
        if kind == "sh":
            self.vk.set_typing(peer_id)
            if not session_exists(p["session"]) and not self._create_tmux(p["session"], work_dir=p["path"]):
                self.vk.send_message(peer_id, "❌ Не удалось создать сессию.")
                return
            self._set_session(user_id, p["session"])
            self._save_state()
            self._cmd_watch(peer_id, user_id, "")
            return
        claude_cfg = self.config.get("claude", {})
        if kind == "dcc":
            cmd = claude_cfg.get("deepclaude_command", "dcc"); label = "🧠 DeepClaude"
        else:
            cmd = claude_cfg.get("command", "claude"); label = "🤖 Claude Code"
        self._launch_ai(peer_id, user_id, p["session"], cmd, label, work_dir=p["path"])

    def _cmd_notify(self, peer_id, user_id, args):
        """Вкл/выкл уведомления об ошибках."""
        current = self.error_notify.get(user_id, False)

        if args in ("on", "вкл"):
            self.error_notify[user_id] = True
        elif args in ("off", "выкл"):
            self.error_notify[user_id] = False
        else:
            # Переключение
            self.error_notify[user_id] = not current

        new_state = self.error_notify[user_id]
        status = "🔔 ВКЛЮЧЕНЫ" if new_state else "🔕 ВЫКЛЮЧЕНЫ"
        kb = make_notify_keyboard(new_state)
        self.vk.send_message(
            peer_id,
            f"⚙️ Уведомления об ошибках: {status}\n\nБот присылает сообщение при ошибках в выводе.",
            keyboard=kb,
        )

    # ── Админка (только для админа) ───────────────────────────────

    def _save_users(self):
        from .config import save_users
        save_users(self.users)

    def _cmd_admin(self, peer_id, user_id, args):
        """Панель администратора: список пользователей и управление доступом."""
        admins = self.config["vk"].get("admin_ids", [])
        lines = ["👑 Админ-панель", "", "Администраторы (полный доступ):"]
        for a in admins:
            lines.append(f"  • {a}" + (" (вы)" if a == user_id else ""))
        lines.append("")
        lines.append("Пользователи:")
        allowed = self.config["vk"].get("allowed_user_ids", [])
        seen = set()
        for uid in allowed:
            if uid in admins:
                continue
            u = self.users.get(uid, {})
            tmux = " 🖥 tmux" if u.get("tmux") else ""
            name = u.get("name", "")
            lines.append(f"  • {uid} {name}{tmux}")
            seen.add(uid)
        for uid, u in self.users.items():
            if uid in seen or uid in admins:
                continue
            tmux = " 🖥 tmux" if u.get("tmux") else ""
            lines.append(f"  • {uid} {u.get('name','')}{tmux}")
        if len(lines) <= 6:
            lines.append("  (только вы)")
        lines += ["", "Команды:",
                  "/adduser <vk_id> [имя] — добавить (Telegram)",
                  "/grant <vk_id> — выдать доступ к серверу",
                  "/revoke <vk_id> — забрать доступ к серверу"]
        kb = make_keyboard([
            [{"label": "➕ Добавить юзера", "color": "positive", "payload": "/adduser"}],
            [{"label": "🏠 Главное меню", "color": "primary", "payload": "/menu"}],
        ], one_time=False)
        self.vk.send_message(peer_id, "\n".join(lines), keyboard=kb)

    def _cmd_adduser(self, peer_id, user_id, args):
        """Добавить пользователя (доступ к Telegram-меню)."""
        if not args.strip():
            self.pending_admin[user_id] = "adduser"
            self.vk.send_message(peer_id, "➕ Введите VK ID нового пользователя (и через пробел имя):")
            return
        self._do_adduser(peer_id, user_id, args)

    def _do_adduser(self, peer_id, admin_id, args):
        parts = args.split(maxsplit=1)
        try:
            uid = int(parts[0])
        except (ValueError, IndexError):
            self.vk.send_message(peer_id, "❌ Нужен числовой VK ID.")
            return
        name = parts[1].strip() if len(parts) > 1 else ""
        # Добавляем в allowed_user_ids конфига и в users
        allowed = self.config["vk"].setdefault("allowed_user_ids", [])
        if uid not in allowed:
            allowed.append(uid)
            save_config(self.config)
        self.users[uid] = {"tmux": False, "name": name}
        self._save_users()
        self.vk.send_message(
            peer_id,
            f"✅ Пользователь {uid} {name} добавлен.\n"
            f"Ему доступен Telegram-прокси (/tg → /tg login).\n"
            f"Дать доступ к серверу: /grant {uid}")
        print(f"👑 admin={admin_id} добавил пользователя {uid}")

    def _cmd_grant(self, peer_id, user_id, args):
        """Выдать пользователю доступ к серверу (tmux/Claude)."""
        try:
            uid = int(args.strip())
        except ValueError:
            self.vk.send_message(peer_id, "❌ Использование: /grant <vk_id>")
            return
        u = self.users.setdefault(uid, {"tmux": False, "name": ""})
        u["tmux"] = True
        if uid not in self.config["vk"].get("allowed_user_ids", []):
            self.config["vk"].setdefault("allowed_user_ids", []).append(uid)
            save_config(self.config)
        self._save_users()
        self.vk.send_message(peer_id, f"✅ Пользователю {uid} выдан доступ к серверу (tmux/Claude).")

    def _cmd_revoke(self, peer_id, user_id, args):
        """Забрать доступ к серверу."""
        try:
            uid = int(args.strip())
        except ValueError:
            self.vk.send_message(peer_id, "❌ Использование: /revoke <vk_id>")
            return
        if uid in self.config["vk"].get("admin_ids", []):
            self.vk.send_message(peer_id, "❌ Нельзя забрать доступ у администратора.")
            return
        u = self.users.setdefault(uid, {"tmux": False, "name": ""})
        u["tmux"] = False
        self._save_users()
        # Немедленно отключаем: снимаем активную сессию и tmux-watch
        self._del_session(uid)
        ws = self._get_watch(uid)
        if ws:
            ws["stop"] = True
            self._del_watch(uid)
            self._watch_threads.pop(uid, None)
        self._save_state()
        self.vk.send_message(peer_id, f"✅ У пользователя {uid} забран доступ к серверу. Telegram остался.")

    # ── Telegram прокси ───────────────────────────────────────────

    def _atomic_json(self, path, data):
        """Атомарная запись JSON: пишем во временный файл и os.replace.
        Защищено локом — безопасно из нескольких потоков."""
        import os, json as _json, tempfile
        from .config import CONFIG_DIR
        try:
            os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
            with self._persist_lock:
                fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as f:
                        _json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp, path)
                    os.chmod(path, 0o600)
                except Exception:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                    raise
        except Exception as e:
            print(f"⚠️ Не удалось сохранить {os.path.basename(path)}: {e}")

    def _fav_file(self):
        import os
        from .config import CONFIG_DIR
        return os.path.join(CONFIG_DIR, "tg_favorites.json")

    def _openchat_file(self):
        import os
        from .config import CONFIG_DIR
        return os.path.join(CONFIG_DIR, "tg_open_chats.json")

    def _save_open_chat(self, user_id, chat_id, topic_id):
        """Запомнить открытый чат (для восстановления после ребута)."""
        self.tg_open_chat[user_id] = {"chat_id": chat_id, "topic_id": topic_id}
        self._atomic_json(self._openchat_file(),
                          {str(u): v for u, v in dict(self.tg_open_chat).items()})

    def _clear_open_chat(self, user_id):
        """Забыть открытый чат (пользователь вышел в список)."""
        self.tg_open_chat.pop(user_id, None)
        self._atomic_json(self._openchat_file(),
                          {str(u): v for u, v in dict(self.tg_open_chat).items()})

    def _load_open_chats(self):
        import os, json as _json
        path = self._openchat_file()
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = _json.load(f)
            self.tg_open_chat = {int(u): v for u, v in data.items()}
        except Exception:
            self.tg_open_chat = {}

    def _load_favorites(self):
        import os, json as _json
        path = self._fav_file()
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = _json.load(f)
            fav = data.get("favorites", data)  # обратная совместимость
            self.tg_favorites = {int(u): set(ids) for u, ids in fav.items()}
            muted = data.get("muted", {})
            self.tg_muted = {int(u): set(ids) for u, ids in muted.items()}
            watch = data.get("watch", {})
            self.tg_watch_cfg = {int(u): int(v) for u, v in watch.items()}
        except Exception:
            self.tg_favorites, self.tg_muted, self.tg_watch_cfg = {}, {}, {}

    def _save_favorites(self):
        with self._tg_lock:  # снимок под локом — без гонки итерации
            data = {
                "favorites": {str(u): list(ids) for u, ids in self.tg_favorites.items()},
                "muted": {str(u): list(ids) for u, ids in self.tg_muted.items()},
                "watch": {str(u): v for u, v in self.tg_watch_cfg.items()},
            }
        self._atomic_json(self._fav_file(), data)

    def _tg_set_fav(self, user_id, chat_id, add):
        with self._tg_lock:
            favs = self.tg_favorites.setdefault(user_id, set())
            favs.add(chat_id) if add else favs.discard(chat_id)
        self._save_favorites()

    def _tg_set_muted(self, user_id, chat_id, add):
        with self._tg_lock:
            m = self.tg_muted.setdefault(user_id, set())
            m.add(chat_id) if add else m.discard(chat_id)
        self._save_favorites()

    # ── Мульти-аккаунты Telegram ──────────────────────────────────

    def _accounts_file(self):
        import os
        from .config import CONFIG_DIR
        return os.path.join(CONFIG_DIR, "tg_accounts.json")

    def _load_accounts(self):
        import os, json as _json
        p = self._accounts_file()
        if not os.path.exists(p):
            return
        try:
            with open(p) as f:
                data = _json.load(f)
            self.tg_accounts = {int(u): v for u, v in data.items()}
        except Exception:
            self.tg_accounts = {}

    def _save_accounts(self):
        self._atomic_json(self._accounts_file(),
                          {str(u): v for u, v in dict(self.tg_accounts).items()})

    def _user_accounts(self, user_id):
        """Аккаунты пользователя. По умолчанию один — 'main'."""
        acc = self.tg_accounts.get(user_id)
        if not acc:
            acc = {"active": "main", "accounts": {"main": "Основной"}}
            self.tg_accounts[user_id] = acc
        return acc

    def _active_slug(self, user_id):
        return self._user_accounts(user_id).get("active", "main")

    def _session_path(self, user_id, slug):
        base = self.config.get("telegram", {}).get("session_file", "~/.vk-tmux-bot/tg_session")
        # 'main' → старый путь (обратная совместимость с уже авторизованной сессией)
        return f"{base}_{user_id}" if slug == "main" else f"{base}_{user_id}_{slug}"

    def _get_tg(self, user_id):
        """Telegram-клиент активного аккаунта пользователя."""
        tg_cfg = self.config.get("telegram", {})
        api_id = tg_cfg.get("api_id", 0)
        api_hash = tg_cfg.get("api_hash", "")
        if not api_id or not api_hash:
            return None
        slug = self._active_slug(user_id)
        key = (user_id, slug)
        # Лок, чтобы два потока не создали двух клиентов на один файл сессии
        with self._clients_lock:
            if key not in self.tg_clients:
                self.tg_clients[key] = TgClient(api_id, api_hash, self._session_path(user_id, slug))
            return self.tg_clients[key]

    def _tg_switch_account(self, peer_id, user_id, slug):
        """Переключить активный аккаунт."""
        acc = self._user_accounts(user_id)
        if slug not in acc["accounts"]:
            self.vk.send_message(peer_id, "❌ Такого аккаунта нет.")
            return
        if slug == acc.get("active"):
            self.vk.send_message(peer_id, "Этот аккаунт уже активен.")
            return
        # Останавливаем ленту/уведомления и отключаем клиент старого аккаунта
        self._tg_stop_live(user_id)
        self._stop_watch(user_id)
        self._clear_open_chat(user_id)
        self.tg_state.pop(user_id, None)
        old_key = (user_id, acc.get("active", "main"))
        with self._clients_lock:
            old = self.tg_clients.pop(old_key, None)
        if old:
            try:
                old.disconnect()
            except Exception:
                pass
        acc["active"] = slug
        self._save_accounts()
        label = acc["accounts"][slug]
        self.vk.send_message(peer_id, f"🔀 Активный аккаунт: {label}")
        self._tg_show_chats(peer_id, user_id, page=0)

    def _tg_show_accounts(self, peer_id, user_id):
        """Показать аккаунты с кнопками переключения/добавления."""
        acc = self._user_accounts(user_id)
        active = acc["active"]
        buttons = []
        for slug, label in acc["accounts"].items():
            mark = "✅ " if slug == active else ""
            buttons.append([{
                "label": f"{mark}{label}"[:40],
                "color": "positive" if slug == active else "primary",
                "payload": f"/tg account {slug}",
            }])
        buttons.append([{"label": "➕ Добавить аккаунт", "color": "secondary", "payload": "/tg addaccount"}])
        buttons.append([{"label": "⬅ К чатам", "color": "secondary", "payload": "/tg back"}])
        kb = make_keyboard(buttons, one_time=False)
        self.vk.send_message(
            peer_id,
            f"🔀 Аккаунты Telegram ({len(acc['accounts'])})\n"
            f"Активный: {acc['accounts'][active]}\n\nПереключить или добавить:",
            keyboard=kb)

    def _tg_logout(self, peer_id, user_id):
        """Выйти из активного Telegram-аккаунта (удалить сессию)."""
        import os
        slug = self._active_slug(user_id)
        # Останавливаем ленту/уведомления
        self._tg_stop_live(user_id)
        self._stop_watch(user_id)
        self._clear_open_chat(user_id)
        self.tg_state.pop(user_id, None)
        # Отключаем и удаляем клиента
        key = (user_id, slug)
        tg = self.tg_clients.pop(key, None)
        if tg:
            try:
                tg.disconnect()
            except Exception:
                pass
        # Удаляем файл сессии
        path = os.path.expanduser(self._session_path(user_id, slug)) + ".session"
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        self.vk.send_message(
            peer_id,
            "🚪 Вы вышли из Telegram.\nЧтобы войти снова: /tg login",
            keyboard=make_tg_menu_keyboard())
        print(f"🚪 user={user_id} вышел из TG ({slug})")

    def _tg_add_account(self, peer_id, user_id):
        """Начать добавление нового аккаунта."""
        self.pending_input[user_id] = "tg_newaccount"
        self.vk.send_message(peer_id, "➕ Название нового аккаунта (например «Рабочий»):")

    def _tg_create_account(self, peer_id, user_id, label):
        """Создать аккаунт и начать вход."""
        import re as _re
        acc = self._user_accounts(user_id)
        # slug из числа существующих
        slug = "acc" + str(len(acc["accounts"]) + 1)
        while slug in acc["accounts"]:
            slug += "x"
        acc["accounts"][slug] = label.strip()[:30] or slug
        acc["active"] = slug
        self._save_accounts()
        # Сбрасываем контекст и запускаем логин для нового аккаунта
        self._tg_stop_live(user_id)
        self.tg_state.pop(user_id, None)
        self.vk.send_message(peer_id, f"✅ Аккаунт «{acc['accounts'][slug]}» создан. Войдите в него:")
        self._tg_login(peer_id, user_id)

    def _cmd_tg(self, peer_id, user_id, args):
        """Главная Telegram-команда.

        /tg              — список чатов
        /tg unread       — только непрочитанные
        /tg find <текст> — поиск чата по имени
        /tg login        — авторизация Telegram
        /tg back         — назад к списку чатов
        """
        if not args:
            self._tg_show_chats(peer_id, user_id)
            return

        parts = args.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if sub == "login":
            self._tg_login(peer_id, user_id)
        elif sub == "back":
            self._tg_show_chats(peer_id, user_id, page=0)
        elif sub == "send":
            self._tg_send(peer_id, user_id, rest)
        elif sub == "chats":
            self._tg_show_chats(peer_id, user_id, page=0)
        elif sub in ("unread", "непрочитанные", "нп"):
            self._tg_show_chats(peer_id, user_id, page=0, only_unread=True)
        elif sub in ("find", "search", "поиск", "найти"):
            if rest.strip():
                self._tg_search(peer_id, user_id, rest.strip())
            else:
                self.pending_input[user_id] = "tg_search"
                kb = make_keyboard([[
                    {"label": "⬅ К чатам", "color": "secondary", "payload": "/tg back"},
                ]], one_time=False)
                self.vk.send_message(
                    peer_id,
                    "🔍 Введите имя человека или чата (можно по-русски):",
                    keyboard=kb,
                )
        else:
            self._tg_show_chats(peer_id, user_id)

    def _tg_login(self, peer_id, user_id):
        """Начать процесс авторизации Telegram (в фоновом потоке)."""
        tg = self._get_tg(user_id)
        if tg is None:
            self.vk.send_message(
                peer_id,
                "❌ Telegram не настроен.\n\n"
                "Добавьте в конфиг:\n"
                "  telegram.api_id\n"
                "  telegram.api_hash\n\n"
                "Получить: https://my.telegram.org → API Development Tools"
            )
            return

        if tg.is_ready:
            self.vk.send_message(peer_id, "✅ Telegram уже подключён!")
            self._tg_show_chats(peer_id, user_id)
            return

        self.vk.send_message(peer_id, "⏳ Подключаюсь к Telegram...")

        def _connect_in_thread():
            try:
                state = tg.connect()
                if state == "need_phone":
                    self.pending_input[user_id] = "tg_phone"
                    self.vk.send_message(peer_id, "📱 Введите номер телефона Telegram (с +):")
                elif state == "ready":
                    self.vk.send_message(peer_id, "✅ Telegram подключён!")
                    self._tg_show_chats(peer_id, user_id)
                else:
                    self.vk.send_message(peer_id, f"⚠️ Статус: {state}")
            except Exception as e:
                self.vk.send_message(peer_id, f"❌ Ошибка подключения: {e}")

        t = threading.Thread(target=_connect_in_thread, daemon=True)
        t.start()

    def _tg_handle_input(self, peer_id, user_id, text):
        """Обработать ввод для Telegram (в фоне чтобы не блочить бота)."""
        mode = self.pending_input.get(user_id, "")
        tg = self._get_tg(user_id)

        if mode == "tg_search":
            self.pending_input.pop(user_id, None)
            self._tg_search(peer_id, user_id, text.strip())
            return

        if mode == "tg_newaccount":
            self.pending_input.pop(user_id, None)
            self._tg_create_account(peer_id, user_id, text.strip())
            return

        if mode in ("tg_phone", "tg_code", "tg_password", "tg_reply"):
            self.pending_input.pop(user_id, None)
            t = threading.Thread(
                target=self._tg_handle_input_async,
                args=(peer_id, user_id, text, mode, tg),
                daemon=True,
            )
            t.start()

    def _tg_handle_input_async(self, peer_id, user_id, text, mode, tg):
        """Фоновое выполнение Telegram-операций."""
        try:
            if mode == "tg_phone":
                ok, msg = tg.send_code(text.strip())
                self.vk.send_message(peer_id, msg)
                if ok:
                    self.pending_input[user_id] = "tg_code"
                else:
                    # Не вышло — даём повторить
                    if "уже использованы" in msg.lower() or "already used" in msg.lower():
                        self.vk.send_message(peer_id, "⚠️ Подождите минуту и попробуйте /tg login снова.")
                    else:
                        self.pending_input[user_id] = "tg_phone"

            elif mode == "tg_code":
                ok, msg = tg.sign_in(text.strip())
                self.vk.send_message(peer_id, msg)
                if not ok and "пароль" in msg.lower():
                    self.pending_input[user_id] = "tg_password"
                elif ok:
                    self._tg_show_chats(peer_id, user_id)
                else:
                    # Неверный код — даём повторить
                    self.pending_input[user_id] = "tg_code"

            elif mode == "tg_password":
                ok, msg = tg.sign_in_password(text.strip())
                self.vk.send_message(peer_id, msg)
                if ok:
                    self._tg_show_chats(peer_id, user_id)
                else:
                    # Неверный пароль — даём повторить
                    self.pending_input[user_id] = "tg_password"

            elif mode == "tg_reply":
                state = self.tg_state.get(user_id, {})
                chat_id = state.get("chat_id")
                if chat_id:
                    self._tg_send(peer_id, user_id, text)
        except Exception as e:
            self.vk.send_message(peer_id, f"❌ Ошибка Telegram: {e}")

    def _tg_ensure_connected(self, tg, peer_id):
        """Подключить TG-клиент если сессия есть но connect ещё не вызван.
        Возвращает True если готов к работе, иначе False (и шлёт сообщение).
        Индикатор подключения показываем только если реально надо подключаться."""
        if tg.is_ready:
            return True
        notice = self.vk.send_message(peer_id, "⏳ Подключаюсь к Telegram…")
        try:
            state = tg.connect()
        except Exception as e:
            self.vk.send_message(peer_id, f"❌ Ошибка подключения к Telegram: {e}")
            return False
        # Убираем индикатор (наше сообщение — удаляется)
        if notice:
            try:
                self.vk.delete_message(peer_id, notice)
            except Exception:
                pass
        if state == "ready":
            return True
        self.vk.send_message(peer_id, "🔑 Нужна авторизация в Telegram. Напишите /tg login")
        return False

    # Размер страницы списка чатов (VK лимит 10 рядов клавиатуры)
    TG_PAGE_SIZE = 5
    # Приоритет типов чатов: личные → группы → каналы
    _TG_KIND_ORDER = {"user": 0, "group": 1, "channel": 2}
    _TG_KIND_ICON = {"user": "👤", "group": "👥", "channel": "📢"}

    def _tg_sort_chats(self, dialogs):
        """Сортировка: личные → группы → каналы (внутри — по свежести)."""
        indexed = sorted(
            enumerate(dialogs),
            key=lambda x: (self._TG_KIND_ORDER.get(x[1][4], 3), x[0]),
        )
        return [c for _, c in indexed]

    def _tg_show_chats(self, peer_id, user_id, page=0, only_unread=False, favorites=False, folder_id=None):
        """Список чатов с пагинацией. only_unread — непрочитанные (без замьюченных),
        favorites — только избранные, folder_id — чаты из папки Telegram."""
        self._tg_stop_live(user_id)  # уходим из чата — глушим живой режим
        self._clear_open_chat(user_id)
        tg = self._get_tg(user_id)
        if tg is None:
            self.vk.send_message(peer_id, "❌ Telegram не настроен.")
            return

        # Вход в Telegram-раздел — сразу досылаем накопившееся из избранного
        if page == 0:
            self._tg_catchup_favorites(user_id, peer_id)

        def _load():
            try:
                if not self._tg_ensure_connected(tg, peer_id):
                    return

                state = self.tg_state.get(user_id, {})
                all_chats = state.get("all_chats") if page > 0 else None
                if all_chats is None:
                    dialogs, err = tg.get_dialogs(limit=100)
                    if err:
                        self.vk.send_message(peer_id, f"❌ {err}")
                        return
                    if not dialogs:
                        self.vk.send_message(peer_id, "📭 Нет чатов.")
                        return
                    all_chats = self._tg_sort_chats(dialogs)

                favs = self.tg_favorites.get(user_id, set())
                muted = self.tg_muted.get(user_id, set())

                # Фильтр по режиму
                if favorites:
                    chats = [c for c in all_chats if c[1] in favs]
                elif folder_id is not None:
                    folders = state.get("folders") or []
                    fmap = {fid: peers for fid, _t, peers in folders}
                    peers = fmap.get(folder_id, set())
                    by_id = {c[1]: c for c in all_chats}
                    resolved = []
                    for pid in peers:
                        if pid in by_id:
                            resolved.append(by_id[pid])
                        else:
                            # чат вне топ-диалогов — резолвим напрямую
                            try:
                                nm = tg.get_entity_name(pid)
                            except Exception:
                                nm = str(pid)
                            s = str(pid)
                            kind = ("channel" if s.startswith("-100")
                                    else ("group" if pid < 0 else "user"))
                            resolved.append((nm, pid, 0, "", kind))
                    chats = self._tg_sort_chats(resolved)
                elif only_unread:
                    # непрочитанные, исключая замьюченные
                    chats = [c for c in all_chats if c[2] > 0 and c[1] not in muted]
                else:
                    chats = all_chats

                # Пустые состояния
                if not chats:
                    prevf = self.tg_state.get(user_id, {}).get("folders")
                    self.tg_state[user_id] = {"view": "chats", "all_chats": all_chats,
                                              "page": 0, "folders": prevf}
                    kb = make_keyboard([[
                        {"label": "📋 Все чаты", "color": "primary", "payload": "/tg page 0"},
                        {"label": "🔍 Поиск", "color": "secondary", "payload": "/tg find"},
                    ]], one_time=False)
                    if favorites:
                        msg = "⭐ В избранном пусто.\nОткройте чат → «⭐ В избранное»."
                    elif folder_id is not None:
                        msg = "📁 В этой папке нет доступных чатов."
                    else:
                        msg = "✅ Непрочитанных нет!"
                    self.vk.send_message(peer_id, msg, keyboard=kb)
                    return

                total = len(chats)
                pages = max(1, (total + self.TG_PAGE_SIZE - 1) // self.TG_PAGE_SIZE)
                page_clamped = max(0, min(page, pages - 1))
                start = page_clamped * self.TG_PAGE_SIZE
                chunk = chats[start:start + self.TG_PAGE_SIZE]

                if favorites:
                    page_cmd = "fpage"
                elif folder_id is not None:
                    page_cmd = f"folder {folder_id}"
                elif only_unread:
                    page_cmd = "upage"
                else:
                    page_cmd = "page"
                buttons = []
                for name, chat_id, unread, preview, kind in chunk:
                    icon = self._TG_KIND_ICON.get(kind, "💬")
                    star = "⭐" if chat_id in favs else ""
                    mute = "🔕" if chat_id in muted else ""
                    badge = f" 🔴{unread}" if unread else ""
                    raw = f"{star}{mute}{icon} {name}{badge}"
                    label = raw[:40] if len(raw) <= 40 else raw[:39] + "…"
                    buttons.append([{
                        "label": label,
                        "color": "positive" if unread else "primary",
                        "payload": f"/tg open {chat_id}",
                    }])

                # Навигация по страницам
                nav = []
                if page_clamped > 0:
                    nav.append({"label": "◀ Назад", "color": "secondary",
                                "payload": f"/tg {page_cmd} {page_clamped - 1}"})
                if page_clamped < pages - 1:
                    nav.append({"label": "Следующие ▶", "color": "secondary",
                                "payload": f"/tg {page_cmd} {page_clamped + 1}"})
                if nav:
                    buttons.append(nav)

                # Переключение режимов
                if favorites or folder_id is not None:
                    mode_row = [{"label": "📋 Все чаты", "color": "primary", "payload": "/tg page 0"},
                                {"label": "📁 Папки", "color": "secondary", "payload": "/tg folders"}]
                elif only_unread:
                    mode_row = [{"label": "📋 Все", "color": "primary", "payload": "/tg page 0"},
                                {"label": "⭐ Избранное", "color": "secondary", "payload": "/tg fav"}]
                else:
                    mode_row = [{"label": "🔴 Непрочитанные", "color": "positive", "payload": "/tg unread"},
                                {"label": "⭐ Избранное", "color": "secondary", "payload": "/tg fav"}]
                buttons.append(mode_row)
                extra = [{"label": "🔍 Поиск", "color": "secondary", "payload": "/tg find"}]
                if not favorites and folder_id is None:
                    extra.append({"label": "📁 Папки", "color": "secondary", "payload": "/tg folders"})
                buttons.append(extra)
                wsec = self.tg_watch_cfg.get(user_id, 0)
                wlabel = f"🔔 Уведомл. ({wsec}с)" if wsec else "🔔 Уведомления"
                buttons.append([{"label": wlabel, "color": "secondary", "payload": "/tg watch"}])

                kb = make_keyboard(buttons, one_time=False)
                # Сохраняем folders в state (для фильтра по папке при пагинации)
                prevf = self.tg_state.get(user_id, {}).get("folders")
                self.tg_state[user_id] = {"view": "chats", "all_chats": all_chats,
                                          "page": page_clamped, "folders": prevf}

                if favorites:
                    title = "⭐ Избранные чаты"
                elif folder_id is not None:
                    fmap = {fid: t for fid, t, _p in (state.get("folders") or [])}
                    title = f"📁 {fmap.get(folder_id, 'Папка')}"
                elif only_unread:
                    title = "🔴 Непрочитанные"
                else:
                    title = "📋 Чаты Telegram"
                unread_total = sum(c[2] for c in all_chats if c[1] not in muted)
                self.vk.send_message(
                    peer_id,
                    f"{title} — стр. {page_clamped + 1}/{pages}\n"
                    f"Показано: {total} | Непрочитано: {unread_total}\n"
                    f"👤 личные · 👥 группы · 📢 каналы   ⭐ избр · 🔕 без уведомл.",
                    keyboard=kb,
                )
            except Exception as e:
                self.vk.send_message(peer_id, f"❌ Ошибка загрузки чатов: {e}")

        threading.Thread(target=_load, daemon=True).start()

    def _tg_show_folders(self, peer_id, user_id):
        """Показать папки Telegram как кнопки."""
        self._tg_stop_live(user_id)
        self._clear_open_chat(user_id)
        tg = self._get_tg(user_id)
        if tg is None:
            self.vk.send_message(peer_id, "❌ Telegram не настроен.")
            return

        def _load():
            try:
                if not self._tg_ensure_connected(tg, peer_id):
                    return
                folders, err = tg.get_folders()
                if err:
                    self.vk.send_message(peer_id, f"❌ {err}")
                    return
                if not folders:
                    kb = make_keyboard([[
                        {"label": "📋 Все чаты", "color": "primary", "payload": "/tg page 0"},
                    ]], one_time=False)
                    self.vk.send_message(peer_id, "📁 Папок в Telegram нет.", keyboard=kb)
                    return

                # Кэшируем папки в state для последующей фильтрации
                st = self.tg_state.get(user_id, {})
                st["folders"] = folders
                st["view"] = "folders"
                self.tg_state[user_id] = st

                buttons = []
                for fid, title, peers in folders[:9]:  # VK лимит рядов
                    buttons.append([{
                        "label": f"📁 {title} ({len(peers)})"[:40],
                        "color": "primary",
                        "payload": f"/tg folder {fid}",
                    }])
                buttons.append([
                    {"label": "📋 Все чаты", "color": "secondary", "payload": "/tg page 0"},
                    {"label": "🔍 Поиск", "color": "secondary", "payload": "/tg find"},
                ])
                kb = make_keyboard(buttons, one_time=False)
                self.vk.send_message(
                    peer_id,
                    f"📁 Папки Telegram ({len(folders)})\nВыберите папку:",
                    keyboard=kb,
                )
            except Exception as e:
                self.vk.send_message(peer_id, f"❌ Ошибка папок: {e}")

        threading.Thread(target=_load, daemon=True).start()

    def _tg_search(self, peer_id, user_id, query):
        """Найти чаты по имени (в фоне)."""
        self._tg_stop_live(user_id)  # уходим из чата — глушим живой режим
        self._clear_open_chat(user_id)
        tg = self._get_tg(user_id)
        if tg is None:
            self.vk.send_message(peer_id, "❌ Telegram не настроен.")
            return

        def _load():
            try:
                if not self._tg_ensure_connected(tg, peer_id):
                    return

                state = self.tg_state.get(user_id, {})
                all_chats = state.get("all_chats")
                if all_chats is None:
                    dialogs, err = tg.get_dialogs(limit=100)
                    if err:
                        self.vk.send_message(peer_id, f"❌ {err}")
                        return
                    all_chats = self._tg_sort_chats(dialogs or [])

                # Нечёткий поиск: прямое совпадение ИЛИ через транслитерацию
                q = query.lower()
                tq = _translit(query)
                found = [
                    c for c in all_chats
                    if q in c[0].lower() or tq in _translit(c[0])
                ]

                if not found:
                    kb = make_keyboard([[
                        {"label": "📋 Все чаты", "color": "primary", "payload": "/tg page 0"},
                        {"label": "🔍 Искать снова", "color": "secondary", "payload": "/tg find"},
                    ]], one_time=False)
                    self.vk.send_message(peer_id, f"🔍 По запросу «{query}» ничего не найдено.", keyboard=kb)
                    return

                # Показываем до 8 результатов
                buttons = []
                for name, chat_id, unread, preview, kind in found[:8]:
                    icon = self._TG_KIND_ICON.get(kind, "💬")
                    badge = f" 🔴{unread}" if unread else ""
                    raw = f"{icon} {name}{badge}"
                    label = raw[:40] if len(raw) <= 40 else raw[:39] + "…"
                    buttons.append([{
                        "label": label,
                        "color": "positive" if unread else "primary",
                        "payload": f"/tg open {chat_id}",
                    }])
                buttons.append([
                    {"label": "📋 Все чаты", "color": "primary", "payload": "/tg page 0"},
                    {"label": "🔍 Искать снова", "color": "secondary", "payload": "/tg find"},
                ])

                kb = make_keyboard(buttons, one_time=False)
                self.tg_state[user_id] = {"view": "chats", "all_chats": all_chats, "page": 0}
                more = f" (показаны первые 8 из {len(found)})" if len(found) > 8 else ""
                self.vk.send_message(
                    peer_id,
                    f"🔍 Найдено по «{query}»: {len(found)}{more}",
                    keyboard=kb,
                )
            except Exception as e:
                self.vk.send_message(peer_id, f"❌ Ошибка поиска: {e}")

        threading.Thread(target=_load, daemon=True).start()

    def _tg_chat_display_name(self, user_id, chat_id):
        """Имя чата из state/кэша без лишних запросов."""
        st = self.tg_state.get(user_id, {})
        if st.get("chat_id") == chat_id and st.get("chat_name"):
            return st["chat_name"]
        for c in (st.get("all_chats") or []):
            if c[1] == chat_id:
                return c[0]
        return "Чат"

    def _tg_toggle_confirm(self, peer_id, user_id, chat_id, text):
        """Короткое подтверждение действия. Если пользователь в этом чате —
        обновляем клавиатуру (кнопки ⭐/🔕 отражают новое состояние)."""
        live = self.tg_live.get(user_id)
        in_this_chat = live and live.get("chat_id") == chat_id
        kb = None
        if in_this_chat:
            kb = self._tg_chat_kb(chat_id, user_id, topic_id=live.get("topic_id"))
        self.vk.send_message(peer_id, f"✅ {text}", keyboard=kb)

    # Клавиатура внутри диалога (печать = отправка, кнопки для навигации)
    def _tg_chat_kb(self, chat_id, user_id, topic_id=None):
        is_fav = chat_id in self.tg_favorites.get(user_id, set())
        is_muted = chat_id in self.tg_muted.get(user_id, set())
        fav_btn = ({"label": "★ Из избранного", "color": "secondary", "payload": f"/tg unfav {chat_id}"}
                   if is_fav else
                   {"label": "⭐ В избранное", "color": "secondary", "payload": f"/tg fav {chat_id}"})
        mute_btn = ({"label": "🔔 Вернуть", "color": "secondary", "payload": f"/tg unmute {chat_id}"}
                    if is_muted else
                    {"label": "🔕 Исключить", "color": "secondary", "payload": f"/tg mute {chat_id}"})
        # В топике «назад» ведёт к списку топиков, иначе — к чатам
        back_btn = ({"label": "⬅ К топикам", "color": "primary", "payload": f"/tg topics {chat_id}"}
                    if topic_id else
                    {"label": "⬅ К чатам", "color": "primary", "payload": "/tg back"})
        return make_keyboard([
            [
                back_btn,
                fav_btn,
            ],
            [
                mute_btn,
                {"label": "🔴 Непрочитанные", "color": "secondary", "payload": "/tg unread"},
            ],
            [
                {"label": "🔍 Поиск", "color": "secondary", "payload": "/tg find"},
            ],
        ], one_time=False)

    def _maybe_transcribe(self, peer_id, mid, media):
        """Для входящего голосового — дождаться встроенной расшифровки VK и
        прислать её текстом отдельной строкой. Работает через механизм самого
        ВКонтакте (messages.getById → audio_message.transcript)."""
        if not mid or not media or media.get("kind") != "voice":
            return
        def _poll():
            # Семафор ограничивает число одновременных опросов (пачка голосовых
            # не разведёт десятки потоков по rate-limited VK API).
            if not self._transcribe_sem.acquire(timeout=30):
                return
            try:
                for _ in range(12):          # до ~36с ожидания
                    time.sleep(3.0)
                    state, text = self.vk.get_audio_transcript(mid)
                    if state == "done":
                        if text and text.strip():
                            self.vk.send_message(peer_id, f"📝 {text.strip()}")
                        return
            finally:
                self._transcribe_sem.release()
        threading.Thread(target=_poll, daemon=True).start()

    def _send_media_bubble(self, user_id, peer_id, chat_id, msg_id, media, bubble, kb=None):
        """Отправить баббл с медиа в VK (+ авто-расшифровка голосового). В фоне."""
        def _work():
            tg = self._get_tg(user_id)
            att = None
            if tg:
                att = self._tg_proxy_incoming_media(tg, user_id, peer_id, chat_id, msg_id, media)
            try:
                mid = self.vk.send_message(peer_id, bubble, keyboard=kb, attachment=att)
                if att:
                    self._maybe_transcribe(peer_id, mid, media)
            except Exception:
                pass
        threading.Thread(target=_work, daemon=True).start()

    def _tg_proxy_incoming_async(self, user_id, peer_id, chat_id, msg_id, media, bubble, kb):
        """Совместимый враппер: отправка медиа-баббла (+расшифровка голосового)."""
        self._send_media_bubble(user_id, peer_id, chat_id, msg_id, media, bubble, kb)

    def _mark_vk_sent(self, user_id, msg_id):
        """Запомнить id сообщения, отправленного ИЗ VK — чтобы живая лента
        не показала его повторно (исходящие с телефона она покажет, эти — нет)."""
        if not msg_id:
            return
        with self._tg_fav_lock:
            dq = self._tg_vk_sent.get(user_id)
            if dq is None:
                dq = deque(maxlen=2000)
                self._tg_vk_sent[user_id] = dq
            dq.append(msg_id)

    def _is_vk_sent(self, user_id, msg_id):
        with self._tg_fav_lock:
            dq = self._tg_vk_sent.get(user_id)
            return bool(dq) and msg_id in dq

    def _select_context_media(self, msgs):
        """Какие сообщения из истории подгрузить реальными медиа при открытии чата.
        Только свежий хвост (последние 4 сообщения, максимум 3 медиа) — чтобы не
        выгребать старую историю."""
        tail = (msgs or [])[-4:]
        media_msgs = [m for m in tail
                      if m[5] and m[5].get("kind") in ("photo", "voice", "video", "video_note", "file")]
        return media_msgs[-3:]

    def _tg_load_context_media(self, tg, user_id, peer_id, chat_id, media_msgs):
        """Подгрузить выбранные медиа-сообщения отдельными бабблами (в фоне).
        Пустые/битые загрузки молча пропускаются — без дублей-пустышек."""
        if not media_msgs:
            return

        def _work():
            for msg_id, sender, text, date, is_out, media in media_msgs:
                try:
                    att = self._tg_proxy_incoming_media(tg, user_id, peer_id, chat_id, msg_id, media)
                    if att:
                        bubble = self._tg_format_msg(sender, text, date, is_out, media)
                        mid = self.vk.send_message(peer_id, bubble, attachment=att)
                        self._maybe_transcribe(peer_id, mid, media)
                except Exception:
                    pass
        threading.Thread(target=_work, daemon=True).start()

    def _tg_proxy_incoming_media(self, tg, user_id, peer_id, chat_id, msg_id, media):
        """Скачать медиа из TG и загрузить в VK. Возвращает строку вложения или None."""
        import os
        path = None
        try:
            path = tg.download_media(chat_id, msg_id)
            if not path or not os.path.exists(path):
                return None
            size = os.path.getsize(path)
            # VK лимиты: фото до ~50МБ, док до ~200МБ. Ограничим разумно.
            if size > 190 * 1024 * 1024:
                return None
            kind = media.get("kind")
            if kind == "photo":
                return self.vk.upload_photo(peer_id, path)
            if kind == "voice":
                # голосовое → VK voice (если .ogg); иначе как файл
                if path.lower().endswith((".ogg", ".oga")):
                    try:
                        return self.vk.upload_voice(peer_id, path)
                    except Exception:
                        pass
                return self.vk.upload_doc(peer_id, path, title="голосовое.ogg")
            if kind in ("video", "video_note"):
                # Кружок/видео → грузим как проигрываемое видео VK (круглого формата нет)
                title = "видео-кружок" if kind == "video_note" else (media.get("name") or "видео")
                att = self.vk.upload_video(peer_id, path, title=title)
                if att:
                    return att
                # не вышло видео — отдаём файлом, чтобы сообщение не потерялось
                return self.vk.upload_doc(peer_id, path, title=title + ".mp4")
            # прочие файлы → документ
            title = media.get("name") or "файл"
            return self.vk.upload_doc(peer_id, path, title=title)
        except Exception as e:
            print(f"⚠️ медиа TG→VK не удалось: {e}")
            return None
        finally:
            if path:
                try:
                    os.remove(path)
                except Exception:
                    pass

    def _media_marker(self, media):
        """Текстовая метка медиа для отображения."""
        if not media:
            return ""
        kind = media.get("kind")
        if kind == "photo":
            return "📷 фото"
        if kind == "voice":
            return "🎤 голосовое"
        if kind == "video_note":
            return "⭕ видео-кружок"
        if kind == "video":
            return "🎥 видео"
        name = media.get("name", "файл")
        return f"📎 {name}"

    def _tg_format_msg(self, sender, text, date, is_out, media=None):
        """Оформить одно сообщение с цветным маркером (🔵 — ты, 🟢 — собеседник)."""
        t = date.strftime("%H:%M") if date else ""
        if is_out:
            head = f"🔵 Вы · {t}".rstrip(" ·")        # исходящее (синий)
        else:
            head = f"🟢 {sender} · {t}".rstrip(" ·")   # входящее (зелёный)
        marker = self._media_marker(media)
        body = text
        if marker:
            body = f"{marker}\n{text}".rstrip() if text else marker
        return f"{head}\n{body}"

    def _tg_context_card(self, chat_name, msgs, skip_ids=None):
        """Стартовая карточка с последними сообщениями (контекст при открытии).
        skip_ids — id сообщений, которые уйдут отдельными медиа-бабблами
        (не дублируем их маркером в карточке)."""
        skip_ids = skip_ids or set()
        header = (
            f"💬 {chat_name}   📡 на связи\n"
            f"✍️ просто напишите — уйдёт собеседнику\n"
            + "━" * 18
        )
        blocks = [self._tg_format_msg(s, txt, d, o, media)
                  for (_id, s, txt, d, o, media) in (msgs or [])
                  if _id not in skip_ids]
        body = ""
        for b in reversed(blocks):
            chunk = b + "\n\n"
            if len(header) + 2 + len(body) + len(chunk) > 3900:
                break
            body = chunk + body
        return header + "\n\n" + (body.rstrip() or "(нет сообщений)")

    def _tg_show_topics(self, peer_id, user_id, chat_id):
        """Показать топики форума как кнопки."""
        self._tg_stop_live(user_id)
        self._clear_open_chat(user_id)
        tg = self._get_tg(user_id)
        if tg is None:
            self.vk.send_message(peer_id, "❌ Telegram не настроен.")
            return

        def _load():
            try:
                if not self._tg_ensure_connected(tg, peer_id):
                    return
                chat_name = tg.get_entity_name(chat_id)
                topics, err = tg.get_topics(chat_id)
                if err:
                    self.vk.send_message(peer_id, f"❌ {err}")
                    return
                if not topics:
                    # Форум без топиков — открываем как обычный чат
                    self._tg_open_conversation(peer_id, user_id, chat_id, None)
                    return

                st = self.tg_state.get(user_id, {})
                st["topics"] = topics
                st["topics_chat"] = chat_id
                st["topics_name"] = chat_name
                self.tg_state[user_id] = st

                buttons = []
                for tid, title, unread in topics[:9]:
                    badge = f" 🔴{unread}" if unread else ""
                    buttons.append([{
                        "label": f"# {title}{badge}"[:40],
                        "color": "positive" if unread else "primary",
                        "payload": f"/tg topic {chat_id} {tid}",
                    }])
                buttons.append([{"label": "⬅ К чатам", "color": "secondary", "payload": "/tg back"}])
                kb = make_keyboard(buttons, one_time=False)
                self.vk.send_message(
                    peer_id,
                    f"🌳 {chat_name} — топики ({len(topics)})\nВыберите топик:",
                    keyboard=kb,
                )
            except Exception as e:
                self.vk.send_message(peer_id, f"❌ Ошибка топиков: {e}")

        threading.Thread(target=_load, daemon=True).start()

    def _tg_show_messages(self, peer_id, user_id, chat_id, reuse=False, topic_id=None):
        """Открыть чат/топик. Форум без указанного топика → показать список топиков."""
        tg = self._get_tg(user_id)
        if tg is None:
            self.vk.send_message(peer_id, "❌ Telegram не настроен.")
            return

        def _pre():
            try:
                if not self._tg_ensure_connected(tg, peer_id):
                    return
                # Форум и топик не выбран → показать топики
                if topic_id is None and tg.is_forum(chat_id):
                    self._tg_show_topics(peer_id, user_id, chat_id)
                    return
                self._tg_open_conversation(peer_id, user_id, chat_id, topic_id)
            except Exception as e:
                self.vk.send_message(peer_id, f"❌ Ошибка: {e}")

        threading.Thread(target=_pre, daemon=True).start()

    def _tg_open_conversation(self, peer_id, user_id, chat_id, topic_id):
        """Открыть диалог/топик: контекст + живая лента."""
        tg = self._get_tg(user_id)
        try:
            chat_name = tg.get_entity_name(chat_id)
            msgs, err = tg.get_messages(chat_id, limit=15, topic_id=topic_id)
            if err:
                self.vk.send_message(peer_id, f"❌ {err}")
                return

            # Заголовок с названием топика если есть
            display = chat_name
            if topic_id:
                st = self.tg_state.get(user_id, {})
                tmap = {t[0]: t[1] for t in (st.get("topics") or [])}
                tname = tmap.get(topic_id)
                display = f"{chat_name} › {tname}" if tname else f"{chat_name} › топик"

            # Выбираем медиа из свежего хвоста для загрузки реальными бабблами.
            # Чисто-медийные (без подписи) убираем из карточки, чтобы не дублировать.
            media_sel = self._select_context_media(msgs)
            skip_ids = {m[0] for m in media_sel if not (m[2] or "").strip()}

            card = self._tg_context_card(display, msgs, skip_ids=skip_ids)
            kb = self._tg_chat_kb(chat_id, user_id, topic_id=topic_id)
            self.vk.send_message(peer_id, card, keyboard=kb)

            # Полный контекст: реальные медиа отдельными бабблами (в фоне).
            self._tg_load_context_media(tg, user_id, peer_id, chat_id, media_sel)

            last_id = max((m[0] for m in msgs), default=0)
            prev = self.tg_state.get(user_id, {})
            self.tg_state[user_id] = {
                "view": "chat", "chat_id": chat_id, "chat_name": chat_name,
                "topic_id": topic_id, "display": display,
                "all_chats": prev.get("all_chats"),
                "topics": prev.get("topics"), "topics_chat": prev.get("topics_chat"),
            }
            tg.mark_read(chat_id)
            self._save_open_chat(user_id, chat_id, topic_id)  # для восстановления после ребута
            self._tg_start_live(peer_id, user_id, chat_id, display, last_id, topic_id)
        except Exception as e:
            self.vk.send_message(peer_id, f"❌ Ошибка загрузки: {e}")

    # ── Живой режим чата ──────────────────────────────────────────

    def _tg_start_live(self, peer_id, user_id, chat_id, chat_name, last_id, topic_id=None):
        """Запустить живую ленту чата/топика (постит новые входящие бабблами)."""
        self._tg_stop_live(user_id)
        self.tg_live[user_id] = {
            "chat_id": chat_id, "peer_id": peer_id, "chat_name": chat_name,
            "stop": False, "last_id": last_id, "topic_id": topic_id,
        }
        t = threading.Thread(target=self._tg_live_loop, args=(user_id,), daemon=True)
        self._tg_live_threads[user_id] = t
        t.start()
        print(f"📡 user={user_id} живая лента: {chat_name}")

    def _tg_stop_live(self, user_id):
        """Остановить живую ленту."""
        live = self.tg_live.get(user_id)
        if live:
            live["stop"] = True
        self.tg_live.pop(user_id, None)
        self._tg_live_threads.pop(user_id, None)

    # ── Глобальные уведомления (watch) ────────────────────────────

    _WATCH_PRESETS = [("30 сек", 30), ("1 мин", 60), ("5 мин", 300),
                      ("10 мин", 600), ("15 мин", 900)]

    def _tg_watch_menu(self, peer_id, user_id):
        """Показать меню выбора интервала уведомлений."""
        cur = self.tg_watch_cfg.get(user_id, 0)
        status = "🔔 включены" if cur else "🔕 выключены"
        cur_txt = f", каждые {cur}с" if cur else ""
        rows = []
        row = []
        for label, sec in self._WATCH_PRESETS:
            mark = "✅ " if sec == cur else ""
            row.append({"label": f"{mark}{label}", "color": ("positive" if sec == cur else "secondary"),
                        "payload": f"/tg watch {sec}"})
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([{"label": "🔕 Выключить", "color": "negative", "payload": "/tg watch off"}])
        rows.append([{"label": "⬅ К чатам", "color": "primary", "payload": "/tg back"}])
        kb = make_keyboard(rows, one_time=False)
        self.vk.send_message(
            peer_id,
            f"🔔 Уведомления о новых сообщениях: {status}{cur_txt}\n\n"
            f"Бот будет проверять чаты с выбранной периодичностью и присылать,\n"
            f"кто написал. Замьюченные (🔕) чаты не проверяются.\n\n"
            f"Выберите интервал:",
            keyboard=kb,
        )

    def _tg_watch_set(self, peer_id, user_id, arg):
        """Установить интервал уведомлений или показать меню."""
        if not arg:
            self._tg_watch_menu(peer_id, user_id)
            return
        if arg in ("off", "выкл", "0", "стоп"):
            self._tg_stop_watch(user_id)
            self.tg_watch_cfg[user_id] = 0
            self._save_favorites()
            self.vk.send_message(peer_id, "🔕 Уведомления выключены.")
            return
        try:
            sec = int(arg)
        except ValueError:
            self._tg_watch_menu(peer_id, user_id)
            return
        sec = max(30, min(sec, 3600))
        self.tg_watch_cfg[user_id] = sec
        self._save_favorites()
        self._start_watch(peer_id, user_id, sec)
        self.vk.send_message(
            peer_id,
            f"🔔 Уведомления включены — проверка каждые {sec}с.\n"
            f"Пришлю, когда в чатах появятся новые сообщения (кроме 🔕).",
        )

    def _start_watch(self, peer_id, user_id, interval):
        """Запустить фоновый мониторинг новых сообщений."""
        self._stop_watch(user_id)
        self.tg_watch[user_id] = {
            "interval": interval, "peer_id": peer_id, "stop": False, "seen": None,
        }
        t = threading.Thread(target=self._tg_watch_loop, args=(user_id,), daemon=True)
        self._tg_watch_threads[user_id] = t
        t.start()
        print(f"🔔 user={user_id} уведомления каждые {interval}с")

    def _stop_watch(self, user_id):
        w = self.tg_watch.get(user_id)
        if w:
            w["stop"] = True
        self.tg_watch.pop(user_id, None)
        self._tg_watch_threads.pop(user_id, None)

    def _tg_watch_loop(self, user_id):
        """Мониторинг: раз в интервал проверяет непрочитанные и шлёт уведомления."""
        tg = self._get_tg(user_id)
        my_w = self.tg_watch.get(user_id)   # мой словарь — по идентичности
        if not my_w:
            return
        while True:
            if self.tg_watch.get(user_id) is not my_w or my_w.get("stop"):
                break
            interval = my_w.get("interval", 60)
            peer_id = my_w.get("peer_id")

            # Ждём интервал кусочками (чтобы быстро остановиться)
            waited = 0.0
            while waited < interval:
                if self.tg_watch.get(user_id) is not my_w or my_w.get("stop"):
                    if self.tg_watch.get(user_id) is my_w:
                        self._tg_watch_threads.pop(user_id, None)
                    return
                time.sleep(1.0)
                waited += 1.0

            try:
                if not tg or not tg.is_ready:
                    continue
                dialogs, err = tg.get_dialogs(limit=100)
                if err:
                    continue

                muted = self.tg_muted.get(user_id, set())
                # Текущий открытый чат — его ведёт живая лента, не дублируем
                open_chat = (self.tg_live.get(user_id) or {}).get("chat_id")

                # Снимок непрочитанных: {chat_id: unread}
                snapshot = {c[1]: c[2] for c in dialogs}
                seen = my_w.get("seen")
                if seen is None:
                    # Первый проход — базовая линия, без уведомлений.
                    # Для избранных запоминаем last_id, чтобы потом не вывалить историю.
                    my_w["seen"] = snapshot
                    fl = self._tg_fav_last.setdefault(user_id, {})
                    favs0 = self.tg_favorites.get(user_id, set())
                    for _n, cid, unread0, _p, _k in dialogs:
                        if cid in favs0 and unread0 > 0 and cid not in fl:
                            m0, e0 = tg.get_messages(cid, limit=1)
                            if not e0 and m0:
                                fl[cid] = m0[-1][0]
                    continue

                favs = self.tg_favorites.get(user_id, set())

                # Уведомления ТОЛЬКО по избранным чатам — остальные не трогаем.
                for name, chat_id, unread, preview, kind in dialogs:
                    if chat_id not in favs or chat_id in muted or chat_id == open_chat:
                        continue
                    old = seen.get(chat_id, 0)
                    if unread > old and unread > 0:
                        icon = self._TG_KIND_ICON.get(kind, "💬")
                        self._tg_push_favorite(user_id, peer_id, chat_id, name,
                                               icon, unread)

                my_w["seen"] = snapshot
            except Exception:
                pass

        if self.tg_watch.get(user_id) is my_w:
            self._tg_watch_threads.pop(user_id, None)

    def _tg_push_favorite(self, user_id, peer_id, chat_id, name, icon, unread):
        """Пуш реальных новых сообщений из ИЗБРАННОГО чата бабблами (текст+медиа)
        с кнопкой перехода в диалог. Дедуп по последнему показанному id
        (общее хранилище self._tg_fav_last — не дублирует watch и догоняющий опрос)."""
        tg = self._get_tg(user_id)
        if not tg or not tg.is_ready:
            return
        try:
            msgs, err = tg.get_messages(chat_id, limit=min(max(unread, 1), 20))
            if err or not msgs:
                return
            newmax = max((m[0] for m in msgs), default=0)
            # Атомарный claim: под локом читаем since и сразу двигаем указатель,
            # чтобы watch и догоняющий опрос не запушили одно и то же.
            with self._tg_fav_lock:
                fav_last = self._tg_fav_last.setdefault(user_id, {})
                since = fav_last.get(chat_id, 0)
                fresh = [m for m in msgs if m[0] > since and not m[4]]
                fav_last[chat_id] = max(newmax, since)
            if not fresh:
                return
            # Шапка избранного, затем сами сообщения бабблами
            self.vk.send_message(peer_id, f"⭐ {icon} {name}")
            for msg_id, sender, text, date, is_out, media in fresh[-5:]:
                bubble = self._tg_format_msg(sender, text, date, is_out, media)
                if media and media.get("kind") in ("photo", "file", "voice", "video", "video_note"):
                    self._tg_proxy_incoming_async(user_id, peer_id, chat_id, msg_id, media, bubble, None)
                else:
                    self.vk.send_message(peer_id, bubble)
            # Навигационный футер (гарантированно последним, с полными кнопками)
            nav = make_keyboard([
                [{"label": "➡️ Перейти в диалог", "color": "primary",
                  "payload": f"/tg open {chat_id}"}],
                [{"label": "📋 К чатам", "color": "secondary", "payload": "/tg back"},
                 {"label": "🔕 Не уведомлять", "color": "secondary",
                  "payload": f"/tg unfav {chat_id}"}],
            ], one_time=False)
            self.vk.send_message(peer_id, "───", keyboard=nav)
        except Exception:
            pass

    def _tg_catchup_favorites(self, user_id, peer_id):
        """Догоняющий опрос: при входе в Telegram-меню сразу досылаем новые
        сообщения из избранных чатов (то, что накопилось, пока вы не смотрели).
        Троттлится, дедуп общий с watch — повторов не будет."""
        favs = self.tg_favorites.get(user_id, set())
        if not favs:
            return
        now = time.time()
        if now - self._tg_catchup_ts.get(user_id, 0) < 8:
            return  # не гоняем на каждый чих навигации
        self._tg_catchup_ts[user_id] = now

        def _work():
            tg = self._get_tg(user_id)
            if not tg or not tg.is_ready:
                return
            try:
                dialogs, err = tg.get_dialogs(limit=100)
                if err or not dialogs:
                    return
                muted = self.tg_muted.get(user_id, set())
                open_chat = (self.tg_live.get(user_id) or {}).get("chat_id")
                for name, chat_id, unread, preview, kind in dialogs:
                    if chat_id not in favs or chat_id in muted or chat_id == open_chat:
                        continue
                    if unread <= 0:
                        continue  # непрочитанных нет — догонять нечего
                    # Доставляем накопившиеся непрочитанные (дедуп по self._tg_fav_last)
                    icon = self._TG_KIND_ICON.get(kind, "💬")
                    self._tg_push_favorite(user_id, peer_id, chat_id, name, icon, unread)
            except Exception:
                pass
        threading.Thread(target=_work, daemon=True).start()

    def _resume_watches(self):
        """Восстановить сохранённые уведомления после перезапуска бота."""
        for user_id, sec in list(self.tg_watch_cfg.items()):
            if sec and sec >= 30:
                peer_id = user_id  # ЛС = peer_id пользователя
                self._start_watch(peer_id, user_id, sec)

    def _resume_tg_sessions(self):
        """После ребута — восстановить открытые чаты: показать где остановились
        (последние сообщения для «дочитать») и снова включить живую ленту."""
        for user_id, info in list(self.tg_open_chat.items()):
            chat_id = info.get("chat_id")
            topic_id = info.get("topic_id")
            if not chat_id:
                continue
            peer_id = user_id
            # В отдельном потоке: connect может занять пару секунд
            threading.Thread(
                target=self._resume_one_chat,
                args=(peer_id, user_id, chat_id, topic_id),
                daemon=True,
            ).start()

    def _resume_one_chat(self, peer_id, user_id, chat_id, topic_id):
        try:
            tg = self._get_tg(user_id)
            if tg is None:
                return
            state = tg.connect()
            if state != "ready":
                return  # не авторизован — не трогаем
            chat_name = tg.get_entity_name(chat_id)
            msgs, err = tg.get_messages(chat_id, limit=15, topic_id=topic_id)
            if err:
                return
            display = chat_name
            if topic_id:
                display = f"{chat_name} › топик"
            # Компактно: не вываливаем всю карточку, а даём вернуться одним тапом.
            # Живую ленту запускаем — новые входящие появятся сами.
            payload = f"/tg topic {chat_id} {topic_id}" if topic_id else f"/tg open {chat_id}"
            kb = make_keyboard([[
                {"label": f"↩️ Открыть {display}"[:40], "color": "primary", "payload": payload},
            ]], one_time=False)
            self.vk.send_message(
                peer_id, f"🔄 После перезапуска: вы были в диалоге с «{display}».", keyboard=kb)
            last_id = max((m[0] for m in msgs), default=0)
            prev = self.tg_state.get(user_id, {})
            self.tg_state[user_id] = {
                "view": "chat", "chat_id": chat_id, "chat_name": chat_name,
                "topic_id": topic_id, "display": display,
                "all_chats": prev.get("all_chats"),
            }
            self._tg_start_live(peer_id, user_id, chat_id, display, last_id, topic_id)
            print(f"🔄 user={user_id} восстановлен чат: {display}")
        except Exception as e:
            print(f"⚠️ Не удалось восстановить чат user={user_id}: {e}")

    def _tg_live_loop(self, user_id):
        """Поллинг чата: новые сообщения постятся отдельными бабблами —
        полная синхронизация диалога, как в настоящем клиенте.

        Показываем и входящие, и исходящие с ДРУГИХ устройств (телефон/десктоп).
        Отправленные из самого VK пропускаем по id (иначе дубль).
        """
        tg = self._get_tg(user_id)
        my_live = self.tg_live.get(user_id)   # мой словарь — по идентичности отличаю себя
        if not my_live:
            return
        interval = 2.0
        errors = 0
        while True:
            # Проверка идентичности: если запущен новый цикл — этот завершается
            if self.tg_live.get(user_id) is not my_live or my_live.get("stop"):
                break

            time.sleep(interval)

            if self.tg_live.get(user_id) is not my_live or my_live.get("stop"):
                break

            chat_id = my_live["chat_id"]
            peer_id = my_live["peer_id"]
            last_id = my_live.get("last_id", 0)
            topic_id = my_live.get("topic_id")
            try:
                if not tg or not tg.is_ready:
                    break
                # Окно с запасом: если между поллами прилетело много сообщений,
                # берём больше, чтобы не потерять старые из пачки.
                msgs, err = tg.get_messages(chat_id, limit=40, topic_id=topic_id)
                if err:
                    errors += 1
                    if errors >= 10:
                        break  # чат недоступен — прекращаем без спама
                    time.sleep(min(errors * 2, 20))  # backoff
                    continue
                errors = 0

                new_msgs = sorted((m for m in msgs if m[0] > last_id), key=lambda m: m[0])
                if not new_msgs:
                    continue

                # Двигаем указатель СРАЗУ (за все новые) — не теряем сообщения
                my_live["last_id"] = max(m[0] for m in new_msgs)

                got_incoming = False
                for msg_id, sender, text, date, is_out, media in new_msgs:
                    # Отправленное из самого VK не дублируем
                    if is_out and self._is_vk_sent(user_id, msg_id):
                        continue
                    if not is_out:
                        got_incoming = True
                    bubble = self._tg_format_msg(sender, text, date, is_out, media)
                    kb = self._tg_chat_kb(chat_id, user_id, topic_id=topic_id)
                    if media and media.get("kind") in ("photo", "file", "voice", "video", "video_note"):
                        # Медиа проксируем в ОТДЕЛЬНОМ потоке, чтобы не блокировать ленту
                        self._tg_proxy_incoming_async(user_id, peer_id, chat_id, msg_id, media, bubble, kb)
                    else:
                        self.vk.send_message(peer_id, bubble, keyboard=kb)

                if got_incoming:
                    tg.mark_read(chat_id)
            except Exception:
                errors += 1
                if errors >= 10:
                    break
                time.sleep(min(errors * 2, 20))

        # Снимаем регистрацию ТОЛЬКО если это всё ещё мой поток —
        # иначе снесём регистрацию свежезапущенной ленты (identity-guard).
        if self.tg_live.get(user_id) is my_live:
            self._tg_live_threads.pop(user_id, None)

    def _tg_send_media(self, peer_id, user_id, attachments, caption):
        """Скачать вложения из VK и отправить в активный чат Telegram (в фоне)."""
        state = self.tg_state.get(user_id, {})
        chat_id = state.get("chat_id")
        topic_id = state.get("topic_id")
        if not chat_id:
            self.vk.send_message(peer_id, "❌ Сначала выберите чат.")
            return
        tg = self._get_tg(user_id)
        if tg is None:
            return

        def _work():
            import os, tempfile, requests as _rq
            sent, skipped = 0, 0
            for att in attachments:
                atype = att.get("type")
                url, fname, is_voice = None, None, False
                if atype == "photo":
                    sizes = att.get("photo", {}).get("sizes", [])
                    if sizes:
                        best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
                        url = best.get("url")
                        fname = "photo.jpg"
                elif atype == "audio_message":
                    # голосовое из VK → голосовое в TG
                    am = att.get("audio_message", {})
                    url = am.get("link_ogg") or am.get("link_mp3")
                    fname = "voice.ogg"
                    is_voice = True
                elif atype == "doc":
                    doc = att.get("doc", {})
                    url = doc.get("url")
                    fname = doc.get("title", "file")
                    ext = doc.get("ext", "")
                    if ext and not fname.endswith(ext):
                        fname = f"{fname}.{ext}"
                    # VK-голосовое иногда приходит как doc audio_message
                    if att.get("doc", {}).get("type") == 5:
                        is_voice = True
                if not url:
                    skipped += 1
                    continue
                path = None
                try:
                    r = _rq.get(url, timeout=120)
                    r.raise_for_status()
                    safe = "".join(c for c in fname if c.isalnum() or c in "._- ") or "file"
                    # уникальное имя — без коллизий при одинаковых именах
                    path = os.path.join(tempfile.gettempdir(), f"vk_{user_id}_{sent}_{safe}")
                    with open(path, "wb") as f:
                        f.write(r.content)
                    ok, m = tg.send_file(chat_id, path, caption=caption if sent == 0 else "",
                                         topic_id=topic_id, voice=is_voice)
                    if ok:
                        sent += 1
                        self._mark_vk_sent(user_id, m)  # m — id, лента не продублирует
                    else:
                        self.vk.send_message(peer_id, f"❌ Не отправилось в TG: {m}")
                except Exception as e:
                    self.vk.send_message(peer_id, f"❌ Ошибка вложения: {e}")
                finally:
                    if path and os.path.exists(path):
                        try:
                            os.remove(path)
                        except Exception:
                            pass
            if skipped and not sent:
                self.vk.send_message(peer_id, "⚠️ Этот тип вложения не поддерживается (только фото и файлы).")

        threading.Thread(target=_work, daemon=True).start()

    def _tg_send(self, peer_id, user_id, text):
        """Отправить текст в активный чат Telegram (в фоне). Эхо не нужно —
        твоё сообщение уже видно в VK, а ответ собеседника покажет живая лента."""
        if not text:
            self.pending_input[user_id] = "tg_reply"
            state = self.tg_state.get(user_id, {})
            chat_id = state.get("chat_id", "")
            kb = make_keyboard([[
                {"label": "⬅ Отмена", "color": "secondary",
                 "payload": f"/tg open {chat_id}" if chat_id else "/tg back"},
            ]], one_time=False)
            self.vk.send_message(peer_id, "📝 Напишите ответ (или Отмена):", keyboard=kb)
            return

        state = self.tg_state.get(user_id, {})
        chat_id = state.get("chat_id")
        topic_id = state.get("topic_id")
        if not chat_id:
            self.vk.send_message(peer_id, "❌ Сначала выберите чат: /tg")
            return

        tg = self._get_tg(user_id)
        if tg is None:
            self.vk.send_message(peer_id, "❌ Telegram не настроен.")
            return

        def _send():
            try:
                if not self._tg_ensure_connected(tg, peer_id):
                    return
                ok, res = tg.send_message(chat_id, text, topic_id=topic_id)
                if not ok:
                    self.vk.send_message(peer_id, f"❌ {res}")
                else:
                    # res — id отправленного; помечаем, чтобы лента не дублировала
                    self._mark_vk_sent(user_id, res)
            except Exception as e:
                self.vk.send_message(peer_id, f"❌ Ошибка отправки: {e}")

        threading.Thread(target=_send, daemon=True).start()

    def _cmd_at(self, peer_id, user_id, args):
        """Запланировать задачу на конкретное время.

        /at HH:MM сессия команда
        /at HH:MM сессия | команда1 | команда2 | ... | 30s
        """
        if not args:
            self.vk.send_message(
                peer_id,
                "⏰ Планировщик: /at\n\n"
                "Запуск команды в указанное время.\n\n"
                "📌 Одна команда:\n"
                "/at 14:30 build npm run build\n\n"
                "📌 Пайплайн:\n"
                "/at 09:00 deploy | git pull | ./deploy.sh | 30s\n\n"
                "Если время уже прошло — задача будет на завтра."
            )
            return

        self._parse_and_schedule(peer_id, user_id, args, is_at=True)

    def _cmd_in(self, peer_id, user_id, args):
        """Запланировать задачу через N минут/часов.

        /in N[m|h] сессия команда
        /in N[m|h] сессия | команда1 | команда2 | ... | 30s
        """
        if not args:
            self.vk.send_message(
                peer_id,
                "⏰ Планировщик: /in\n\n"
                "Отложенный запуск команды.\n\n"
                "📌 Одна команда:\n"
                "/in 5m build npm run build\n\n"
                "📌 Пайплайн команд:\n"
                "/in 5m build | git pull | npm install | npm run build | 30s\n"
                "Команды разделяются |, задержка между ними в конце.\n\n"
                "📌 Claude Code:\n"
                "/in 2m claude | \\\"поставь петлю на 10 минут\\\" | 30s\n\n"
                "Задержка: s/сек/m/мин (по умолчанию 10с)"
            )
            return

        self._parse_and_schedule(peer_id, user_id, args, is_at=False)

    def _parse_and_schedule(self, peer_id, user_id, args, is_at):
        """Общий парсер: разбирает /in или /at с поддержкой пайплайнов."""
        # Проверяем: есть ли пайплайн (символ | после первого пробела)?
        # Формат: "5m session | cmd1 | cmd2 | 30s" или "5m session simple command"
        if "|" in args:
            # Пайплайн-формат
            parts = args.split("|", 1)
            head = parts[0].strip()  # "5m session"
            tail = parts[1].strip()  # "cmd1 | cmd2 | 30s"

            # Разбираем head: время + сессия
            head_parts = head.split(maxsplit=1)
            if len(head_parts) < 2:
                self.vk.send_message(peer_id, "❌ Формат: /in 5m сессия | команда1 | команда2 | 30s")
                return
            time_str, session_name = head_parts

            # Разбираем tail: команды + задержка
            commands, inter_delay = parse_pipeline_args(tail)
            if not commands:
                self.vk.send_message(peer_id, "❌ Нужна хотя бы одна команда после |")
                return
        else:
            # Обычный формат: "5m session command"
            parts = args.split(maxsplit=2)
            if len(parts) < 3:
                self.vk.send_message(
                    peer_id,
                    "❌ Формат: /in 5m сессия команда\n"
                    "Или пайплайн: /in 5m сессия | команда1 | команда2 | 30s"
                )
                return
            time_str, session_name, command = parts
            commands = [command]
            inter_delay = 10

        # Парсим время
        if is_at:
            ts, desc = _parse_at_time(time_str)
        else:
            ts, desc = _parse_in_time(time_str)

        if ts is None:
            self.vk.send_message(peer_id, f"❌ {desc}")
            return

        self._schedule_task(peer_id, user_id, ts, session_name, commands, inter_delay, desc)

    def _schedule_task(self, peer_id, user_id, trigger_ts, session_name, commands, inter_delay, desc):
        """Создать и зарегистрировать отложенную задачу (с поддержкой пайплайна)."""
        import uuid
        task_id = uuid.uuid4().hex[:8]

        task = ScheduledTask(
            task_id=task_id,
            trigger_time=trigger_ts,
            session_name=session_name.strip(),
            command=commands[0],  # первая команда для обратной совместимости
            user_id=user_id,
            peer_id=peer_id,
            task_type="create_and_run",
            commands=commands,
            inter_delay=inter_delay,
        )

        self.scheduler.add_task(task)
        when = _fmt_time(trigger_ts)

        if task.is_pipeline:
            cmds_text = "\n".join(f"  {i+1}. {c[:80]}" for i, c in enumerate(commands))
            self.vk.send_message(
                peer_id,
                f"⏰ Пайплайн запланирован!\n\n"
                f"🆔 ID: {task_id}\n"
                f"🆕 Сессия: {session_name}\n"
                f"📝 Команды ({len(commands)}):\n{cmds_text}\n"
                f"⏱ Задержка: {inter_delay}с\n"
                f"🕐 Старт: {when} ({desc})\n\n"
                f"Отменить: /cancel {task_id}"
            )
        else:
            self.vk.send_message(
                peer_id,
                f"⏰ Задача запланирована!\n\n"
                f"🆔 ID: {task_id}\n"
                f"🆕 Сессия: {session_name}\n"
                f"📝 Команда: {commands[0][:100]}\n"
                f"🕐 Когда: {when} ({desc})\n\n"
                f"Отменить: /cancel {task_id}\n"
                f"Все задачи: /tasks"
            )
        print(f"⏰ user={user_id} запланировал: {task.summary()}")

    def _cmd_tasks(self, peer_id, user_id, args):
        """Показать список отложенных задач."""
        tasks = self.scheduler.list_tasks(user_id)

        if not tasks:
            self.vk.send_message(
                peer_id,
                "📭 Нет запланированных задач.\n\n"
                "Создать:\n"
                "• /in 5m сессия команда — через N минут\n"
                "• /at 14:30 сессия команда — в указанное время"
            )
            return

        lines = [f"📋 Запланированные задачи: ({len(tasks)})", ""]
        for t in tasks:
            cancel_hint = f"/cancel {t.id}"
            lines.append(f"🆔 {t.id} {t.summary()}")
            lines.append(f"   Отмена: {cancel_hint}")
            lines.append("")

        self.vk.send_message(peer_id, "\n".join(lines))

    def _cmd_cancel(self, peer_id, user_id, args):
        """Отменить задачу по ID."""
        if not args:
            self._cmd_tasks(peer_id, user_id, "")
            return

        task_id = args.strip()
        task = self.scheduler.get_task(task_id)

        if not task:
            self.vk.send_message(
                peer_id,
                f"❌ Задача {task_id} не найдена.\nИспользуйте /tasks для списка."
            )
            return

        if task.user_id != user_id:
            self.vk.send_message(peer_id, "❌ Это не ваша задача.")
            return

        self.scheduler.remove_task(task_id)
        self.vk.send_message(
            peer_id,
            f"✅ Задача {task_id} отменена:\n{task.summary()}"
        )
        print(f"⏰ user={user_id} отменил задачу: {task_id}")

    def _execute_scheduled_task(self, task):
        """Callback: выполнить отложенную задачу (одиночную или пайплайн)."""
        if task.task_type != "create_and_run":
            return

        # Создаём сессию (если существует — используем существующую)
        if not session_exists(task.session_name):
            work_dir = self.config["tmux"].get("work_dir", None)
            if not self._create_tmux(task.session_name):
                self.vk.send_message(
                    task.peer_id,
                    f"⏰❌ Не удалось создать сессию {task.session_name} для задачи {task.id}"
                )
                return

        # Подключаем пользователя
        self._set_session(task.user_id, task.session_name)
        self._save_state()

        time.sleep(0.4)  # даём shell'у инициализироваться

        if task.is_pipeline:
            self._execute_pipeline(task)
        else:
            self._execute_single_command(task)

    def _execute_single_command(self, task):
        """Выполнить одиночную команду."""
        if send_keys(task.session_name, task.command, press_enter=True):
            time.sleep(0.5)
            output = get_output(task.session_name, self.config["tmux"]["output_lines"])
            formatted = format_output(task.session_name, output)
            self.vk.send_message(
                task.peer_id,
                f"⏰ Задача выполнена! {task.id}\n"
                f"Сессия: {task.session_name}\n"
                f"Команда: {task.command}\n\n"
                f"{formatted}"
            )
        else:
            self.vk.send_message(
                task.peer_id,
                f"⏰⚠️ Сессия {task.session_name} создана, но команда не отправилась.\n"
                f"Команда: {task.command}"
            )

    def _execute_pipeline(self, task):
        """Выполнить пайплайн команд последовательно с задержкой."""
        total = len(task.commands)
        self.vk.send_message(
            task.peer_id,
            f"⏰ Пайплайн запущен! {task.id}\n"
            f"Сессия: {task.session_name}\n"
            f"Команд: {total}, задержка: {task.inter_delay}с\n"
            f"Начинаю выполнение..."
        )

        for i, cmd in enumerate(task.commands):
            progress = f"[{i+1}/{total}]"
            if not send_keys(task.session_name, cmd, press_enter=True):
                self.vk.send_message(
                    task.peer_id,
                    f"⏰❌ {progress} Не удалось отправить: {cmd[:80]}"
                )
                return

            if i < total - 1:
                # Ждём задержку кроме последней команды
                self.vk.send_message(
                    task.peer_id,
                    f"⏰ {progress} Выполнено: {cmd[:60]}\n"
                    f"⏳ Ожидание {task.inter_delay}с до следующей..."
                )
                time.sleep(task.inter_delay)

        # Финальный вывод
        time.sleep(0.5)
        output = get_output(task.session_name, self.config["tmux"]["output_lines"])
        formatted = format_output(task.session_name, output)
        self.vk.send_message(
            task.peer_id,
            f"⏰✅ Пайплайн завершён! {task.id}\n"
            f"Выполнено {total} команд в {task.session_name}\n\n"
            f"{formatted}"
        )

    # ── Watch thread ─────────────────────────────────────────────

    def _start_watch_thread(self, user_id):
        """Запустить поток автообновления."""
        # Гарантируем, что старый поток помечен на остановку и снят с учёта
        self._watch_threads.pop(user_id, None)
        t = threading.Thread(target=self._watch_loop, args=(user_id,), daemon=True)
        self._watch_threads[user_id] = t
        t.start()

    def _watch_loop(self, user_id):
        """Цикл автообновления (выполняется в отдельном потоке).
        Плюс детект простоя: если вывод не меняется N минут — уведомление
        (например, Claude завершил задачу/петлю)."""
        my_ws = self._get_watch(user_id)   # мой словарь — идентичность отличает поток
        if not my_ws:
            return
        last_output = ""
        interval = self.config["tmux"].get("watch_interval", 2.0)
        idle_minutes = self.config["tmux"].get("idle_notify_minutes", 10)
        idle_secs = max(60, idle_minutes * 60)
        error_last = set()
        edit_count = 0
        MAX_EDITS = 30  # пересоздаём каждые 30 правок (лимит VK)
        last_change = time.time()
        idle_notified = False

        while True:
            ws = self._get_watch(user_id)
            # Новый watch-поток запущен → этот завершается
            if not ws or ws is not my_ws or ws.get("stop"):
                break

            session = ws["session"]
            peer_id = ws["peer_id"]
            message_id = ws["message_id"]

            if not session_exists(session):
                try:
                    self.vk.send_message(peer_id, f"❌ Сессия «{session}» больше не существует. Watch остановлен.")
                except Exception:
                    pass
                self._del_watch(user_id)
                self._save_state()
                break

            try:
                output = get_output(session, self.config["tmux"]["output_lines"])
            except Exception:
                time.sleep(interval)
                continue

            if output == last_output:
                # Детект простоя: тишина idle_secs → одно уведомление
                if not idle_notified and (time.time() - last_change) >= idle_secs:
                    idle_notified = True
                    tail = "\n".join(output.strip().split("\n")[-20:])
                    mins = int(idle_secs / 60)
                    try:
                        self.vk.send_message(
                            peer_id,
                            f"💤 Сессия «{session}» не меняется {mins} мин — вероятно, "
                            f"задача завершена.\n\n📄 Последние строки:\n{tail[-3500:]}",
                            keyboard=make_watch_keyboard())
                    except Exception:
                        pass
                    print(f"💤 user={user_id} детект простоя: {session}")
                time.sleep(interval)
                continue

            # Вывод изменился — сбрасываем таймер простоя
            last_change = time.time()
            idle_notified = False
            last_output = output
            formatted = format_output(session, output)
            edit_count += 1

            # Уведомления об ошибках
            if self.error_notify.get(user_id, False):
                errors = detect_errors(output)
                new_errors = [e for e in errors if e not in error_last]
                if new_errors:
                    error_last.update(new_errors)
                    err_lines = "\n".join(f"❌ {e[:150]}" for e in new_errors[:3])
                    try:
                        self.vk.send_message(peer_id, f"🚨 Ошибки в {session}:\n{err_lines}")
                    except Exception:
                        pass

            # Обновляем вывод: редактируем или пересоздаём
            if edit_count >= MAX_EDITS:
                try:
                    kb = make_watch_keyboard()
                    mid = self.vk.send_message(peer_id, formatted, keyboard=kb)
                    with self._lock:
                        if user_id in self.watching_sessions:
                            self.watching_sessions[user_id]["message_id"] = mid
                    try:
                        self.vk.delete_message(peer_id, message_id)
                    except Exception:
                        pass
                    edit_count = 0
                except Exception:
                    pass
            else:
                try:
                    self.vk.edit_message(peer_id, message_id, formatted)
                except Exception:
                    try:
                        kb = make_watch_keyboard()
                        mid = self.vk.send_message(peer_id, formatted, keyboard=kb)
                        with self._lock:
                            if user_id in self.watching_sessions:
                                self.watching_sessions[user_id]["message_id"] = mid
                        try:
                            self.vk.delete_message(peer_id, message_id)
                        except Exception:
                            pass
                        edit_count = 0
                    except Exception:
                        pass

            time.sleep(interval)

        # Снимаем регистрацию только если это всё ещё мой поток
        if self._watch_threads.get(user_id) is threading.current_thread():
            self._watch_threads.pop(user_id, None)

    # ── Возобновление watch ──────────────────────────────────────

    def _resume_watch(self):
        """Возобновить watch-сессии после перезапуска."""
        cleaned = False
        with self._lock:
            for user_id, info in list(self.watching_sessions.items()):
                session = info.get("session", "")
                if not session_exists(session):
                    print(f"⚠️ Сессия {session} не найдена, пропускаем возобновление watch")
                    del self.watching_sessions[user_id]
                    cleaned = True
                    continue
                print(f"👁 Возобновляем watch: {session} для user={user_id}")

        if cleaned:
            self._save_state()

        # Запускаем потоки (вне блокировки, чтобы избежать дедлока)
        for user_id in list(self.watching_sessions.keys()):
            self._start_watch_thread(user_id)
