"""Конфигурация бота.

Поддерживает YAML (предпочтительно) и JSON (обратная совместимость).
Боевой конфиг лежит в ~/.vk-tmux-bot/ и НЕ попадает в репозиторий.
Пример для новых пользователей — config.example.yaml в корне проекта.
"""
import os
import json
import stat

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# Значения по умолчанию — то, что не задано в конфиге, берётся отсюда.
DEFAULT_CONFIG = {
    "vk": {
        "group_token": "",
        "group_id": 0,
        # admin_ids — кто может управлять tmux/Claude и админкой.
        # Если пусто — админом становится первый из allowed_user_ids.
        "admin_ids": [],
        # allowed_user_ids — кому вообще разрешён бот (Telegram-прокси).
        "allowed_user_ids": [],
    },
    "tmux": {
        "output_lines": 60,          # строк вывода в /o
        "watch_interval": 2.0,       # интервал автообновления (сек)
        "work_dir": "~",             # рабочая директория для новых сессий
        "idle_notify_minutes": 10,   # тишина N минут → уведомление «сессия остановилась»
        "term_width": 62,            # ширина терминала — узкая, чтобы TUI Claude
        "term_height": 40,           # помещался в чат и не «ехал»
        # Быстрые команды — кнопки для частых действий в терминальной сессии
        "quick_commands": ["ls -la", "git status", "clear", "htop", "pwd", "df -h"],
    },
    "claude": {
        "command": "claude",         # команда запуска Claude Code
        "deepclaude_command": "dcc", # команда для кнопки DCC (deepclaude)
    },
    "bot": {
        "rate_limit_delay": 0.4,     # задержка между сообщениями (сек)
        "long_poll_wait": 25,        # таймаут long poll (сек)
    },
    "telegram": {
        "api_id": 0,                 # из https://my.telegram.org
        "api_hash": "",              # из https://my.telegram.org
        "session_file": "~/.vk-tmux-bot/tg_session",  # префикс файлов сессий Telethon
    },
    # Telegram-БОТ (управление сервером/Claude из Telegram) — работает независимо от VK.
    "tgbot": {
        "enabled": False,            # включить Telegram-бота
        "bot_token": "",             # токен от @BotFather
        "admin_ids": [],             # кто управляет сервером (первый — админ)
        "allowed_user_ids": [],      # кому разрешён бот
    },
}

CONFIG_DIR = os.path.expanduser("~/.vk-tmux-bot")
CONFIG_YAML = os.path.join(CONFIG_DIR, "config.yaml")
CONFIG_JSON = os.path.join(CONFIG_DIR, "config.json")


def ensure_config_dir():
    """Создать директорию конфига с правильными правами."""
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, mode=0o700)


def _deep_merge(base, override):
    """Рекурсивно наложить override на base (для дефолтов)."""
    result = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _read_raw():
    """Прочитать сырой конфиг из YAML или JSON. Возвращает (dict|None, path)."""
    if _HAS_YAML and os.path.exists(CONFIG_YAML):
        with open(CONFIG_YAML, "r") as f:
            return yaml.safe_load(f) or {}, CONFIG_YAML
    if os.path.exists(CONFIG_JSON):
        with open(CONFIG_JSON, "r") as f:
            return json.load(f), CONFIG_JSON
    return None, None


def load_config():
    """Загрузить и провалидировать конфигурацию. Возвращает dict или None."""
    ensure_config_dir()

    raw, path = _read_raw()
    if raw is None:
        # Создаём пустой YAML-шаблон
        save_config(DEFAULT_CONFIG)
        print(f"⚙️  Создан файл конфигурации: {CONFIG_YAML}")
        print("   Заполните vk.group_token, vk.group_id и vk.allowed_user_ids")
        return None

    # Накладываем на дефолты, чтобы новые поля всегда были
    config = _deep_merge(DEFAULT_CONFIG, raw)

    if not config.get("vk", {}).get("group_token"):
        print("❌ Не указан vk.group_token в конфиге")
        return None
    if not config.get("vk", {}).get("group_id"):
        print("❌ Не указан vk.group_id в конфиге")
        return None

    # Если admin_ids не заданы — админом становится первый из allowed (с предупреждением)
    if not config["vk"].get("admin_ids"):
        allowed = config["vk"].get("allowed_user_ids", [])
        config["vk"]["admin_ids"] = [allowed[0]] if allowed else []
        if allowed:
            print(f"⚠️  admin_ids не задан — админом назначен первый в allowed: {allowed[0]}")
            print("   Чтобы задать явно, добавьте vk.admin_ids в config.yaml")

    return config


def save_config(config):
    """Сохранить конфигурацию (в YAML если доступен, иначе JSON)."""
    ensure_config_dir()
    if _HAS_YAML:
        with open(CONFIG_YAML, "w") as f:
            yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
        os.chmod(CONFIG_YAML, stat.S_IRUSR | stat.S_IWUSR)
    else:
        with open(CONFIG_JSON, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        os.chmod(CONFIG_JSON, stat.S_IRUSR | stat.S_IWUSR)


def get_config_path():
    """Путь к активному файлу конфигурации."""
    _, path = _read_raw()
    return path or (CONFIG_YAML if _HAS_YAML else CONFIG_JSON)


# ── Динамические пользователи (админ добавляет из бота) ──────────

USERS_FILE = os.path.join(CONFIG_DIR, "users.json")


def load_users():
    """Загрузить пользователей, добавленных админом в рантайме.
    Возвращает {user_id(int): {"tmux": bool, "name": str}}."""
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except Exception:
        return {}


def save_users(users):
    """Сохранить динамических пользователей."""
    ensure_config_dir()
    try:
        with open(USERS_FILE, "w") as f:
            json.dump({str(k): v for k, v in users.items()}, f, indent=2, ensure_ascii=False)
        os.chmod(USERS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
