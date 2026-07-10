"""Telegram-бот управления сервером и Claude Code.

Фичи (как у VK-бота, но нативнее): список сессий тапом, Claude/DeepClaude,
стриминг вывода моноширинно (<pre>), пульт клавиш (стрелки/Tab/Esc), планировщик
с пайплайнами, детект простоя, // для слэш-команд в сессию.
"""
import sys
import os
import time
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vkbot.config import load_config
from vkbot.tmux_handler import (
    list_sessions, session_exists, get_output, send_keys, send_control_key,
    create_session, kill_session, clean_pane, detect_session_state,
)
from vkbot.scheduler import (
    Scheduler, ScheduledTask, _parse_at_time, _parse_in_time, _fmt_time,
    parse_pipeline_args,
)
from .api import TgBotApi, pre_block, ikb


# tmux-имена клавиш для пульта
KEY_MAP = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "e": "Enter", "esc": "Escape", "tab": "Tab", "btab": "BTab",
    "space": "Space", "pgup": "PageUp", "pgdn": "PageDown",
}


import re as _re
# Волатильные элементы TUI (спиннер Claude, таймеры) — их изменения игнорируем,
# чтобы не редактировать сообщение каждую секунду и не ловить 429 от Telegram.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✻✽✢·⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒"
_VOLATILE_LINE = _re.compile(r"(esc to interrupt|\(\d+s[^)]*\)|tokens|↑|↓)", _re.I)


def _strip_volatile(text):
    """Убрать спиннер/таймеры для сравнения (анимация ≠ реальное изменение)."""
    out = []
    for ln in text.split("\n"):
        s = "".join(ch for ch in ln if ch not in _SPINNER)
        if _VOLATILE_LINE.search(s):
            s = _VOLATILE_LINE.sub("", s)
        out.append(s.rstrip())
    return "\n".join(out)


def fmt_stream(session_name, raw_output, max_len=3600):
    """Оформить вывод сессии моноширинно (ровный TUI)."""
    output = clean_pane(raw_output)
    if not output.strip():
        output = "(пусто — напишите текст или нажмите ⏎)"
    if len(output) > max_len:
        output = "…(обрезано)\n" + output[-max_len:]
    state = detect_session_state(output)
    hint = {"prompt": "💬 ждёт ответа", "build": "🔨 сборка…",
            "running": "⚡ выполняется…", "error": "🚨 ошибка", "idle": "🟢 готово"}.get(state, "")
    header = f"📺 <b>{session_name}</b>  {hint}"
    return header + "\n" + pre_block(output)


class TgTmuxBot:
    """Telegram-бот управления сервером."""

    def __init__(self):
        self.config = None
        self.api = None
        self._running = False
        self.scheduler = Scheduler(execute_callback=self._execute_task)
        self.sessions = {}          # chat_id -> имя активной сессии
        self.streams = {}           # chat_id -> {session, msg_id, stop, ...}
        self._stream_threads = {}
        self.pending = {}           # chat_id -> режим ввода
        self.settings = {}          # рантайм-настройки (override конфига)
        self._lock = threading.Lock()

    # ── Настройки (рантайм, поверх конфига) ───────────────────

    def _settings_file(self):
        from vkbot.config import CONFIG_DIR
        return os.path.join(CONFIG_DIR, "tgbot_settings.json")

    def _load_settings(self):
        import json
        p = self._settings_file()
        if os.path.exists(p):
            try:
                self.settings = json.load(open(p))
            except Exception:
                self.settings = {}

    def _save_settings(self):
        import json, tempfile
        from vkbot.config import CONFIG_DIR
        try:
            os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(self.settings, f)
            os.replace(tmp, self._settings_file())
        except Exception:
            pass

    def _s(self, key, default):
        """Значение настройки (рантайм override → конфиг → дефолт)."""
        if key in self.settings:
            return self.settings[key]
        return self.config["tmux"].get(key, default)

    # ── Персистентность (активные сессии переживают ребут) ────

    def _state_file(self):
        from vkbot.config import CONFIG_DIR
        return os.path.join(CONFIG_DIR, "tgbot_state.json")

    def _save_sessions(self):
        import json, tempfile
        from vkbot.config import CONFIG_DIR
        try:
            os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump({str(k): v for k, v in self.sessions.items()}, f)
            os.replace(tmp, self._state_file())
        except Exception:
            pass

    def _load_sessions(self):
        import json
        p = self._state_file()
        if not os.path.exists(p):
            return
        try:
            with open(p) as f:
                data = json.load(f)
            self.sessions = {int(k): v for k, v in data.items()}
        except Exception:
            self.sessions = {}

    def _resume_streams(self):
        """После ребута — вернуть стримы для существующих сессий."""
        for chat_id, session in list(self.sessions.items()):
            if session_exists(session):
                try:
                    self.api.send(chat_id, f"🔄 После перезапуска — сессия «{session}» на связи:")
                    self._start_stream(chat_id, session)
                except Exception:
                    pass
            else:
                self.sessions.pop(chat_id, None)
        self._save_sessions()

    # ── Доступ ────────────────────────────────────────────────

    def _allowed(self, uid):
        cfg = self.config["tgbot"]
        ids = set(cfg.get("allowed_user_ids", [])) | set(cfg.get("admin_ids", []))
        ids |= set(self.settings.get("extra_allowed", []))
        return uid in ids

    def _is_admin(self, uid):
        return uid in set(self.config["tgbot"].get("admin_ids", []))

    # ── Запуск ────────────────────────────────────────────────

    def start(self):
        print("🚀 Telegram Tmux Bot запускается…\n")
        self.config = load_config()
        if not self.config:
            print("❌ Конфиг не загружен.")
            return False
        tg = self.config.get("tgbot", {})
        if not tg.get("enabled"):
            print("❌ tgbot.enabled = false в конфиге. Включите, чтобы запустить.")
            return False
        token = tg.get("bot_token", "")
        if not token:
            print("❌ Не указан tgbot.bot_token.")
            return False

        # Прокси только для Telegram-бота (Telegram может быть заблокирован в РФ).
        # Значение "env" — взять из https_proxy/HTTPS_PROXY окружения.
        proxy = (tg.get("proxy") or "").strip()
        if proxy.lower() == "env":
            import os
            proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
        if proxy:
            print("🌐 Telegram Bot API через прокси (адрес скрыт)")

        self.api = TgBotApi(token, proxy=proxy or None)
        ok, info = self.api.validate()
        print(f"🔍 Бот: {info}")
        if not ok:
            print("❌ Невалидный токен.")
            return False

        self.api.set_commands([
            {"command": "menu", "description": "Главное меню"},
            {"command": "ls", "description": "Список сессий"},
            {"command": "claude", "description": "Claude Code"},
            {"command": "dcc", "description": "DeepClaude"},
            {"command": "new", "description": "Новая сессия"},
            {"command": "tasks", "description": "Отложенные задачи"},
            {"command": "help", "description": "Помощь"},
        ])

        allowed = set(tg.get("allowed_user_ids", [])) | set(tg.get("admin_ids", []))
        print(f"✅ Разрешённые: {sorted(allowed)}")
        self._load_settings()
        self._load_sessions()
        self.scheduler.start()
        self._resume_streams()
        self._running = True
        print("🎯 Слушаю Telegram…\n")
        self._long_poll()
        return True

    def stop(self):
        print("\n🛑 Остановка…")
        self._running = False
        for cid in list(self.streams):
            s = self.streams.get(cid)
            if s:
                s["stop"] = True
        self.scheduler.stop()

    # ── Long poll ─────────────────────────────────────────────

    def _long_poll(self):
        offset = None
        while self._running:
            try:
                updates = self.api.get_updates(offset=offset, timeout=25)
                for upd in updates:
                    offset = upd["update_id"] + 1
                    # Обработка в отдельном потоке — поллинг НЕ блокируется
                    # (иначе долгие операции подвешивают отклик на кнопки).
                    threading.Thread(
                        target=self._safe_handle, args=(upd,), daemon=True).start()
            except Exception as e:
                print(f"❌ Poll: {e}")
                time.sleep(3)

    def _safe_handle(self, upd):
        try:
            self._handle_update(upd)
        except Exception as e:
            print(f"⚠️ Ошибка обработки: {e}")
            import traceback
            traceback.print_exc()

    def _handle_update(self, upd):
        if "message" in upd:
            self._handle_message(upd["message"])
        elif "callback_query" in upd:
            self._handle_callback(upd["callback_query"])

    # ── Сообщения ─────────────────────────────────────────────

    def _handle_message(self, msg):
        chat_id = msg["chat"]["id"]
        uid = msg.get("from", {}).get("id", 0)
        text = (msg.get("text") or "").strip()

        if not self._allowed(uid):
            self.api.send(chat_id, f"⛔ Нет доступа.\nВаш Telegram ID: {uid}")
            return
        if not text:
            return

        # Слэш-команда в сессию: //model → шлём /model в tmux
        if text.startswith("//") and self.sessions.get(chat_id):
            self._send_to_session(chat_id, text[1:])
            self.api.delete(chat_id, msg["message_id"])
            return

        # Режим ожидания ввода (имя сессии, текст, ID юзера) — слэш прерывает
        if chat_id in self.pending and not text.startswith("/"):
            mode = self.pending.pop(chat_id)
            # своё сообщение удаляем — чат не растёт
            self.api.delete(chat_id, msg["message_id"])
            self._handle_pending(chat_id, uid, mode, text)
            return
        self.pending.pop(chat_id, None)  # слэш-команда отменяет ожидание

        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0][1:].split("@")[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            self._command(chat_id, uid, cmd, args)
        elif self.sessions.get(chat_id):
            # обычный текст → в активную сессию
            self._send_to_session(chat_id, text)
            self.api.delete(chat_id, msg["message_id"])
        else:
            self._cmd_menu(chat_id, uid=uid)

    def _handle_pending(self, chat_id, uid, mode, text):
        if mode == "new":
            name = text.strip().split()[0] if text.strip() else ""
            self._create_and_open(chat_id, name)
        elif mode == "send":
            self._send_to_session(chat_id, text)
        elif mode == "adduser":
            self._admin_adduser(chat_id, uid, text)

    # ── Callback (inline-кнопки) ──────────────────────────────

    def _screen(self, chat_id, text, keyboard=None, html=False, edit=None):
        """Показать экран: РЕДАКТИРОВАТЬ сообщение (edit=mid) или отправить новое.
        Навигация правит одно сообщение — чат не растёт, ощущается нативно."""
        if edit:
            if self.api.edit(chat_id, edit, text, keyboard=keyboard, html_mode=html):
                return edit
        return self.api.send(chat_id, text, keyboard=keyboard, html_mode=html)

    def _handle_callback(self, cb):
        chat_id = cb["message"]["chat"]["id"]
        mid = cb["message"]["message_id"]
        uid = cb.get("from", {}).get("id", 0)
        data = cb.get("data", "")
        self.api.answer_callback(cb["id"])
        if not self._allowed(uid):
            return

        # Навигационные экраны — редактируем текущее сообщение (edit=mid)
        if data == "menu":
            self._cmd_menu(chat_id, edit=mid)
        elif data == "ls":
            self._cmd_ls(chat_id, edit=mid)
        elif data == "tasks":
            self._cmd_tasks(chat_id, edit=mid)
        elif data == "manage":
            self._cmd_manage(chat_id, edit=mid)
        elif data == "settings":
            self._cmd_settings(chat_id, edit=mid)
        elif data == "admin":
            self._cmd_admin(chat_id, uid, edit=mid)
        elif data == "projects":
            self._cmd_projects(chat_id, edit=mid)
        elif data.startswith("proj:"):
            try:
                self._cmd_project(chat_id, int(data[5:]), edit=mid)
            except ValueError:
                self._cmd_projects(chat_id, edit=mid)
        elif data.startswith("pj:"):
            # pj:<idx>:<kind>
            try:
                _, sidx, kind = data.split(":", 2)
                self._launch_project(chat_id, int(sidx), kind, reuse_mid=mid)
            except ValueError:
                self._cmd_projects(chat_id, edit=mid)
        # Открытие сессии/Claude — превращаем это же сообщение в живой стрим
        elif data == "claude":
            self._launch_ai(chat_id, "claude", reuse_mid=mid)
        elif data == "dcc":
            self._launch_ai(chat_id, "dcc", reuse_mid=mid)
        elif data.startswith("open:"):
            self._attach_and_stream(chat_id, data[5:], reuse_mid=mid)
        elif data == "o":
            self._refresh_stream(chat_id)
        elif data == "new":
            self.pending[chat_id] = "new"
            self._screen(chat_id, "➕ Имя новой сессии:", keyboard=self._cancel_kb(chat_id), edit=mid)
        elif data == "stop":
            self._stop_stream(chat_id)
            self._screen(chat_id, "🛑 Стрим остановлен.", keyboard=self._menu_kb(), edit=mid)
        elif data == "detach":
            self._detach(chat_id, edit=mid)
        elif data.startswith("kill:"):
            self._kill(chat_id, data[5:], edit=mid)
        elif data.startswith("k:"):
            self._send_key(chat_id, data[2:])
        elif data == "quick":
            # Во время стрима — просто меняем набор кнопок на том же сообщении
            if chat_id in self.streams:
                self.streams[chat_id]["kb_mode"] = "quick"
                self._rerender_stream(chat_id)
            else:
                self._cmd_quick(chat_id, edit=mid)
        elif data == "padmode":
            if chat_id in self.streams:
                self.streams[chat_id]["kb_mode"] = "pad"
                self._rerender_stream(chat_id)
        elif data == "sessmenu":
            if chat_id in self.streams:
                self.streams[chat_id]["kb_mode"] = "sessmenu"
                self._rerender_stream(chat_id)
        elif data == "killcur":
            # Первый шаг подтверждения (кнопки на том же сообщении)
            if chat_id in self.streams:
                self.streams[chat_id]["kb_mode"] = "killconfirm"
                self._rerender_stream(chat_id)
            else:
                self._kill_current(chat_id, edit=mid)
        elif data == "killyes":
            self._kill_current(chat_id, edit=mid)
        elif data.startswith("q:"):
            self._run_quick(chat_id, data[2:])
        elif data.startswith("cancel:"):
            self._cmd_cancel(chat_id, data[7:])
        elif data.startswith("deluser:"):
            self._admin_deluser(chat_id, uid, data[8:], edit=mid)
        elif data == "adduser":
            if self._is_admin(uid):
                self.pending[chat_id] = "adduser"
                self._screen(chat_id, "➕ Telegram ID нового пользователя:",
                             keyboard=ikb([[("⬅ Отмена", "admin")]]), edit=mid)
        elif data == "help":
            self._cmd_help(chat_id, edit=mid)
        elif data.startswith("set:"):
            self._apply_setting(chat_id, data[4:], edit=mid)
        elif data == "input":
            self.pending[chat_id] = "send"
            # Пауза стрима, чтобы приглашение ввода не затёрлось перерисовкой
            if chat_id in self.streams:
                self.streams[chat_id]["paused"] = True
            self._screen(chat_id, "📝 Напишите текст — уйдёт в сессию:",
                         keyboard=self._cancel_kb(chat_id), edit=mid)

    # ── Команды ───────────────────────────────────────────────

    def _command(self, chat_id, uid, cmd, args):
        if cmd in ("start", "help", "помощь"):
            self._cmd_help(chat_id)
        elif cmd in ("menu", "меню"):
            self._cmd_menu(chat_id, uid=uid)
        elif cmd in ("settings", "настройки"):
            self._cmd_settings(chat_id)
        elif cmd in ("admin", "админ", "админка"):
            self._cmd_admin(chat_id, uid)
        elif cmd in ("ls", "sessions", "сессии"):
            self._cmd_ls(chat_id)
        elif cmd in ("manage", "управление"):
            self._cmd_manage(chat_id)
        elif cmd in ("quick", "быстрые"):
            self._cmd_quick(chat_id)
        elif cmd in ("new", "новая"):
            if args.strip():
                self._create_and_open(chat_id, args.strip().split()[0], args)
            else:
                self.pending[chat_id] = "new"
                self.api.send(chat_id, "➕ Имя новой сессии (можно: имя команда):", keyboard=self._cancel_kb(chat_id))
        elif cmd in ("claude", "клод"):
            self._launch_ai(chat_id, "claude")
        elif cmd in ("dcc", "дкк"):
            self._launch_ai(chat_id, "dcc")
        elif cmd in ("attach", "подключить"):
            if args.strip():
                self._attach_and_stream(chat_id, args.strip())
            else:
                self._cmd_ls(chat_id)
        elif cmd in ("kill", "удалить"):
            self._kill(chat_id, args.strip())
        elif cmd in ("detach", "откл"):
            self._detach(chat_id)
        elif cmd in ("o", "output", "вывод"):
            self._refresh_stream(chat_id)
        elif cmd in ("watch", "смотреть"):
            s = self.sessions.get(chat_id)
            if s:
                self._start_stream(chat_id, s)
        elif cmd in ("unwatch", "стоп"):
            self._stop_stream(chat_id)
        elif cmd in ("e", "enter"):
            self._send_key(chat_id, "e")
        elif cmd == "c":
            self._send_ctrl(chat_id, "C-c")
        elif cmd == "d":
            self._send_ctrl(chat_id, "C-d")
        elif cmd in KEY_MAP:
            self._send_key(chat_id, cmd)
        elif cmd in ("s", "send", "отправить"):
            if args:
                self._send_to_session(chat_id, args)
            else:
                self.pending[chat_id] = "send"
                self.api.send(chat_id, "📝 Текст:", keyboard=self._cancel_kb(chat_id))
        elif cmd in ("in", "через"):
            self._cmd_schedule(chat_id, args, is_at=False)
        elif cmd in ("at", "в"):
            self._cmd_schedule(chat_id, args, is_at=True)
        elif cmd in ("tasks", "задачи"):
            self._cmd_tasks(chat_id)
        elif cmd in ("cancel", "отмена"):
            self._cmd_cancel(chat_id, args.strip())
        else:
            self.api.send(chat_id, f"❓ Неизвестная команда: /{cmd}", keyboard=self._menu_kb())

    def _cmd_help(self, chat_id, edit=None):
        msg = (
            "🤖 <b>Tmux / Claude Code — из Telegram</b>\n\n"
            "🖥 <b>Сессии</b>\n"
            "/ls — список (тап = подключиться + стрим)\n"
            "/new имя [команда] — создать\n"
            "/claude — Claude Code · /dcc — DeepClaude\n"
            "/attach · /kill · /detach\n\n"
            "🎮 <b>Пульт</b> (кнопки под выводом):\n"
            "стрелки, Enter, Esc, Tab, Shift+Tab, Ctrl+C\n"
            "— навигация по меню Claude (/model, /resume)\n\n"
            "⌨️ <b>Ввод</b> (когда подключён):\n"
            "• просто текст → в сессию\n"
            "• //model, //resume — слэш-команды В Claude\n\n"
            "⏰ <b>Планировщик</b>\n"
            "/in 5m сессия команда — через N минут\n"
            "/at 14:30 сессия команда — ко времени\n"
            "пайплайн: /in 5m s | cmd1 | cmd2 | 30s\n"
            "/tasks · /cancel id\n\n"
            "💤 Тишина сессии N мин → уведомление «готово»"
        )
        self._screen(chat_id, msg, keyboard=ikb([[("🏠 Меню", "menu"), ("🖥 Сессии", "ls")]]),
                     html=True, edit=edit)

    def _menu_kb(self):
        return ikb([
            [("🖥 Сессии", "ls"), ("➕ Новая", "new")],
            [("🤖 Claude", "claude"), ("🧠 DeepClaude", "dcc")],
            [("⏰ Задачи", "tasks"), ("⚙️ Настройки", "settings")],
            [("🏠 Меню", "menu")],
        ])

    def _nav_kb(self, chat_id):
        """Навигация для любого экрана: назад в сессию (если есть) + меню."""
        rows = []
        if self.sessions.get(chat_id):
            rows.append([("🔄 К сессии", "o"), ("🖥 Сессии", "ls")])
        else:
            rows.append([("🖥 Сессии", "ls")])
        rows.append([("🏠 Меню", "menu")])
        return ikb(rows)

    def _cancel_kb(self, chat_id):
        """Приглашение ввода — с кнопкой отмены (назад в сессию/меню)."""
        target = "o" if self.sessions.get(chat_id) else "menu"
        return ikb([[("⬅ Отмена", target)]])

    # ── Настройки ─────────────────────────────────────────────

    def _cmd_settings(self, chat_id, edit=None):
        idle = self._s("idle_notify_minutes", 10)
        iv = self._s("watch_interval", 2.0)
        w = self._s("term_width", 62)

        def m(cur, key, val, lbl):
            active = (float(cur) == float(val))
            return (("✅ " if active else "") + lbl, f"set:{key}:{val}")

        rows = [
            [("💤 Простой → уведомление:", "settings")],
            [m(idle, "idle", 5, "5м"), m(idle, "idle", 10, "10м"),
             m(idle, "idle", 15, "15м"), m(idle, "idle", 30, "30м")],
            [("🔄 Частота обновления:", "settings")],
            [m(iv, "iv", 2, "2с"), m(iv, "iv", 3, "3с"), m(iv, "iv", 5, "5с")],
            [("📏 Ширина терминала:", "settings")],
            [m(w, "w", 50, "50"), m(w, "w", 62, "62"), m(w, "w", 80, "80")],
            [("🖥 Сессии", "ls"), ("🏠 Меню", "menu")],
        ]
        self._screen(chat_id,
                     "⚙️ <b>Настройки</b>\n"
                     "Меняются на лету и сохраняются. Ширина — для новых сессий.",
                     keyboard=ikb(rows), html=True, edit=edit)

    def _apply_setting(self, chat_id, spec, edit=None):
        try:
            key, val = spec.split(":")
        except ValueError:
            return
        if key == "idle":
            self.settings["idle_notify_minutes"] = int(val)
        elif key == "iv":
            self.settings["watch_interval"] = float(val)
        elif key == "w":
            self.settings["term_width"] = int(val)
        self._save_settings()
        self._cmd_settings(chat_id, edit=edit)

    # ── Админка ───────────────────────────────────────────────

    def _cmd_admin(self, chat_id, uid, edit=None):
        if not self._is_admin(uid):
            self._screen(chat_id, "🔒 Только для администратора.", keyboard=self._menu_kb(), edit=edit)
            return
        cfg = self.config["tgbot"]
        admins = cfg.get("admin_ids", [])
        allowed = set(cfg.get("allowed_user_ids", [])) | set(self.settings.get("extra_allowed", []))
        lines = ["👑 <b>Админка</b> — доступ к боту", "", "Администраторы:"]
        lines += [f"  • <code>{a}</code>" for a in admins]
        others = [u for u in allowed if u not in admins]
        lines += ["", "Пользователи:"] + ([f"  • <code>{u}</code>" for u in others] or ["  (нет)"])
        rows = [[("➕ Добавить юзера", "adduser")]]
        for u in others:
            rows.append([(f"🗑 Убрать {u}", f"deluser:{u}")])
        rows.append([("🏠 Меню", "menu")])
        self._screen(chat_id, "\n".join(lines), keyboard=ikb(rows), html=True, edit=edit)

    def _admin_deluser(self, chat_id, uid, target, edit=None):
        if not self._is_admin(uid):
            return
        try:
            tid = int(target)
        except ValueError:
            return
        extra = set(self.settings.get("extra_allowed", []))
        extra.discard(tid)
        self.settings["extra_allowed"] = list(extra)
        # если был в конфиге — тоже убираем на лету (в рантайме)
        cfg = self.config["tgbot"]
        if tid in cfg.get("allowed_user_ids", []):
            cfg["allowed_user_ids"] = [x for x in cfg["allowed_user_ids"] if x != tid]
        self._save_settings()
        self._cmd_admin(chat_id, uid, edit=edit)

    def _admin_adduser(self, chat_id, uid, text):
        if not self._is_admin(uid):
            return
        try:
            tid = int(text.strip().split()[0])
        except (ValueError, IndexError):
            self.api.send(chat_id, "❌ Нужен числовой Telegram ID.",
                          keyboard=ikb([[("⬅ Назад", "admin")]]))
            return
        extra = set(self.settings.get("extra_allowed", []))
        extra.add(tid)
        self.settings["extra_allowed"] = list(extra)
        self._save_settings()
        self.api.send(chat_id, f"✅ Пользователь {tid} добавлен.")
        self._cmd_admin(chat_id, uid)

    def _cmd_menu(self, chat_id, uid=None, edit=None):
        is_adm = self._is_admin(uid) if uid else False
        rows = [
            [("🖥 Сессии", "ls"), ("➕ Новая", "new")],
            [("🤖 Claude", "claude"), ("🧠 DeepClaude", "dcc")],
        ]
        if self._projects():
            rows.append([("📂 Мои проекты", "projects")])
        rows.append([("⏰ Задачи", "tasks"), ("⚙️ Настройки", "settings")])
        if is_adm:
            rows.append([("👑 Админка", "admin"), ("ℹ️ Помощь", "help")])
        else:
            rows.append([("ℹ️ Помощь", "help")])
        cur = self.sessions.get(chat_id)
        title = f"⚡ <b>Главное меню</b>" + (f"\nАктивная сессия: <b>{cur}</b>" if cur else "")
        self._screen(chat_id, title, keyboard=ikb(rows), html=True, edit=edit)

    _STATUS_ICON = {"prompt": "💬", "build": "🔨", "running": "⚡", "error": "🚨", "idle": "🟢"}

    def _session_status(self, name):
        try:
            out = get_output(name, 25)
            return self._STATUS_ICON.get(detect_session_state(out), "🟢")
        except Exception:
            return "•"

    def _cmd_ls(self, chat_id, edit=None):
        sessions = list_sessions()
        cur = self.sessions.get(chat_id)
        if not sessions:
            self._screen(chat_id, "📭 Активных сессий нет.\nСоздайте новую или запустите Claude:",
                         keyboard=ikb([[("➕ Новая", "new"), ("🤖 Claude", "claude")],
                                       [("🏠 Меню", "menu")]]), edit=edit)
            return
        rows = []
        for s in sessions:
            mark = "▶ " if s == cur else ""
            rows.append([(f"{mark}{self._session_status(s)} {s}", f"open:{s}")])
        rows.append([("➕ Новая", "new"), ("🗑 Управление", "manage")])
        rows.append([("🏠 Меню", "menu")])
        self._screen(chat_id,
                     f"🖥 <b>Сессии</b> ({len(sessions)}) — тап, чтобы подключиться:\n"
                     f"🟢 готова · ⚡ работает · 💬 ждёт · 🚨 ошибка",
                     keyboard=ikb(rows), html=True, edit=edit)

    def _cmd_manage(self, chat_id, edit=None):
        sessions = list_sessions()
        if not sessions:
            self._cmd_ls(chat_id, edit=edit)
            return
        rows = [[(f"🗑 {s}", f"kill:{s}")] for s in sessions]
        rows.append([("⬅ К сессиям", "ls"), ("🏠 Меню", "menu")])
        self._screen(chat_id, "🗑 Тап — удалить сессию:", keyboard=ikb(rows), edit=edit)

    def _cmd_quick(self, chat_id, edit=None):
        session = self.sessions.get(chat_id)
        if not session:
            self._screen(chat_id, "⚠️ Сначала подключитесь к сессии.",
                         keyboard=self._nav_kb(chat_id), edit=edit)
            return
        cmds = self.config["tmux"].get("quick_commands", [])
        rows, row = [], []
        for i, c in enumerate(cmds):
            row.append((c[:24], f"q:{i}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([("⬅ Назад", "o"), ("🏠 Меню", "menu")])
        self._screen(chat_id, f"⚡ Быстрые команды → «{session}»:", keyboard=ikb(rows), edit=edit)

    # ── Сессии / стрим ────────────────────────────────────────

    def _create_tmux(self, name, work_dir=None):
        t = self.config["tmux"]
        return create_session(name, work_dir=work_dir or t.get("work_dir"),
                              width=self._s("term_width", 62),
                              height=self._s("term_height", 40))

    # ── Мои проекты (конфиг-driven) ───────────────────────────
    def _projects(self):
        """Список проектов из конфига: [{name, path, session}, ...] (валидные)."""
        out = []
        for p in (self.config.get("tmux", {}).get("projects") or []):
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip()
            path = str(p.get("path") or "").strip()
            if not name or not path:
                continue
            sess = str(p.get("session") or name).strip()
            # имя сессии tmux — только безопасные символы
            sess = _re.sub(r"[^a-zA-Z0-9_-]", "-", sess)
            out.append({"name": name, "path": path, "session": sess})
        return out

    def _cmd_projects(self, chat_id, edit=None):
        projs = self._projects()
        if not projs:
            self._screen(chat_id,
                         "📂 <b>Мои проекты</b>\n\nНе настроены. Добавьте в конфиг "
                         "секцию <code>tmux.projects</code> (name + path).",
                         keyboard=self._nav_kb(chat_id), html=True, edit=edit)
            return
        rows, row = [], []
        for i, p in enumerate(projs):
            row.append((f"📁 {p['name']}", f"proj:{i}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([("🏠 Меню", "menu")])
        self._screen(chat_id, "📂 <b>Мои проекты</b>\nВыберите проект:",
                     keyboard=ikb(rows), html=True, edit=edit)

    def _cmd_project(self, chat_id, idx, edit=None):
        projs = self._projects()
        if idx < 0 or idx >= len(projs):
            self._cmd_projects(chat_id, edit=edit)
            return
        p = projs[idx]
        running = " · ▶ запущена" if session_exists(p["session"]) else ""
        rows = [
            [(f"🤖 Claude", f"pj:{idx}:claude"), (f"🧠 DeepClaude", f"pj:{idx}:dcc")],
            [("🖥 Терминал", f"pj:{idx}:sh")],
            [("⬅ Проекты", "projects"), ("🏠 Меню", "menu")],
        ]
        self._screen(chat_id,
                     f"📁 <b>{p['name']}</b>{running}\n"
                     f"<code>{p['path']}</code>\n\nЧто запустить в этой папке?",
                     keyboard=ikb(rows), html=True, edit=edit)

    def _launch_project(self, chat_id, idx, kind, reuse_mid=None):
        projs = self._projects()
        if idx < 0 or idx >= len(projs):
            self._cmd_projects(chat_id, edit=reuse_mid)
            return
        p = projs[idx]
        if kind == "sh":
            # просто терминал в папке проекта
            if session_exists(p["session"]):
                self._attach_and_stream(chat_id, p["session"], reuse_mid=reuse_mid)
            elif self._create_tmux(p["session"], work_dir=p["path"]):
                self._attach_and_stream(chat_id, p["session"], reuse_mid=reuse_mid)
            else:
                self._screen(chat_id, "❌ Не удалось создать сессию.",
                             keyboard=self._menu_kb(), edit=reuse_mid)
            return
        self._launch_ai(chat_id, kind, reuse_mid=reuse_mid,
                        work_dir=p["path"], session_name=p["session"])

    def _create_and_open(self, chat_id, name, full_args=None):
        import re
        if not re.match(r"^[a-zA-Z0-9_-]+$", name or ""):
            self.api.send(chat_id, "❌ Имя: латиница, цифры, - и _.", keyboard=self._nav_kb(chat_id))
            return
        if session_exists(name):
            self._attach_and_stream(chat_id, name)
            return
        if not self._create_tmux(name):
            self.api.send(chat_id, "❌ Не удалось создать сессию.", keyboard=self._menu_kb())
            return
        # опциональная команда: /new имя команда
        if full_args:
            parts = full_args.split(maxsplit=1)
            if len(parts) > 1:
                time.sleep(0.4)
                send_keys(name, parts[1], press_enter=True)
        self._attach_and_stream(chat_id, name)

    def _create_and_open_named(self, chat_id, name):
        self._create_and_open(chat_id, name)

    def _launch_ai(self, chat_id, kind, reuse_mid=None, work_dir=None, session_name=None):
        session = session_name or kind  # 'claude'/'dcc' или имя проекта
        cfg = self.config.get("claude", {})
        command = cfg.get("command", "claude") if kind == "claude" else cfg.get("deepclaude_command", "dcc")
        label = "🤖 Claude Code" if kind == "claude" else "🧠 DeepClaude"
        self.api.typing(chat_id)
        if not session_exists(session):
            if not self._create_tmux(session, work_dir=work_dir):
                self._screen(chat_id, "❌ Не удалось создать сессию.",
                             keyboard=self._menu_kb(), edit=reuse_mid)
                return
            time.sleep(0.4)
            send_keys(session, command, press_enter=True)
            time.sleep(1.2)
            print(f"{label}: запущен ({session})")
        self._attach_and_stream(chat_id, session, reuse_mid=reuse_mid)

    def _attach_and_stream(self, chat_id, name, reuse_mid=None):
        if not session_exists(name):
            self._screen(chat_id, f"❌ Сессии «{name}» нет.", keyboard=self._menu_kb(), edit=reuse_mid)
            return
        self.sessions[chat_id] = name
        self._save_sessions()
        self._start_stream(chat_id, name, reuse_mid=reuse_mid)

    def _detach(self, chat_id, edit=None):
        self._stop_stream(chat_id)
        self.sessions.pop(chat_id, None)
        self._save_sessions()
        self._cmd_menu(chat_id, edit=edit)

    def _kill(self, chat_id, name, edit=None):
        if not name:
            return
        if self.sessions.get(chat_id) == name:
            self._stop_stream(chat_id)
            self.sessions.pop(chat_id, None)
            self._save_sessions()
        kill_session(name)
        # Всегда показываем ОБНОВЛЁННЫЙ экран управления (не застрявшие кнопки)
        self._cmd_manage(chat_id, edit=edit)

    def _kill_current(self, chat_id, edit=None):
        """Завершить (убить) активную сессию текущего чата и вернуться в меню."""
        name = self.sessions.get(chat_id)
        st = self.streams.get(chat_id)
        mid = edit or (st.get("msg_id") if st else None)
        self._stop_stream(chat_id)
        self.sessions.pop(chat_id, None)
        self._save_sessions()
        if name and session_exists(name):
            kill_session(name)
        txt = f"❌ Сессия «{name}» завершена." if name else "Сессия не выбрана."
        self._screen(chat_id, txt, keyboard=self._menu_kb(), edit=mid)

    def _pad_kb(self):
        return ikb([
            [("⬆️", "k:up"), ("⏎", "k:e"), ("⎋ Esc", "k:esc")],
            [("⬅️", "k:left"), ("⬇️", "k:down"), ("➡️", "k:right")],
            [("⇥ Tab", "k:tab"), ("⇧⇥", "k:btab"), ("⛔ Ctrl+C", "k:c")],
            [("📝 Текст", "input"), ("⚡ Быстрые", "quick"), ("🔄", "o")],
            # Свернуть = уйти, сессия ЖИВЁТ. Завершение спрятано в «⚙️ Ещё».
            [("🖥 Сессии", "ls"), ("🔽 Свернуть", "detach"), ("⚙️ Ещё", "sessmenu")],
        ])

    def _sessmenu_kb(self):
        """Подменю действий над сессией — здесь живёт опасное «Завершить»
        (спрятано от случайного клика в пульте)."""
        return ikb([
            [("❌ Завершить сессию", "killcur")],
            [("⬅ Клавиши", "padmode")],
        ])

    def _killconfirm_kb(self):
        """Явное подтверждение завершения — второй шаг, на том же сообщении."""
        return ikb([
            [("❌ Да, завершить сессию", "killyes")],
            [("⬅ Отмена, вернуться", "sessmenu")],
        ])

    def _quick_inline_kb(self):
        """Клавиатура быстрых команд ВНУТРИ стрима (текст терминала не меняем —
        меняется только набор кнопок на том же сообщении)."""
        cmds = self.config["tmux"].get("quick_commands", [])
        rows, row = [], []
        for i, c in enumerate(cmds):
            row.append((c[:22], f"q:{i}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([("⬅ Клавиши", "padmode")])
        return ikb(rows)

    def _stream_kb(self, chat_id):
        """Клавиатура для сообщения-стрима по текущему режиму."""
        st = self.streams.get(chat_id)
        mode = st.get("kb_mode") if st else "pad"
        if mode == "quick":
            return self._quick_inline_kb()
        if mode == "sessmenu":
            return self._sessmenu_kb()
        if mode == "killconfirm":
            return self._killconfirm_kb()
        return self._pad_kb()

    def _rerender_stream(self, chat_id):
        """Немедленно перерисовать сообщение-стрим (текущий вывод + клавиатура
        по режиму) — чтобы смена набора кнопок была мгновенной, без нового окна."""
        st = self.streams.get(chat_id)
        if not st:
            return
        session = st["session"]
        try:
            text = fmt_stream(session, get_output(session, self.config["tmux"]["output_lines"]))
            st["stable"] = _strip_volatile(text)
            self.api.edit(chat_id, st["msg_id"], text,
                          keyboard=self._stream_kb(chat_id), html_mode=True)
        except Exception:
            pass

    def _run_quick(self, chat_id, idx):
        """Выполнить быструю команду по индексу и вернуть пульт (без новых окон)."""
        try:
            cmds = self.config["tmux"].get("quick_commands", [])
            cmd = cmds[int(idx)]
        except (ValueError, IndexError):
            return
        st = self.streams.get(chat_id)
        if st:
            st["kb_mode"] = "pad"   # возвращаем пульт
        session = self.sessions.get(chat_id)
        if session and session_exists(session):
            send_keys(session, cmd, press_enter=True)
            if chat_id not in self.streams:
                self._start_stream(chat_id, session)
            else:
                self._rerender_stream(chat_id)
        else:
            self._send_to_session(chat_id, cmd)

    def _start_stream(self, chat_id, session, reuse_mid=None):
        self._stop_stream(chat_id)
        text = fmt_stream(session, get_output(session, self.config["tmux"]["output_lines"]))
        # reuse_mid — превращаем сообщение-меню в живой стрим (чат не растёт)
        mid = self._screen(chat_id, text, keyboard=self._pad_kb(), html=True, edit=reuse_mid)
        st = {"session": session, "msg_id": mid, "stop": False,
              "last": text, "stable": _strip_volatile(text),
              "last_change": time.time(), "idle_notified": False,
              "kb_mode": "pad", "paused": False}
        self.streams[chat_id] = st
        t = threading.Thread(target=self._stream_loop, args=(chat_id,), daemon=True)
        self._stream_threads[chat_id] = t
        t.start()
        print(f"📡 стрим: {session} (chat {chat_id})")

    def _stop_stream(self, chat_id):
        st = self.streams.pop(chat_id, None)
        if st:
            st["stop"] = True
        self._stream_threads.pop(chat_id, None)

    def _refresh_stream(self, chat_id):
        s = self.sessions.get(chat_id)
        if not s:
            self.api.send(chat_id, "⚠️ Нет активной сессии.", keyboard=self._nav_kb(chat_id))
            return
        st = self.streams.get(chat_id)
        if st:
            # снимаем паузу/подменю — возвращаемся к живому терминалу
            st["paused"] = False
            st["kb_mode"] = "pad"
            self.pending.pop(chat_id, None)
            self._rerender_stream(chat_id)
        else:
            self._start_stream(chat_id, s)

    def _stream_loop(self, chat_id):
        my = self.streams.get(chat_id)
        if not my:
            return
        # Минимум 3с между правками — Telegram лимитирует частое редактирование.
        interval = max(3.0, self._s("watch_interval", 3.0))
        idle_secs = max(60, self._s("idle_notify_minutes", 10) * 60)
        fails = 0
        while True:
            if self.streams.get(chat_id) is not my or my.get("stop"):
                break
            time.sleep(interval + min(fails * 2, 12))  # backoff при ошибках/429
            if self.streams.get(chat_id) is not my or my.get("stop"):
                break
            if my.get("paused"):
                continue  # открыт модальный ввод — терминал не перерисовываем
            session = my["session"]
            if not session_exists(session):
                self.api.send(chat_id, f"❌ Сессия «{session}» завершилась.", keyboard=self._menu_kb())
                self.streams.pop(chat_id, None)
                self.sessions.pop(chat_id, None)
                break
            try:
                raw = get_output(session, self.config["tmux"]["output_lines"])
                text = fmt_stream(session, raw)
                # Сравниваем «стабильную» версию (без спиннера/таймеров Claude),
                # чтобы анимация не вызывала постоянных правок и 429.
                stable = _strip_volatile(text)
                if stable != my.get("stable"):
                    my["stable"] = stable
                    my["last"] = text
                    my["last_change"] = time.time()
                    my["idle_notified"] = False
                    ok = self.api.edit(chat_id, my["msg_id"], text,
                                       keyboard=self._stream_kb(chat_id), html_mode=True)
                    fails = 0 if ok else fails + 1
                else:
                    # Детект простоя: тихо N минут → вероятно, задача готова.
                    # Не плодим отдельное сообщение без кнопок (оно «перебивает»
                    # терминал). Вместо этого шлём НОВОЕ сообщение (оно пингует)
                    # с баннером + актуальным экраном + пультом и делаем его
                    # новым якорем стрима, а старое удаляем — терминал один.
                    if not my["idle_notified"] and (time.time() - my["last_change"]) >= idle_secs:
                        my["idle_notified"] = True
                        mins = int(idle_secs / 60)
                        banner = f"💤 <b>«{session}»</b> тихо {mins} мин — вероятно, задача готова.\n"
                        # Новое сообщение (пингует) с пультом становится якорем
                        # для дальнейшего вывода. Старое НЕ удаляем — вдруг вы
                        # им сейчас пользуетесь; оно просто останется историей.
                        new_mid = self.api.send(chat_id, banner + text,
                                                keyboard=self._stream_kb(chat_id), html_mode=True)
                        if new_mid:
                            my["msg_id"] = new_mid
                            my["stable"] = stable      # чтобы не переиздавать сразу
            except Exception:
                pass
        if self._stream_threads.get(chat_id) is threading.current_thread():
            self._stream_threads.pop(chat_id, None)

    # ── Ввод в сессию ─────────────────────────────────────────

    def _send_to_session(self, chat_id, text):
        session = self.sessions.get(chat_id)
        if not session:
            self.api.send(chat_id, "⚠️ Нет активной сессии.", keyboard=self._nav_kb(chat_id))
            return
        if not session_exists(session):
            self.api.send(chat_id, f"❌ Сессия «{session}» не существует.", keyboard=self._menu_kb())
            self.sessions.pop(chat_id, None)
            return
        self.api.typing(chat_id)
        send_keys(session, text, press_enter=True)
        st = self.streams.get(chat_id)
        if st:
            st["paused"] = False       # ввод завершён — терминал снова живой
            st["kb_mode"] = "pad"
            self._rerender_stream(chat_id)
        else:
            self._start_stream(chat_id, session)

    def _send_key(self, chat_id, cmd):
        session = self.sessions.get(chat_id)
        if not session:
            return
        if cmd == "c":
            send_control_key(session, "C-c")
        else:
            key = KEY_MAP.get(cmd)
            if key:
                send_control_key(session, key)
        # стрим сам покажет результат
        if chat_id not in self.streams:
            self._start_stream(chat_id, session)

    def _send_ctrl(self, chat_id, ctrl):
        session = self.sessions.get(chat_id)
        if session:
            send_control_key(session, ctrl)

    # ── Планировщик ───────────────────────────────────────────

    def _cmd_schedule(self, chat_id, args, is_at):
        if not args.strip():
            ex = "/at 14:30" if is_at else "/in 5m"
            self.api.send(chat_id, f"⏰ Формат: {ex} сессия команда\n"
                                   f"Пайплайн: {ex} s | cmd1 | cmd2 | 30s",
                          keyboard=self._menu_kb())
            return
        if "|" in args:
            head, tail = args.split("|", 1)
            hp = head.strip().split(maxsplit=1)
            if len(hp) < 2:
                self.api.send(chat_id, "❌ Нужно: время сессия | команды", keyboard=self._menu_kb())
                return
            when, session = hp
            commands, delay = parse_pipeline_args(tail.strip())
        else:
            parts = args.split(maxsplit=2)
            if len(parts) < 3:
                self.api.send(chat_id, "❌ Нужно: время сессия команда", keyboard=self._menu_kb())
                return
            when, session, cmd = parts
            commands, delay = [cmd], 10
        ts, desc = (_parse_at_time(when) if is_at else _parse_in_time(when))
        if ts is None:
            self.api.send(chat_id, f"❌ {desc}", keyboard=self._menu_kb())
            return
        self._add_task(chat_id, ts, session, commands, delay, desc)

    def _schedule_from_text(self, chat_id, when, text):
        pass  # (интерактивный ввод не используется — планируем командой)

    def _add_task(self, chat_id, ts, session, commands, delay, desc):
        tid = uuid.uuid4().hex[:8]
        task = ScheduledTask(tid, ts, session.strip(), commands[0], chat_id, chat_id,
                             commands=commands, inter_delay=delay)
        self.scheduler.add_task(task)
        when = _fmt_time(ts)
        if task.is_pipeline:
            body = "\n".join(f"  {i+1}. {c[:60]}" for i, c in enumerate(commands))
            self.api.send(chat_id, f"⏰ Пайплайн запланирован (id {tid})\n"
                                   f"Сессия: {session}\n{body}\nЗадержка: {delay}с\nСтарт: {when}",
                          keyboard=ikb([[("⏰ Задачи", "tasks"), ("🏠 Меню", "menu")]]))
        else:
            self.api.send(chat_id, f"⏰ Задача {tid}: «{commands[0][:60]}» в сессии {session} — {when}",
                          keyboard=ikb([[("⏰ Задачи", "tasks"), ("🏠 Меню", "menu")]]))

    def _cmd_tasks(self, chat_id):
        tasks = self.scheduler.list_tasks(chat_id)
        if not tasks:
            self.api.send(chat_id, "📭 Нет запланированных задач.\n"
                                   "Создать: /in 5m сессия команда",
                          keyboard=self._menu_kb())
            return
        lines = [f"📋 <b>Задачи</b> ({len(tasks)}):"]
        rows = []
        for t in tasks:
            lines.append(f"• <code>{t.id}</code> {t.summary()}")
            rows.append([(f"🗑 Отменить {t.id}", f"cancel:{t.id}")])
        rows.append([("🖥 Сессии", "ls"), ("🏠 Меню", "menu")])
        self.api.send(chat_id, "\n".join(lines), keyboard=ikb(rows), html_mode=True)

    def _cmd_cancel(self, chat_id, tid):
        if not tid:
            self._cmd_tasks(chat_id)
            return
        task = self.scheduler.get_task(tid)
        if not task or task.user_id != chat_id:
            self.api.send(chat_id, f"❌ Задача {tid} не найдена.", keyboard=self._menu_kb())
            return
        self.scheduler.remove_task(tid)
        self.api.send(chat_id, f"✅ Задача {tid} отменена.")
        self._cmd_tasks(chat_id)

    def _execute_task(self, task):
        chat_id = task.peer_id
        session = task.session_name
        if not session_exists(session):
            if not self._create_tmux(session):
                self.api.send(chat_id, f"⏰❌ Не создать сессию {session}")
                return
        self.sessions[chat_id] = session
        time.sleep(0.4)
        total = len(task.commands)
        if task.is_pipeline:
            self.api.send(chat_id, f"⏰ Пайплайн {task.id}: {total} команд в «{session}»")
        for i, cmd in enumerate(task.commands):
            send_keys(session, cmd, press_enter=True)
            if i < total - 1:
                time.sleep(task.inter_delay)
        time.sleep(0.6)
        self.api.send(chat_id, f"⏰✅ Выполнено в «{session}»:\n"
                      + pre_block("\n".join(clean_pane(get_output(session, 30)).split("\n")[-20:])),
                      keyboard=self._pad_kb(), html_mode=True)
        self._start_stream(chat_id, session)
