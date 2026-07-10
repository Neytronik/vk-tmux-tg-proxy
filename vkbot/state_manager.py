"""Сохранение и восстановление состояния бота.

Хранит:
- current_sessions: {user_id: session_name}
- watching_sessions: {user_id: {session, message_id, peer_id}}
"""
import json
import os
from .config import CONFIG_DIR


STATE_FILE = os.path.join(CONFIG_DIR, "state.json")


def save_state(current_sessions, watching_sessions):
    """Сохранить состояние в JSON."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)

    # Конвертируем ключи в строки (JSON не любит int-ключи)
    watch_dict = {}
    for uid, info in watching_sessions.items():
        watch_dict[str(uid)] = info

    state = {
        "current_sessions": {str(k): v for k, v in current_sessions.items()},
        "watching_sessions": watch_dict,
    }

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def load_state():
    """Загрузить состояние из JSON.
    Возвращает (current_sessions, watching_sessions).
    """
    if not os.path.exists(STATE_FILE):
        return {}, {}

    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}, {}

    # Конвертируем ключи обратно в int
    current = {}
    for k, v in state.get("current_sessions", {}).items():
        try:
            current[int(k)] = v
        except ValueError:
            current[k] = v

    watching = {}
    for k, v in state.get("watching_sessions", {}).items():
        try:
            watching[int(k)] = {**v, "stop": False}  # сбрасываем флаг остановки
        except ValueError:
            watching[k] = {**v, "stop": False}

    return current, watching
