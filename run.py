#!/home/vkbot/.venv/bin/python3
"""CLI для управления VK Tmux Bot.

Команды:
  init       — настроить бота (создать конфиг)
  start      — запустить бота
  status     — показать статус
  config     — показать путь к конфигу
"""

import sys
import os

# Добавляем директорию проекта в путь — чтобы импорты работали откуда угодно
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import json
import signal

from vkbot.config import (
    load_config,
    save_config,
    get_config_path,
    DEFAULT_CONFIG,
    CONFIG_DIR,
)


def cmd_init():
    """Интерактивная настройка."""
    print("╔══════════════════════════════════════════╗")
    print("║     🤖 VK Tmux Bot — Настройка          ║")
    print("╚══════════════════════════════════════════╝")
    print()
    print("Для работы нужен токен группы ВКонтакте.")
    print("Как получить:")
    print("  1. Создайте группу ВК (или используйте существующую)")
    print("  2. Управление → Настройки → Работа с API")
    print("  3. Создайте ключ доступа с правами: сообщения (разрешённые)")
    print("  4. Включите Long Poll API: Управление → Настройки → Long Poll API")
    print("     (Тип событий: входящие сообщения)")
    print()

    # Токен
    token = input("Токен группы: ").strip()
    while not token:
        print("⚠️ Токен обязателен")
        token = input("Токен группы: ").strip()

    # Group ID
    group_id_str = input("ID группы (цифры): ").strip()
    while not group_id_str.isdigit():
        print("⚠️ Нужно ввести число (например, 123456789)")
        group_id_str = input("ID группы: ").strip()
    group_id = int(group_id_str)

    # Разрешённые пользователи
    print()
    print("Введите VK ID пользователей, которым разрешён доступ (через запятую).")
    print("Например: 123456789, 987654321")
    print("Оставьте пустым — доступ будет открыт всем, кто напишет боту.")
    users_str = input("Разрешённые ID: ").strip()
    allowed = []
    if users_str:
        for uid in users_str.split(","):
            uid = uid.strip()
            if uid.isdigit():
                allowed.append(int(uid))

    config = {
        "vk": {
            "group_token": token,
            "group_id": group_id,
            "allowed_user_ids": allowed,
        },
        "tmux": DEFAULT_CONFIG["tmux"],
        "bot": DEFAULT_CONFIG["bot"],
    }

    save_config(config)
    print()
    print("=" * 50)
    print("✅ Конфигурация сохранена!")
    print(f"   Файл: {get_config_path()}")
    print()
    print("📋 Следующий шаг: запустите бота командой:")
    print("   python3 run.py start")
    print("=" * 50)


def cmd_start():
    """Запустить бота."""
    config = load_config()
    if not config:
        print("❌ Сначала настройте бота: python3 run.py init")
        sys.exit(1)

    # Проверяем tmux
    import subprocess
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True, timeout=5)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("❌ tmux не установлен.")
        print("   Установите: sudo apt install tmux")
        sys.exit(1)

    from vkbot.bot import VkTmuxBot

    bot = VkTmuxBot()

    # Обработка сигналов для чистого выхода
    def signal_handler(sig, frame):
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        bot.stop()
        sys.exit(1)


def cmd_status():
    """Показать статус."""
    print("📊 VK Tmux Bot — Статус")
    print()

    # Проверка конфига
    config = load_config()
    if config:
        print(f"✅ Конфиг: {get_config_path()}")
        print(f"   Группа: {config['vk']['group_id']}")
        print(f"   Пользователей в белом списке: {len(config['vk']['allowed_user_ids'])}")
    else:
        print(f"❌ Конфиг не настроен. Используйте: python3 run.py init")

    # Проверка tmux
    import subprocess
    import os
    sock = f"/tmp/tmux-{os.getuid()}/default"
    try:
        result = subprocess.run(
            ["tmux", "-S", sock, "list-sessions"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            sessions = [s for s in result.stdout.split("\n") if s.strip()]
            print(f"✅ tmux запущен — {len(sessions)} сессий")
            for s in sessions:
                print(f"   • {s.split(':')[0]}")
        else:
            print("⚠️ tmux установлен, но нет активных сессий (или сервер не запущен)")
    except FileNotFoundError:
        print("❌ tmux не установлен")
    except Exception:
        print("⚠️ Не удалось проверить tmux")

    # Состояние
    from vkbot.state_manager import load_state
    cur, watch = load_state()
    print(f"📌 Сохранённых подключений: {len(cur)}")
    print(f"👁 Сохранённых watch-сессий: {len(watch)}")


def cmd_config():
    """Показать путь к конфигу."""
    print(f"📁 Конфигурация: {get_config_path()}")
    config = load_config()
    if config:
        # Показываем без токена
        safe = json.loads(json.dumps(config))
        safe["vk"]["group_token"] = safe["vk"]["group_token"][:15] + "..." if safe["vk"]["group_token"] else "(пусто)"
        print(json.dumps(safe, indent=2, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print("VK Tmux Bot — управление tmux через ВКонтакте")
        print()
        print("Команды:")
        print("  init     — настроить бота")
        print("  start    — запустить бота")
        print("  status   — показать статус")
        print("  config   — показать конфигурацию")
        print()
        print("Пример:")
        print("  python3 run.py init")
        print("  python3 run.py start")
        sys.exit(0)

    command = sys.argv[1]

    if command == "init":
        cmd_init()
    elif command == "start":
        cmd_start()
    elif command == "status":
        cmd_status()
    elif command == "config":
        cmd_config()
    else:
        print(f"❌ Неизвестная команда: {command}")
        print("Доступные: init, start, status, config")
        sys.exit(1)


if __name__ == "__main__":
    main()
