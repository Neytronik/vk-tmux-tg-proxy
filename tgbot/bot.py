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
        self._lock = threading.Lock()

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
        return uid in ids

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

        self.api = TgBotApi(token)
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
                    try:
                        self._handle_update(upd)
                    except Exception as e:
                        print(f"⚠️ Ошибка обработки: {e}")
            except Exception as e:
                print(f"❌ Poll: {e}")
                time.sleep(3)

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

        # Режим ожидания ввода (имя сессии и т.п.)
        if chat_id in self.pending and not text.startswith("/"):
            mode = self.pending.pop(chat_id)
            self._handle_pending(chat_id, mode, text)
            return

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
            self._cmd_menu(chat_id)

    def _handle_pending(self, chat_id, mode, text):
        if mode == "new":
            name = text.strip().split()[0]
            self._create_and_open(chat_id, name)
        elif mode == "send":
            self._send_to_session(chat_id, text)
        elif mode.startswith("sched:"):
            when = mode.split(":", 1)[1]
            self._schedule_from_text(chat_id, when, text)

    # ── Callback (inline-кнопки) ──────────────────────────────

    def _handle_callback(self, cb):
        chat_id = cb["message"]["chat"]["id"]
        uid = cb.get("from", {}).get("id", 0)
        data = cb.get("data", "")
        self.api.answer_callback(cb["id"])
        if not self._allowed(uid):
            return

        if data == "menu":
            self._cmd_menu(chat_id)
        elif data == "ls":
            self._cmd_ls(chat_id)
        elif data == "claude":
            self._launch_ai(chat_id, "claude")
        elif data == "dcc":
            self._launch_ai(chat_id, "dcc")
        elif data == "new":
            self.pending[chat_id] = "new"
            self.api.send(chat_id, "➕ Имя новой сессии:")
        elif data == "tasks":
            self._cmd_tasks(chat_id)
        elif data == "stop":
            self._stop_stream(chat_id)
            self.api.send(chat_id, "🛑 Стрим остановлен.")
        elif data == "o":
            self._refresh_stream(chat_id)
        elif data.startswith("open:"):
            self._attach_and_stream(chat_id, data[5:])
        elif data.startswith("kill:"):
            self._kill(chat_id, data[5:])
        elif data.startswith("k:"):
            self._send_key(chat_id, data[2:])
        elif data == "input":
            self.pending[chat_id] = "send"
            self.api.send(chat_id, "📝 Текст для отправки в сессию:")

    # ── Команды ───────────────────────────────────────────────

    def _command(self, chat_id, uid, cmd, args):
        if cmd in ("start", "help", "помощь"):
            self._cmd_help(chat_id)
        elif cmd in ("menu", "меню"):
            self._cmd_menu(chat_id)
        elif cmd in ("ls", "sessions", "сессии"):
            self._cmd_ls(chat_id)
        elif cmd in ("new", "новая"):
            if args.strip():
                self._create_and_open(chat_id, args.strip().split()[0], args)
            else:
                self.pending[chat_id] = "new"
                self.api.send(chat_id, "➕ Имя новой сессии (можно: имя команда):")
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
                self.api.send(chat_id, "📝 Текст:")
        elif cmd in ("in", "через"):
            self._cmd_schedule(chat_id, args, is_at=False)
        elif cmd in ("at", "в"):
            self._cmd_schedule(chat_id, args, is_at=True)
        elif cmd in ("tasks", "задачи"):
            self._cmd_tasks(chat_id)
        elif cmd in ("cancel", "отмена"):
            self._cmd_cancel(chat_id, args.strip())
        else:
            self.api.send(chat_id, f"❓ Неизвестная команда: /{cmd}\n/help — список")

    def _cmd_help(self, chat_id):
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
        self.api.send(chat_id, msg, keyboard=self._menu_kb(), html_mode=True)

    def _menu_kb(self):
        return ikb([
            [("🖥 Сессии", "ls"), ("➕ Новая", "new")],
            [("🤖 Claude", "claude"), ("🧠 DeepClaude", "dcc")],
            [("⏰ Задачи", "tasks"), ("ℹ️ Помощь", "menu")],
        ])

    def _cmd_menu(self, chat_id):
        self.api.send(chat_id, "⚡ Главное меню — выберите действие:", keyboard=self._menu_kb())

    def _cmd_ls(self, chat_id):
        sessions = list_sessions()
        cur = self.sessions.get(chat_id)
        if not sessions:
            self.api.send(chat_id, "Нет сессий. Создайте новую:", keyboard=ikb([
                [("➕ Новая", "new"), ("🤖 Claude", "claude")]]))
            return
        rows = []
        for s in sessions:
            mark = "▶ " if s == cur else ""
            rows.append([(f"{mark}📺 {s}", f"open:{s}")])
        rows.append([("➕ Новая", "new"), ("🏠 Меню", "menu")])
        self.api.send(chat_id, f"🖥 Сессии ({len(sessions)}). Тап — подключиться:", keyboard=ikb(rows))

    # ── Сессии / стрим ────────────────────────────────────────

    def _create_tmux(self, name):
        t = self.config["tmux"]
        return create_session(name, work_dir=t.get("work_dir"),
                              width=t.get("term_width"), height=t.get("term_height"))

    def _create_and_open(self, chat_id, name, full_args=None):
        import re
        if not re.match(r"^[a-zA-Z0-9_-]+$", name or ""):
            self.api.send(chat_id, "❌ Имя: латиница, цифры, - и _.")
            return
        if session_exists(name):
            self._attach_and_stream(chat_id, name)
            return
        if not self._create_tmux(name):
            self.api.send(chat_id, "❌ Не удалось создать сессию.")
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

    def _launch_ai(self, chat_id, kind):
        session = kind  # 'claude' или 'dcc'
        cfg = self.config.get("claude", {})
        command = cfg.get("command", "claude") if kind == "claude" else cfg.get("deepclaude_command", "dcc")
        label = "🤖 Claude Code" if kind == "claude" else "🧠 DeepClaude"
        self.api.typing(chat_id)
        if not session_exists(session):
            if not self._create_tmux(session):
                self.api.send(chat_id, "❌ Не удалось создать сессию.")
                return
            time.sleep(0.4)
            send_keys(session, command, press_enter=True)
            time.sleep(1.2)
            print(f"{label}: запущен")
        self._attach_and_stream(chat_id, session)

    def _attach_and_stream(self, chat_id, name):
        if not session_exists(name):
            self.api.send(chat_id, f"❌ Сессии «{name}» нет.")
            return
        self.sessions[chat_id] = name
        self._save_sessions()
        self._start_stream(chat_id, name)

    def _detach(self, chat_id):
        self._stop_stream(chat_id)
        self.sessions.pop(chat_id, None)
        self._save_sessions()
        self.api.send(chat_id, "🔌 Отключено. Сессия продолжает работать.",
                      keyboard=self._menu_kb())

    def _kill(self, chat_id, name):
        if not name:
            return
        if self.sessions.get(chat_id) == name:
            self._stop_stream(chat_id)
            self.sessions.pop(chat_id, None)
            self._save_sessions()
        if kill_session(name):
            # После удаления показываем ОБНОВЛЁННЫЙ список (не застрявшие кнопки)
            self._cmd_ls(chat_id)
        else:
            self.api.send(chat_id, f"❌ Не удалось удалить «{name}».")

    def _pad_kb(self):
        return ikb([
            [("⬆️", "k:up"), ("⏎", "k:e"), ("⎋ Esc", "k:esc")],
            [("⬅️", "k:left"), ("⬇️", "k:down"), ("➡️", "k:right")],
            [("⇥ Tab", "k:tab"), ("⇧⇥", "k:btab"), ("⛔ Ctrl+C", "k:c")],
            [("📝 Текст", "input"), ("🔄", "o"), ("🛑 Стоп", "stop")],
            [("🖥 Сессии", "ls"), ("🏠 Меню", "menu")],
        ])

    def _start_stream(self, chat_id, session):
        self._stop_stream(chat_id)
        text = fmt_stream(session, get_output(session, self.config["tmux"]["output_lines"]))
        mid = self.api.send(chat_id, text, keyboard=self._pad_kb(), html_mode=True)
        st = {"session": session, "msg_id": mid, "stop": False,
              "last": text, "last_change": time.time(), "idle_notified": False}
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
            self.api.send(chat_id, "⚠️ Нет активной сессии. /ls")
            return
        if chat_id not in self.streams:
            self._start_stream(chat_id, s)

    def _stream_loop(self, chat_id):
        my = self.streams.get(chat_id)
        if not my:
            return
        interval = max(2.0, self.config["tmux"].get("watch_interval", 2.0))
        idle_secs = max(60, self.config["tmux"].get("idle_notify_minutes", 10) * 60)
        while True:
            if self.streams.get(chat_id) is not my or my.get("stop"):
                break
            time.sleep(interval)
            if self.streams.get(chat_id) is not my or my.get("stop"):
                break
            session = my["session"]
            if not session_exists(session):
                self.api.send(chat_id, f"❌ Сессия «{session}» завершилась.")
                self.streams.pop(chat_id, None)
                self.sessions.pop(chat_id, None)
                break
            try:
                raw = get_output(session, self.config["tmux"]["output_lines"])
                text = fmt_stream(session, raw)
                if text != my.get("last"):
                    my["last"] = text
                    my["last_change"] = time.time()
                    my["idle_notified"] = False
                    self.api.edit(chat_id, my["msg_id"], text, keyboard=self._pad_kb(), html_mode=True)
                else:
                    # детект простоя
                    if not my["idle_notified"] and (time.time() - my["last_change"]) >= idle_secs:
                        my["idle_notified"] = True
                        tail = "\n".join(clean_pane(raw).split("\n")[-20:])
                        mins = int(idle_secs / 60)
                        self.api.send(
                            chat_id,
                            f"💤 «{session}» без изменений {mins} мин — вероятно, задача готова.\n"
                            + pre_block(tail[-3500:]),
                            html_mode=True)
            except Exception:
                pass
        if self._stream_threads.get(chat_id) is threading.current_thread():
            self._stream_threads.pop(chat_id, None)

    # ── Ввод в сессию ─────────────────────────────────────────

    def _send_to_session(self, chat_id, text):
        session = self.sessions.get(chat_id)
        if not session:
            self.api.send(chat_id, "⚠️ Нет активной сессии. /ls")
            return
        if not session_exists(session):
            self.api.send(chat_id, f"❌ Сессия «{session}» не существует.")
            self.sessions.pop(chat_id, None)
            return
        self.api.typing(chat_id)
        send_keys(session, text, press_enter=True)
        if chat_id not in self.streams:
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
                                   f"Пайплайн: {ex} s | cmd1 | cmd2 | 30s")
            return
        if "|" in args:
            head, tail = args.split("|", 1)
            hp = head.strip().split(maxsplit=1)
            if len(hp) < 2:
                self.api.send(chat_id, "❌ Нужно: время сессия | команды")
                return
            when, session = hp
            commands, delay = parse_pipeline_args(tail.strip())
        else:
            parts = args.split(maxsplit=2)
            if len(parts) < 3:
                self.api.send(chat_id, "❌ Нужно: время сессия команда")
                return
            when, session, cmd = parts
            commands, delay = [cmd], 10
        ts, desc = (_parse_at_time(when) if is_at else _parse_in_time(when))
        if ts is None:
            self.api.send(chat_id, f"❌ {desc}")
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
                                   f"Сессия: {session}\n{body}\nЗадержка: {delay}с\nСтарт: {when}")
        else:
            self.api.send(chat_id, f"⏰ Задача {tid}: «{commands[0][:60]}» в сессии {session} — {when}\n"
                                   f"Отмена: /cancel {tid}")

    def _cmd_tasks(self, chat_id):
        tasks = self.scheduler.list_tasks(chat_id)
        if not tasks:
            self.api.send(chat_id, "📭 Нет задач.\n/in 5m сессия команда — создать",
                          keyboard=self._menu_kb())
            return
        lines = [f"📋 Задачи ({len(tasks)}):"]
        for t in tasks:
            lines.append(f"• {t.id}: {t.summary()}")
            lines.append(f"   отмена: /cancel {t.id}")
        self.api.send(chat_id, "\n".join(lines))

    def _cmd_cancel(self, chat_id, tid):
        if not tid:
            self._cmd_tasks(chat_id)
            return
        task = self.scheduler.get_task(tid)
        if not task or task.user_id != chat_id:
            self.api.send(chat_id, f"❌ Задача {tid} не найдена.")
            return
        self.scheduler.remove_task(tid)
        self.api.send(chat_id, f"✅ Задача {tid} отменена.")

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
