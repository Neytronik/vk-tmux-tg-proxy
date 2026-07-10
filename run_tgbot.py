#!/home/vkbot/.venv/bin/python3
"""Запуск Telegram-бота управления сервером/Claude.

  python3 run_tgbot.py start
"""
import sys
import os
import signal

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "start":
        print("Telegram Tmux/Claude Bot")
        print("  python3 run_tgbot.py start")
        sys.exit(0)

    import subprocess
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True, timeout=5)
    except Exception:
        print("❌ tmux не установлен: sudo apt install tmux")
        sys.exit(1)

    from tgbot.bot import TgTmuxBot
    bot = TgTmuxBot()

    def sig(s, f):
        bot.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, sig)
    signal.signal(signal.SIGTERM, sig)

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


if __name__ == "__main__":
    main()
