"""Планировщик отложенных задач.

Позволяет:
- Запланировать создание сессии и запуск команды через N минут/часов
- Запланировать команду в существующую сессию в конкретное время
- Просмотреть список задач
- Отменить задачу

Задачи сохраняются в JSON между перезапусками.
"""
import os
import json
import time
import threading
import uuid
from datetime import datetime, timedelta

from .config import CONFIG_DIR

SCHEDULE_FILE = os.path.join(CONFIG_DIR, "scheduled.json")


def _now():
    return time.time()


def _fmt_time(ts):
    """Форматировать timestamp в читаемую строку."""
    return datetime.fromtimestamp(ts).strftime("%H:%M %d.%m.%Y")


def _parse_at_time(time_str):
    """Разобрать строку времени HH:MM → timestamp сегодня.
    Возвращает (timestamp, описание) или (None, ошибка).
    """
    try:
        parts = time_str.strip().split(":")
        if len(parts) != 2:
            return None, "Формат: ЧЧ:ММ (например, 14:30)"
        hours, minutes = int(parts[0]), int(parts[1])
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            return None, "Часы 0-23, минуты 0-59"

        now = datetime.now()
        target = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)

        # Если время уже прошло сегодня — переносим на завтра
        if target <= now:
            target += timedelta(days=1)

        return target.timestamp(), target.strftime("сегодня в %H:%M" if target.date() == now.date() else "%d.%m в %H:%M")
    except ValueError:
        return None, "Неверный формат времени"


def _parse_in_time(time_str):
    """Разобрать строку 'N[m|h]' → timestamp.
    Возвращает (timestamp, описание) или (None, ошибка).
    """
    import re
    match = re.match(r"^(\d+)\s*(m|min|мин|минут|h|hour|час|часов?)?$", time_str.strip().lower())
    if not match:
        return None, "Формат: число + m/h (например, 5m, 2h, 30мин, 1час)"

    value = int(match.group(1))
    unit = match.group(2) or "m"

    if unit in ("h", "hour", "час", "часов"):
        seconds = value * 3600
        desc = f"через {value} ч."
    else:
        seconds = value * 60
        desc = f"через {value} мин."

    if seconds <= 0:
        return None, "Время должно быть больше 0"
    if seconds > 86400 * 7:  # максимум 7 дней
        return None, "Максимум 7 дней"

    return _now() + seconds, desc


def parse_pipeline_args(args_str):
    """Разобрать строку пайплайна: команды через | и опциональная задержка в конце.

    Формат: \"команда1 | команда2 | ... | 30s\"
    Возвращает (commands, inter_delay).

    Примеры:
        parse_pipeline_args(\"echo hello | ls -la | 30s\")
        → ([\"echo hello\", \"ls -la\"], 30)

        parse_pipeline_args(\"echo hello\")
        → ([\"echo hello\"], 10)  # дефолтная задержка

        parse_pipeline_args(\"cmd1 | cmd2 | cmd3\")
        → ([\"cmd1\", \"cmd2\", \"cmd3\"], 10)
    """
    if not args_str:
        return [], 10

    # Разбиваем по | (но не внутри кавычек)
    parts = _smart_split(args_str, "|")
    parts = [p.strip() for p in parts if p.strip()]

    if not parts:
        return [], 10

    # Проверяем: последний элемент — задержка?
    delay = _parse_delay(parts[-1])
    if delay is not None and len(parts) > 1:
        # Последний элемент — задержка
        commands = parts[:-1]
        inter_delay = delay
    elif delay is not None and len(parts) == 1:
        # Только задержка? Значит это просто число (например, id)
        commands = parts
        inter_delay = 10
    else:
        commands = parts
        inter_delay = 10

    # Чистим кавычки вокруг команд
    commands = [_strip_quotes(c) for c in commands]

    return commands, inter_delay


def _smart_split(text, separator):
    """Разбить строку по разделителю, но не внутри кавычек."""
    import re
    # Находим все сегменты: либо в кавычках, либо без
    pattern = r'(?:"[^"]*"|[^' + re.escape(separator) + r']+)'
    return re.findall(pattern, text)


def _strip_quotes(s):
    """Убрать обрамляющие кавычки если есть."""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1]
    if len(s) >= 2 and s[0] == "«" and s[-1] == "»":
        return s[1:-1]
    return s


def _parse_delay(time_str):
    """Разобрать задержку между командами: '10s', '30s', '1m', '5min'.
    Возвращает секунды (int) или None если не похоже на задержку.
    """
    import re
    match = re.match(r"^\s*(\d+)\s*(s|sec|сек|m|min|мин|минут[а]?)?\s*$", time_str.strip().lower())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2) or "s"

    if unit in ("m", "min", "мин", "минут", "минута"):
        return value * 60
    else:
        return value


class ScheduledTask:
    """Одна отложенная задача.

    Поддерживает как одиночную команду, так и пайплайн из нескольких команд
    с задержкой между ними.
    """
    def __init__(self, task_id, trigger_time, session_name, command,
                 user_id, peer_id, task_type="create_and_run",
                 commands=None, inter_delay=10):
        self.id = task_id
        self.trigger_time = trigger_time       # unix timestamp
        self.session_name = session_name
        self.command = command                  # одиночная команда (для обратной совместимости)
        self.user_id = user_id
        self.peer_id = peer_id
        self.task_type = task_type
        self.created_at = _now()
        # Пайплайн: список команд и задержка между ними (сек)
        self.commands = commands or ([command] if command else [])
        self.inter_delay = inter_delay          # секунд между командами

    @property
    def is_pipeline(self):
        """True если это пайплайн из нескольких команд."""
        return len(self.commands) > 1

    def to_dict(self):
        return {
            "id": self.id,
            "trigger_time": self.trigger_time,
            "session_name": self.session_name,
            "command": self.command,
            "user_id": self.user_id,
            "peer_id": self.peer_id,
            "task_type": self.task_type,
            "created_at": self.created_at,
            "commands": self.commands,
            "inter_delay": self.inter_delay,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            task_id=d["id"],
            trigger_time=d["trigger_time"],
            session_name=d["session_name"],
            command=d["command"],
            user_id=d["user_id"],
            peer_id=d["peer_id"],
            task_type=d.get("task_type", "create_and_run"),
            commands=d.get("commands", []),
            inter_delay=d.get("inter_delay", 10),
        )

    def summary(self):
        """Краткое описание для вывода пользователю."""
        when = _fmt_time(self.trigger_time)
        prefix = "🆕" if self.task_type == "create_and_run" else "▶"
        if self.is_pipeline:
            cmds = " | ".join(c[:40] for c in self.commands)
            return f"{prefix} `{self.session_name}`: {len(self.commands)} команд через {self.inter_delay}с — {when}"
        else:
            cmd = (self.command or "")[:60]
            return f"{prefix} `{self.session_name}`: `{cmd}` — {when}"


class Scheduler:
    """Менеджер отложенных задач."""

    def __init__(self, execute_callback):
        """
        execute_callback(task) — будет вызван когда задача готова к выполнению.
        Должен вернуть True/False.
        """
        self._tasks = {}         # {task_id: ScheduledTask}
        self._callback = execute_callback
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    # ── Управление задачами ────────────────────────────────────

    def add_task(self, task):
        """Добавить задачу."""
        with self._lock:
            self._tasks[task.id] = task
        self._save()

    def remove_task(self, task_id):
        """Удалить задачу по ID (или префиксу)."""
        found = False
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                found = True
            else:
                for tid in list(self._tasks.keys()):
                    if tid.startswith(task_id):
                        del self._tasks[tid]
                        found = True
                        break
        if found:
            self._save()  # вне лока — избегаем deadlock
        return found

    def list_tasks(self, user_id=None):
        """Список задач (всех или для конкретного пользователя)."""
        with self._lock:
            tasks = list(self._tasks.values())
        if user_id is not None:
            tasks = [t for t in tasks if t.user_id == user_id]
        # Сортировка по времени
        tasks.sort(key=lambda t: t.trigger_time)
        return tasks

    def get_task(self, task_id):
        """Найти задачу по ID или префиксу."""
        with self._lock:
            if task_id in self._tasks:
                return self._tasks[task_id]
            for tid, task in self._tasks.items():
                if tid.startswith(task_id):
                    return task
        return None

    # ── Запуск / остановка ──────────────────────────────────────

    def start(self):
        """Запустить поток планировщика."""
        self._load()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

        # Лог
        pending = len(self._tasks)
        if pending:
            print(f"⏰ Планировщик запущен — {pending} задач ожидают")

    def stop(self):
        """Остановить планировщик."""
        self._running = False
        self._save()

    # ── Внутренний цикл ─────────────────────────────────────────

    def _loop(self):
        """Проверять задачи каждые 10 секунд."""
        while self._running:
            try:
                self._check_due()
            except Exception:
                pass
            time.sleep(10)

    def _check_due(self):
        """Выполнить задачи, время которых наступило."""
        now = _now()
        due = []

        with self._lock:
            for task_id, task in list(self._tasks.items()):
                if task.trigger_time <= now:
                    due.append(task)
                    del self._tasks[task_id]

        if due:
            self._save()
            for task in due:
                try:
                    if task.is_pipeline:
                        print(f"⏰ Выполняю пайплайн {task.id[:8]}: {task.summary()}")
                    else:
                        print(f"⏰ Выполняю задачу {task.id[:8]}: {task.summary()}")
                    self._callback(task)
                except Exception as e:
                    print(f"❌ Ошибка выполнения задачи {task.id[:8]}: {e}")

    # ── Сохранение / загрузка ───────────────────────────────────

    def _save(self):
        """Сохранить задачи в JSON."""
        try:
            os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
        except Exception:
            return  # не можем создать директорию — молча пропускаем
        with self._lock:
            data = [t.to_dict() for t in self._tasks.values()]
        try:
            with open(SCHEDULE_FILE, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass  # не можем записать — не критично для работы

    def _load(self):
        """Загрузить задачи из JSON."""
        if not os.path.exists(SCHEDULE_FILE):
            return
        try:
            with open(SCHEDULE_FILE, "r") as f:
                data = json.load(f)
            with self._lock:
                for item in data:
                    try:
                        task = ScheduledTask.from_dict(item)
                        # Пропускаем задачи, которые должны были выполниться
                        # больше 1 часа назад (видимо, бот был выключен)
                        if task.trigger_time < _now() - 3600:
                            print(f"⏭ Пропускаю просроченную задачу: {task.summary()}")
                            continue
                        self._tasks[task.id] = task
                    except (KeyError, ValueError) as e:
                        print(f"⚠️ Ошибка загрузки задачи: {e}")
        except (json.JSONDecodeError, IOError):
            pass

    # ── Статистика ──────────────────────────────────────────────

    def count(self, user_id=None):
        """Количество ожидающих задач."""
        tasks = self.list_tasks(user_id)
        return len(tasks)
