"""Работа с tmux через subprocess.

Все функции зеркалируют логику tmux-telegram-control,
но написаны на Python с использованием subprocess.

Использует сокет текущего пользователя (/tmp/tmux-{UID}/default)
чтобы избежать конфликтов с root-сервером tmux.
"""
import os
import subprocess
import re

# Сокет tmux для текущего пользователя
_TMUX_SOCKET = os.environ.get(
    "TMUX_SOCKET",
    f"/tmp/tmux-{os.getuid()}/default"
)


def _ensure_socket_dir():
    """Создать каталог сокета tmux (пропадает после перезагрузки)."""
    sock_dir = os.path.dirname(_TMUX_SOCKET)
    try:
        os.makedirs(sock_dir, mode=0o700, exist_ok=True)
    except Exception:
        pass


def _tmux(*args):
    """Выполнить команду tmux и вернуть (success, output).

    Использует пользовательский сокет для изоляции от других tmux серверов.
    """
    _ensure_socket_dir()
    try:
        result = subprocess.run(
            ["tmux", "-S", _TMUX_SOCKET] + list(args),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout.rstrip("\n")
        else:
            return False, result.stderr or result.stdout or ""
    except FileNotFoundError:
        return False, "tmux не установлен"
    except subprocess.TimeoutExpired:
        return False, "Таймаут выполнения tmux"
    except Exception as e:
        return False, str(e)


def list_sessions():
    """Список всех tmux сессий."""
    ok, out = _tmux("list-sessions", "-F", "#{session_name}")
    if not ok:
        return []
    return [s.strip() for s in out.split("\n") if s.strip()]


def session_exists(name):
    """Проверить существование сессии."""
    return name in list_sessions()


def get_output(session_name, lines=60):
    """Получить последние N строк вывода панели."""
    ok, out = _tmux("capture-pane", "-p", "-t", session_name, "-S", f"-{lines}")
    if not ok:
        return f"❌ Не могу прочитать вывод: {out}"
    return _filter_separators(out)


def _filter_separators(output):
    """Отфильтровать строки-разделители (─── и т.п.)."""
    lines = output.split("\n")
    filtered = []
    for line in lines:
        stripped = re.sub(r"[─\-\s]", "", line)
        if stripped:
            filtered.append(line)
    return "\n".join(filtered) if filtered else output


def send_keys(session_name, text, press_enter=True):
    """Отправить клавиши в сессию."""
    args = ["send-keys", "-t", session_name]
    if text:
        args.append(text)
    if press_enter:
        args.append("Enter")
    ok, _ = _tmux(*args)
    return ok


def send_control_key(session_name, key):
    """Отправить контрольную клавишу (C-c, C-d и т.д.)."""
    ok, _ = _tmux("send-keys", "-t", session_name, key)
    return ok


def create_session(session_name, work_dir=None):
    """Создать новую tmux сессию в фоне."""
    args = ["new-session", "-d", "-s", session_name]
    if work_dir:
        args.extend(["-c", work_dir])
    ok, _ = _tmux(*args)
    if ok:
        return session_exists(session_name)
    return False


def kill_session(session_name):
    """Удалить tmux сессию."""
    ok, _ = _tmux("kill-session", "-t", session_name)
    return ok


def detect_errors(output):
    """Найти строки с ошибками в выводе."""
    error_patterns = [
        r"error:", r"error ", r"failed", r"failure", r"panic", r"fatal",
        r"exception", r"traceback", r"segmentation fault", r"core dumped",
        r"permission denied", r"cannot", r"unable to", r"not found",
        r"connection refused", r"timeout", r"killed",
        # Русские паттерны
        r"ошибка", r"не удалось", r"отказано", r"не найден",
    ]
    errors = []
    for line in output.split("\n"):
        line_lower = line.lower()
        for pat in error_patterns:
            if re.search(pat, line_lower):
                errors.append(line.strip())
                break
    return errors


def highlight_errors(output):
    """Подсветить строки с ошибками префиксом ❌."""
    error_patterns = [
        r"error:", r"failed", r"fatal", r"exception", r"traceback",
        r"ошибка", r"не удалось",
    ]
    lines = output.split("\n")
    result = []
    for line in lines:
        line_lower = line.lower()
        highlighted = False
        for pat in error_patterns:
            if re.search(pat, line_lower):
                result.append(f"❌ {line}")
                highlighted = True
                break
        if not highlighted:
            result.append(line)
    return "\n".join(result)


def detect_session_state(output):
    """Определить состояние сессии (ждёт ввода, выполняется, ошибка, простой)."""
    lines = output.split("\n")
    last_lines = "\n".join(lines[-10:]).lower()

    # Ждёт подтверждения (y/n)
    if re.search(r"(y/n|\(y/n\)|approve\?|proceed\?|continue\?|подтвердите|продолжить)", last_lines):
        return "prompt"

    # Идёт сборка/установка
    if re.search(r"(building|compiling|docker build|npm install|pip install|make|cmake|cargo build)", last_lines):
        return "build"

    # Нет приглашения командной строки — значит что-то выполняется
    if lines and not re.search(r"[$#>~]", lines[-1] if lines else ""):
        return "running"

    # Ошибка
    if re.search(r"(error|failed|fatal|ошибка)", last_lines):
        return "error"

    return "idle"
