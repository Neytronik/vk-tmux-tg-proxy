#!/usr/bin/env python3
"""Тесты для VK Tmux Bot.

Запуск:
    .venv/bin/python tests.py

Проверяет:
    1. tmux_handler — все функции работы с tmux
    2. vk_api — клавиатуры, обработка ошибок, невалидный токен
    3. state_manager — сохранение/загрузка состояния
    4. bot — форматирование вывода, обработка команд (unit)
"""
import sys
import os
import json
import time
import tempfile
import unittest

# Добавляем проект в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vkbot.tmux_handler import (
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
    _filter_separators,
    _tmux,
)
from vkbot.vk_api import (
    VkApi,
    VkApiError,
    make_keyboard,
    make_main_keyboard,
    make_sessions_keyboard,
    make_kill_keyboard,
    make_watch_keyboard,
    make_notify_keyboard,
)
from vkbot.state_manager import save_state, load_state
from vkbot.config import load_config, save_config, CONFIG_DIR
from vkbot.scheduler import (
    Scheduler, ScheduledTask, _parse_at_time, _parse_in_time,
    _fmt_time, _now, parse_pipeline_args, _parse_delay,
)
from datetime import datetime, timedelta

# ── Вспомогательные функции ──────────────────────────────────────

TEST_SESSION = "vkbot_test_session_do_not_use"


def has_tmux():
    """Проверить, что tmux доступен (исполняемый файл есть и работает с пользовательским сокетом)."""
    import subprocess
    sock = f"/tmp/tmux-{os.getuid()}/default"
    try:
        result = subprocess.run(
            ["tmux", "-S", sock, "-V"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def cleanup_test_session():
    """Удалить тестовую сессию если есть."""
    if session_exists(TEST_SESSION):
        kill_session(TEST_SESSION)


# ── Тесты tmux_handler ───────────────────────────────────────────

class TestTmuxHandler(unittest.TestCase):
    """Тесты работы с tmux."""

    @classmethod
    def setUpClass(cls):
        if not has_tmux():
            raise unittest.SkipTest("tmux не установлен — пропускаем интеграционные тесты")
        cleanup_test_session()

    @classmethod
    def tearDownClass(cls):
        cleanup_test_session()

    def test_01_create_session(self):
        """Создание новой сессии."""
        self.assertFalse(session_exists(TEST_SESSION),
                        f"Сессия {TEST_SESSION} не должна существовать до теста")

        result = create_session(TEST_SESSION)
        self.assertTrue(result, "create_session должен вернуть True")
        self.assertTrue(session_exists(TEST_SESSION),
                       f"Сессия {TEST_SESSION} должна существовать после создания")

    def test_02_list_sessions(self):
        """Список сессий содержит созданную."""
        sessions = list_sessions()
        self.assertIn(TEST_SESSION, sessions,
                     f"Сессия {TEST_SESSION} должна быть в списке")

    def test_03_send_keys_and_get_output(self):
        """Отправка команды и получение вывода."""
        # Отправляем echo
        ok = send_keys(TEST_SESSION, "echo HELLO_FROM_VKBOT_TEST", press_enter=True)
        self.assertTrue(ok, "send_keys должен сработать")

        # Ждём немного
        time.sleep(0.5)

        # Получаем вывод
        output = get_output(TEST_SESSION, lines=20)
        self.assertIn("HELLO_FROM_VKBOT_TEST", output,
                     f"Вывод должен содержать тестовую строку, получили: {output[:200]}")

    def test_04_send_keys_no_enter(self):
        """Отправка текста без Enter."""
        ok = send_keys(TEST_SESSION, "partial_command", press_enter=False)
        self.assertTrue(ok, "send_keys без Enter должен сработать")

    def test_05_send_control_c(self):
        """Отправка Ctrl+C (безопасно для сессии)."""
        ok = send_control_key(TEST_SESSION, "C-c")
        self.assertTrue(ok, "send_control_key C-c должен сработать")

    def test_06_detect_errors(self):
        """Обнаружение ошибок в выводе."""
        # Создаём строку с ошибкой
        test_output = "Line 1\nError: something went wrong\nLine 3\nFailed to connect\nLine 5"
        errors = detect_errors(test_output)
        self.assertGreaterEqual(len(errors), 2,
                               f"Должно найти минимум 2 ошибки, нашлось: {len(errors)}")
        self.assertTrue(any("Error:" in e for e in errors),
                       "Должна быть ошибка с 'Error:'")
        self.assertTrue(any("Failed" in e for e in errors),
                       "Должна быть ошибка с 'Failed'")

    def test_07_highlight_errors(self):
        """Подсветка ошибок в выводе."""
        test_output = "Normal line\nError: bad thing\nAnother normal line"
        highlighted = highlight_errors(test_output)
        self.assertIn("❌ Error:", highlighted,
                     "Строки с ошибками должны начинаться с ❌")
        self.assertNotIn("❌ Normal line", highlighted,
                        "Нормальные строки не должны иметь ❌")
        self.assertNotIn("❌ Another normal", highlighted,
                        "Нормальные строки не должны иметь ❌")

    def test_08_detect_session_state(self):
        """Определение состояния сессии."""
        # idle — есть приглашение
        idle_output = "some output\nuser@host:~$ "
        self.assertEqual(detect_session_state(idle_output), "idle")

        # error
        error_output = "some output\nError: connection refused\nuser@host:~$ "
        self.assertEqual(detect_session_state(error_output), "error")

        # running — нет приглашения
        running_output = "Building... 45%\nCompiling...\n"
        self.assertEqual(detect_session_state(running_output), "build")

        # prompt — y/n
        prompt_output = "Are you sure? (y/n)"
        self.assertEqual(detect_session_state(prompt_output), "prompt")

    def test_09_filter_separators(self):
        """Фильтрация строк-разделителей."""
        test = "line1\n──────────────\nline2\n--------------\nline3"
        filtered = _filter_separators(test)
        self.assertIn("line1", filtered)
        self.assertIn("line2", filtered)
        self.assertIn("line3", filtered)
        # Проверяем, что разделители убраны
        lines = filtered.split("\n")
        self.assertEqual(len(lines), 3, f"Должно быть 3 строки, получили: {lines}")

    def test_10_get_output_nonexistent(self):
        """Получение вывода несуществующей сессии."""
        output = get_output("nonexistent_session_xyz_123", lines=10)
        self.assertTrue(output.startswith("❌"),
                       f"Должна быть ошибка, получили: {output[:100]}")

    def test_11_session_exists_negative(self):
        """Проверка несуществующей сессии."""
        self.assertFalse(session_exists("nonexistent_xyz_456"))

    def test_12_send_ctrl_d(self):
        """Ctrl+D (убьёт shell — сессия исчезнет). Создаём отдельную сессию."""
        ctrl_d_session = "vkbot_test_ctrl_d"
        cleanup_test_session_ctrl_d = lambda: kill_session(ctrl_d_session) if session_exists(ctrl_d_session) else None

        # Создаём временную сессию
        self.assertTrue(create_session(ctrl_d_session), "Должна создаться временная сессия")
        # Ждём инициализации shell
        time.sleep(0.3)
        # Ctrl+D — выйдет из shell, окно закроется, сессия умрёт
        ok = send_control_key(ctrl_d_session, "C-d")
        self.assertTrue(ok, "Ctrl+D должен сработать")
        time.sleep(0.3)
        # Сессия должна исчезнуть (окно закрылось)
        self.assertFalse(session_exists(ctrl_d_session),
                        "Сессия должна исчезнуть после Ctrl+D")

    def test_13_kill_session(self):
        """Удаление основной тестовой сессии."""
        self.assertTrue(session_exists(TEST_SESSION),
                       "Сессия должна существовать перед удалением")
        result = kill_session(TEST_SESSION)
        self.assertTrue(result, "kill_session должен вернуть True")
        self.assertFalse(session_exists(TEST_SESSION),
                        "Сессии не должно быть после удаления")


# ── Тесты планировщика ────────────────────────────────────────────

class TestScheduler(unittest.TestCase):
    """Тесты планировщика отложенных задач."""

    def setUp(self):
        self.results = []
        self.scheduler = Scheduler(execute_callback=lambda t: self.results.append(t))

    def test_add_and_list_tasks(self):
        """Добавление и список задач."""
        task = ScheduledTask(
            task_id="test001",
            trigger_time=_now() + 3600,
            session_name="test_session",
            command="echo hello",
            user_id=123,
            peer_id=456,
        )
        self.scheduler.add_task(task)
        tasks = self.scheduler.list_tasks(user_id=123)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].id, "test001")
        self.assertEqual(tasks[0].session_name, "test_session")

    def test_remove_task(self):
        """Удаление задачи."""
        task = ScheduledTask("test002", _now() + 7200, "s", "cmd", 1, 2)
        self.scheduler.add_task(task)
        self.assertTrue(self.scheduler.remove_task("test002"))
        self.assertEqual(len(self.scheduler.list_tasks()), 0)

    def test_remove_by_prefix(self):
        """Удаление по префиксу ID."""
        task = ScheduledTask("abcdef01", _now() + 100, "s", "c", 1, 2)
        self.scheduler.add_task(task)
        self.assertTrue(self.scheduler.remove_task("abc"))
        self.assertEqual(len(self.scheduler.list_tasks()), 0)

    def test_due_task_executes(self):
        """Просроченная задача выполняется."""
        task = ScheduledTask("due001", _now() - 1, "s", "cmd", 1, 2)
        self.scheduler.add_task(task)
        self.scheduler._check_due()
        self.assertEqual(len(self.results), 1)
        self.assertEqual(self.results[0].id, "due001")
        self.assertEqual(len(self.scheduler.list_tasks()), 0)

    def test_filter_by_user(self):
        """Фильтрация задач по пользователю."""
        t1 = ScheduledTask("t1", _now() + 100, "s", "c", user_id=1, peer_id=1)
        t2 = ScheduledTask("t2", _now() + 200, "s", "c", user_id=2, peer_id=2)
        self.scheduler.add_task(t1)
        self.scheduler.add_task(t2)
        self.assertEqual(len(self.scheduler.list_tasks(user_id=1)), 1)
        self.assertEqual(len(self.scheduler.list_tasks(user_id=2)), 1)
        self.assertEqual(len(self.scheduler.list_tasks()), 2)

    def test_task_summary(self):
        """Формат сводки задачи."""
        task = ScheduledTask("sum001", _now() + 3600, "build", "npm run build", 1, 2)
        summary = task.summary()
        self.assertIn("build", summary)
        self.assertIn("npm run build", summary)

    def test_parse_at_time(self):
        """Разбор времени HH:MM."""
        # Будущее время сегодня
        future = datetime.now() + timedelta(hours=2)
        ts, desc = _parse_at_time(future.strftime("%H:%M"))
        self.assertIsNotNone(ts)
        self.assertIn("сегодня", desc)
        # Прошедшее время — должно быть завтра
        past = datetime.now() - timedelta(hours=2)
        ts2, desc2 = _parse_at_time(past.strftime("%H:%M"))
        self.assertIsNotNone(ts2)
        # Неверный формат
        ts3, desc3 = _parse_at_time("abc")
        self.assertIsNone(ts3)
        # Неверные значения
        ts4, _ = _parse_at_time("25:00")
        self.assertIsNone(ts4)

    def test_parse_in_time(self):
        """Разбор относительного времени."""
        ts, desc = _parse_in_time("5m")
        self.assertIsNotNone(ts)
        self.assertIn("мин", desc)
        ts2, desc2 = _parse_in_time("2h")
        self.assertIsNotNone(ts2)
        self.assertIn("ч", desc2)
        ts3, _ = _parse_in_time("30мин")
        self.assertIsNotNone(ts3)
        ts4, _ = _parse_in_time("1час")
        self.assertIsNotNone(ts4)
        # Неверные
        ts5, _ = _parse_in_time("abc")
        self.assertIsNone(ts5)
        ts6, _ = _parse_in_time("0m")
        self.assertIsNone(ts6)

    def test_parse_delay(self):
        """Разбор задержки."""
        self.assertEqual(_parse_delay("10s"), 10)
        self.assertEqual(_parse_delay("30s"), 30)
        self.assertEqual(_parse_delay("1m"), 60)
        self.assertEqual(_parse_delay("5min"), 300)
        self.assertEqual(_parse_delay("30сек"), 30)
        self.assertEqual(_parse_delay("2мин"), 120)
        self.assertIsNone(_parse_delay("abc"))
        self.assertIsNone(_parse_delay("hello world"))

    def test_parse_pipeline_simple(self):
        """Пайплайн: одна команда."""
        cmds, delay = parse_pipeline_args("echo hello")
        self.assertEqual(cmds, ["echo hello"])
        self.assertEqual(delay, 10)

    def test_parse_pipeline_multi(self):
        """Пайплайн: несколько команд без задержки."""
        cmds, delay = parse_pipeline_args("cmd1 | cmd2 | cmd3")
        self.assertEqual(cmds, ["cmd1", "cmd2", "cmd3"])
        self.assertEqual(delay, 10)

    def test_parse_pipeline_with_delay(self):
        """Пайплайн: команды с задержкой в конце."""
        cmds, delay = parse_pipeline_args("cmd1 | cmd2 | 30s")
        self.assertEqual(cmds, ["cmd1", "cmd2"])
        self.assertEqual(delay, 30)

    def test_parse_pipeline_quoted(self):
        """Пайплайн: команды в кавычках."""
        cmds, delay = parse_pipeline_args('echo hello | "echo world with spaces" | 15s')
        self.assertEqual(cmds, ["echo hello", "echo world with spaces"])
        self.assertEqual(delay, 15)

    def test_parse_pipeline_russian_quotes(self):
        """Пайплайн: русские кавычки «»."""
        cmds, delay = parse_pipeline_args('cmd1 | «поставь петлю на 10 минут» | 30s')
        self.assertEqual(cmds, ["cmd1", "поставь петлю на 10 минут"])
        self.assertEqual(delay, 30)

    def test_parse_pipeline_delay_minutes(self):
        """Пайплайн: задержка в минутах."""
        cmds, delay = parse_pipeline_args("cmd1 | cmd2 | cmd3 | 2m")
        self.assertEqual(cmds, ["cmd1", "cmd2", "cmd3"])
        self.assertEqual(delay, 120)

    def test_scheduled_task_pipeline(self):
        """ScheduledTask: is_pipeline и commands."""
        task = ScheduledTask(
            task_id="pipe1",
            trigger_time=_now() + 3600,
            session_name="test",
            command="cmd1",
            user_id=1,
            peer_id=2,
            commands=["cmd1", "cmd2", "cmd3"],
            inter_delay=30,
        )
        self.assertTrue(task.is_pipeline)
        self.assertEqual(len(task.commands), 3)
        self.assertEqual(task.inter_delay, 30)
        self.assertIn("3 команд", task.summary())
        self.assertIn("30с", task.summary())

    def test_scheduled_task_single(self):
        """ScheduledTask: одиночная команда не пайплайн."""
        task = ScheduledTask(
            task_id="single1",
            trigger_time=_now() + 3600,
            session_name="test",
            command="echo hi",
            user_id=1,
            peer_id=2,
        )
        self.assertFalse(task.is_pipeline)
        self.assertEqual(task.commands, ["echo hi"])


# ── Тесты VK API ─────────────────────────────────────────────────

class TestVkKeyboards(unittest.TestCase):
    """Тесты генерации клавиатур VK."""

    def test_make_keyboard_structure(self):
        """Базовая структура клавиатуры."""
        kb = make_keyboard([
            [{"label": "A", "color": "primary", "payload": "/a"}],
            [{"label": "B", "color": "negative"}],
        ])
        self.assertIn("buttons", kb)
        self.assertEqual(len(kb["buttons"]), 2)
        self.assertEqual(kb["one_time"], False)
        # Проверка кнопки с payload
        btn = kb["buttons"][0][0]
        self.assertEqual(btn["action"]["label"], "A")
        self.assertEqual(btn["color"], "primary")
        self.assertIn("payload", btn["action"])
        payload = json.loads(btn["action"]["payload"])
        self.assertEqual(payload["cmd"], "/a")
        # Кнопка без payload
        btn2 = kb["buttons"][1][0]
        self.assertNotIn("payload", btn2["action"])

    def test_inline_keyboard(self):
        """Inline-клавиатура."""
        kb = make_keyboard(
            [[{"label": "X", "color": "primary"}]],
            inline=True,
        )
        self.assertTrue(kb.get("inline"))

    def test_one_time_keyboard(self):
        """Одноразовая клавиатура."""
        kb = make_keyboard(
            [[{"label": "X", "color": "primary"}]],
            one_time=True,
        )
        self.assertTrue(kb["one_time"])

    def test_main_keyboard_with_session(self):
        """Клавиатура с активной сессией."""
        kb = make_main_keyboard("mysession")
        self.assertIn("buttons", kb)
        labels = []
        for row in kb["buttons"]:
            for btn in row:
                labels.append(btn["action"]["label"])
        expected = ["📺 Вывод", "👁 Следить", "📝 Команда", "⏎ Enter",
                    "⛔ Ctrl+C", "🚪 Ctrl+D", "🔄 Сессии", "🗑 Удалить",
                    "🤖 Claude", "🧠 DeepClaude", "🔌 Откл.", "✈️ Telegram"]
        for exp in expected:
            self.assertIn(exp, labels, f"Кнопка '{exp}' должна быть в клавиатуре")

    def test_main_keyboard_without_session(self):
        """Клавиатура без активной сессии."""
        kb = make_main_keyboard(None)
        labels = []
        for row in kb["buttons"]:
            for btn in row:
                labels.append(btn["action"]["label"])
        self.assertIn("📋 Сессии", labels)
        self.assertIn("➕ Новая", labels)
        self.assertIn("ℹ️ Помощь", labels)
        self.assertIn("⏰ Отложить", labels)
        self.assertIn("✈️ Telegram", labels)
        self.assertIn("🤖 Claude", labels)

    def test_sessions_keyboard(self):
        """Клавиатура со списком сессий."""
        kb = make_sessions_keyboard(["sess1", "sess2", "sess3"], current="sess2")
        labels = []
        for row in kb["buttons"]:
            for btn in row:
                labels.append(btn["action"]["label"])
        self.assertIn("▶ sess2", labels)
        self.assertIn("sess1", labels)
        self.assertIn("sess3", labels)
        self.assertIn("➕ Новая сессия", labels)
        self.assertIn("🗑 Удалить...", labels)

    def test_sessions_keyboard_empty(self):
        """Клавиатура без сессий."""
        kb = make_sessions_keyboard([], None)
        labels = []
        for row in kb["buttons"]:
            for btn in row:
                labels.append(btn["action"]["label"])
        self.assertIn("➕ Новая сессия", labels)
        # Для пустого списка не должно быть кнопки удаления
        self.assertNotIn("🗑 Удалить...", labels)

    def test_kill_keyboard(self):
        """Клавиатура удаления."""
        kb = make_kill_keyboard(["a", "b"])
        labels = []
        for row in kb["buttons"]:
            for btn in row:
                labels.append(btn["action"]["label"])
        self.assertIn("🗑 a", labels)
        self.assertIn("🗑 b", labels)
        self.assertFalse(kb["one_time"])  # консистентно с остальными меню

    def test_watch_keyboard(self):
        """Клавиатура watch mode."""
        kb = make_watch_keyboard()
        labels = []
        for row in kb["buttons"]:
            for btn in row:
                labels.append(btn["action"]["label"])
        self.assertIn("⛔ Ctrl+C", labels)
        self.assertIn("🛑 Стоп", labels)
        self.assertIn("🔄 Обновить", labels)

    def test_notify_keyboard(self):
        """Клавиатура уведомлений."""
        kb_on = make_notify_keyboard(True)
        labels_on = []
        for row in kb_on["buttons"]:
            for btn in row:
                labels_on.append(btn["action"]["label"])
        self.assertIn("🔔 ВКЛ", labels_on)

        kb_off = make_notify_keyboard(False)
        labels_off = []
        for row in kb_off["buttons"]:
            for btn in row:
                labels_off.append(btn["action"]["label"])
        self.assertIn("🔕 ВЫКЛ", labels_off)


class TestVkApiErrors(unittest.TestCase):
    """Тесты обработки ошибок VK API."""

    def test_invalid_token(self):
        """Невалидный токен должен вернуть ошибку."""
        vk = VkApi("invalid_token_12345")
        ok, info = vk.validate_token()
        self.assertFalse(ok, f"Невалидный токен должен вернуть ok=False, получили: {info}")
        self.assertTrue("❌" in info or "error" in info.lower() or "ошибка" in info.lower(),
                       f"Должна быть ошибка, получили: {info}")

    def test_invalid_method_call(self):
        """Вызов с невалидным токеном должен выбросить VkApiError."""
        vk = VkApi("invalid_token_12345")
        with self.assertRaises(VkApiError):
            vk.send_message(123, "test")

    def test_vk_api_error_repr(self):
        """Представление ошибки VkApiError."""
        err = VkApiError(5, "User authorization failed")
        self.assertEqual(err.code, 5)
        self.assertIn("5", str(err))
        self.assertIn("User authorization failed", str(err))


# ── Тесты state_manager ──────────────────────────────────────────

class TestStateManager(unittest.TestCase):
    """Тесты сохранения/загрузки состояния."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        # Подменяем CONFIG_DIR
        import vkbot.state_manager as sm
        sm.CONFIG_DIR = self.tmp_dir
        sm.STATE_FILE = os.path.join(self.tmp_dir, "state.json")
        # Также подменяем в конфиге
        import vkbot.config as cfg
        cfg.CONFIG_DIR = self.tmp_dir
        cfg.CONFIG_FILE = os.path.join(self.tmp_dir, "config.json")

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)
        # Восстанавливаем пути
        import vkbot.state_manager as sm
        sm.CONFIG_DIR = os.path.expanduser("~/.vk-tmux-bot")
        sm.STATE_FILE = os.path.join(sm.CONFIG_DIR, "state.json")
        import vkbot.config as cfg
        cfg.CONFIG_DIR = os.path.expanduser("~/.vk-tmux-bot")
        cfg.CONFIG_FILE = os.path.join(cfg.CONFIG_DIR, "config.json")

    def test_save_load_state(self):
        """Сохранение и загрузка состояния."""
        current = {123: "session_a", 456: "session_b"}
        watching = {
            123: {"session": "session_a", "message_id": 100, "peer_id": 200},
        }
        save_state(current, watching)

        loaded_cur, loaded_watch = load_state()
        self.assertEqual(loaded_cur, current)
        self.assertEqual(len(loaded_watch), 1)
        self.assertEqual(loaded_watch[123]["session"], "session_a")
        self.assertEqual(loaded_watch[123]["stop"], False)  # stop сбрасывается

    def test_load_empty_state(self):
        """Загрузка несуществующего состояния."""
        cur, watch = load_state()
        self.assertEqual(cur, {})
        self.assertEqual(watch, {})

    def test_save_load_unicode_session_name(self):
        """Сохранение с unicode в названиях."""
        current = {1: "mytest"}
        save_state(current, {})
        loaded, _ = load_state()
        self.assertEqual(loaded[1], "mytest")


# ── Тесты форматирования вывода ──────────────────────────────────

class TestTelegramProxy(unittest.TestCase):
    """Тесты Telegram-прокси (транслит-поиск, сортировка чатов)."""

    def test_translit_basic(self):
        """Транслитерация кириллица → латиница."""
        from vkbot.bot import _translit
        self.assertEqual(_translit("артем"), "artem")
        self.assertEqual(_translit("егор"), "egor")
        self.assertEqual(_translit("саша"), "sasha")
        self.assertEqual(_translit("Игорь"), "igor")

    def test_translit_latin_passthrough(self):
        """Латиница остаётся латиницей (в нижнем регистре)."""
        from vkbot.bot import _translit
        self.assertEqual(_translit("Artem"), "artem")
        self.assertEqual(_translit("Sasha O"), "sasha o")

    def test_search_matches_cyrillic_query_latin_name(self):
        """Поиск: кириллический запрос находит латинское имя."""
        from vkbot.bot import _translit
        # Имитируем логику поиска
        name = "Artem Orlovich"
        query = "артем"
        matched = query.lower() in name.lower() or _translit(query) in _translit(name)
        self.assertTrue(matched)

    def test_search_matches_latin_query_cyrillic_name(self):
        """Поиск: латинский запрос находит кириллическое имя."""
        from vkbot.bot import _translit
        name = "Игорь Алферов"
        query = "igor"
        matched = query.lower() in name.lower() or _translit(query) in _translit(name)
        self.assertTrue(matched)

    def test_chat_sort_order(self):
        """Сортировка: личные → группы → каналы."""
        from vkbot.bot import VkTmuxBot
        bot = VkTmuxBot()
        # (name, chat_id, unread, preview, kind)
        dialogs = [
            ("Канал", 1, 0, "", "channel"),
            ("Группа", 2, 0, "", "group"),
            ("Человек", 3, 0, "", "user"),
        ]
        sorted_chats = bot._tg_sort_chats(dialogs)
        kinds = [c[4] for c in sorted_chats]
        self.assertEqual(kinds, ["user", "group", "channel"])


class TestOutputFormatting(unittest.TestCase):
    """Тесты форматирования вывода для VK."""

    def test_format_output_basic(self):
        """Базовое форматирование."""
        from vkbot.bot import format_output
        result = format_output("test", "line1\nline2")
        self.assertIn("📺 test", result)
        self.assertIn("line1", result)
        self.assertIn("line2", result)
        self.assertIn("━" * 22, result)

    def test_format_output_empty(self):
        """Форматирование пустого вывода."""
        from vkbot.bot import format_output
        result = format_output("empty", "")
        self.assertIn("пусто", result)

    def test_clean_pane_strips_trailing(self):
        """Хвостовые пробелы (padding tmux) убираются."""
        from vkbot.bot import _clean_pane
        out = _clean_pane("hello     \nworld   \n\n\n\nend")
        # нет строк с хвостовыми пробелами
        for ln in out.split("\n"):
            self.assertEqual(ln, ln.rstrip())
        # 3+ пустых схлопнуты
        self.assertNotIn("\n\n\n", out)

    def test_format_output_truncation(self):
        """Обрезание длинного вывода."""
        from vkbot.bot import format_output
        long_text = "x" * 4000
        result = format_output("big", long_text, max_len=100)
        self.assertLess(len(result), 250)  # должно быть обрезано
        self.assertIn("обрезано", result)

    def test_format_output_with_state(self):
        """Форматирование с определением состояния."""
        from vkbot.bot import format_output
        result = format_output("err", "Error: bad\nuser@host:~$ ")
        self.assertIn("🚨", result)


# ── Тесты конфигурации ──────────────────────────────────────────

class TestConfig(unittest.TestCase):
    """Тесты конфигурации."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        import vkbot.config as cfg
        cfg.CONFIG_DIR = self.tmp_dir
        cfg.CONFIG_YAML = os.path.join(self.tmp_dir, "config.yaml")
        cfg.CONFIG_JSON = os.path.join(self.tmp_dir, "config.json")

    def tearDown(self):
        import shutil
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)
        import vkbot.config as cfg
        cfg.CONFIG_DIR = os.path.expanduser("~/.vk-tmux-bot")
        cfg.CONFIG_YAML = os.path.join(cfg.CONFIG_DIR, "config.yaml")
        cfg.CONFIG_JSON = os.path.join(cfg.CONFIG_DIR, "config.json")

    def test_save_load_config(self):
        """Сохранение и загрузка конфига (YAML)."""
        config = {
            "vk": {
                "group_token": "test_token_123",
                "group_id": 123456,
                "admin_ids": [111],
                "allowed_user_ids": [111, 222],
            },
            "tmux": {"output_lines": 100, "watch_interval": 5.0},
            "bot": {"rate_limit_delay": 1.0, "long_poll_wait": 10},
        }
        save_config(config)
        loaded = load_config()
        self.assertEqual(loaded["vk"]["group_token"], "test_token_123")
        self.assertEqual(loaded["vk"]["group_id"], 123456)
        self.assertEqual(loaded["vk"]["allowed_user_ids"], [111, 222])
        self.assertEqual(loaded["vk"]["admin_ids"], [111])
        self.assertEqual(loaded["tmux"]["output_lines"], 100)
        self.assertEqual(loaded["bot"]["rate_limit_delay"], 1.0)
        # Дефолты подмешиваются
        self.assertIn("claude", loaded)

    def test_admin_defaults_to_first_allowed(self):
        """Если admin_ids пуст — админом становится первый allowed."""
        config = {
            "vk": {"group_token": "t", "group_id": 1, "allowed_user_ids": [777, 888]},
        }
        save_config(config)
        loaded = load_config()
        self.assertEqual(loaded["vk"]["admin_ids"], [777])

    def test_load_missing_config(self):
        """Загрузка несуществующего конфига возвращает None."""
        import vkbot.config as cfg
        for p in (cfg.CONFIG_YAML, cfg.CONFIG_JSON):
            if os.path.exists(p):
                os.remove(p)
        result = load_config()
        self.assertIsNone(result)


class TestTgBot(unittest.TestCase):
    """Тесты Telegram-бота (общие хелперы, рендер, клавиатуры)."""

    def test_ikb_structure(self):
        from tgbot.api import ikb
        kb = ikb([[("A", "a"), ("B", "b")], [("C", "c")]])
        self.assertIn("inline_keyboard", kb)
        self.assertEqual(len(kb["inline_keyboard"]), 2)
        self.assertEqual(kb["inline_keyboard"][0][0]["text"], "A")
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "a")

    def test_pre_block_escapes(self):
        from tgbot.api import pre_block
        r = pre_block("a < b & c > d")
        self.assertTrue(r.startswith("<pre>") and r.endswith("</pre>"))
        self.assertIn("&lt;", r)
        self.assertIn("&amp;", r)
        self.assertNotIn("< b", r)  # экранировано

    def test_fmt_stream_monospace(self):
        from tgbot.bot import fmt_stream
        r = fmt_stream("mysess", "line1\nline2   \n\n\n\nline3")
        self.assertIn("mysess", r)
        self.assertIn("<pre>", r)
        # хвостовые пробелы убраны
        self.assertNotIn("line2   ", r)

    def test_fmt_stream_empty(self):
        from tgbot.bot import fmt_stream
        r = fmt_stream("s", "")
        self.assertIn("пусто", r)

    def test_key_map(self):
        from tgbot.bot import KEY_MAP
        self.assertEqual(KEY_MAP["up"], "Up")
        self.assertEqual(KEY_MAP["btab"], "BTab")
        self.assertEqual(KEY_MAP["esc"], "Escape")


# ── Запуск ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Запуск тестов VK Tmux Bot")
    print("=" * 60)
    print()

    # Проверяем tmux
    if has_tmux():
        print("✅ tmux доступен — интеграционные тесты будут запущены")
        sessions = list_sessions()
        print(f"   Активных сессий: {len(sessions)}")
    else:
        print("⚠️ tmux не установлен — интеграционные тесты будут пропущены")

    print()

    # Запускаем тесты
    unittest.main(verbosity=2)
