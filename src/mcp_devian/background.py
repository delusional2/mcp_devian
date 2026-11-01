"""Фоновая проверка через launchd: включается и выключается прямо из чата."""

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR

LABEL = "com.mcp-devian.monitor"
PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"
LOG_DIR = DATA_DIR / "logs"
# Свой ключ в plist (launchd его игнорирует): каким Python задача запускается.
PYTHON_KEY = "McpDevianPython"


def command(python: str | None = None) -> list[str]:
    # Тот же Python, в котором работает сервер: в нём уже стоят все зависимости.
    # Claude Desktop стирает папку расширения при обновлении и удалении, поэтому
    # без Python задача тихо выходит, а не падает каждые 15 минут.
    py = shlex.quote(python or sys.executable)
    return ["/bin/sh", "-c", f"[ -x {py} ] || exit 0; exec {py} -m mcp_devian check --scheduled"]


def environment() -> dict[str, str]:
    env = {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin", "PYTHONUNBUFFERED": "1"}
    for key in ("MCP_DEVIAN_DATA", "MCP_DEVIAN_SETTINGS"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def is_enabled() -> bool:
    return PLIST.exists()


def _plist(minutes: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": command(),
        "WorkingDirectory": str(DATA_DIR),
        "StartInterval": minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / "monitor.log"),
        "StandardErrorPath": str(LOG_DIR / "monitor.err.log"),
        "EnvironmentVariables": environment(),
        PYTHON_KEY: sys.executable,
    }


def enable(minutes: int = 15) -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    # Если агент ещё не загружен, unload ругается — это нормально.
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True, check=False)
    PLIST.write_bytes(plistlib.dumps(_plist(minutes)))
    done = subprocess.run(["launchctl", "load", str(PLIST)], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"launchctl load: {done.stderr.strip() or done.returncode}")
    return f"Фоновая проверка включена: раз в {minutes} мин, пока мак не спит. Логи: {LOG_DIR}"


def refresh() -> bool:
    """При старте сервера: если задача включена, но указывает на старый Python, переписать её."""
    if not PLIST.exists():
        return False
    try:
        current = plistlib.loads(PLIST.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return False
    if current.get("ProgramArguments") == command():
        return False
    # Переписываем, только если записанного Python больше нет (расширение обновили).
    # Иначе другой экземпляр (например, разработческий) и расширение отбирали бы задачу друг у друга.
    recorded = current.get(PYTHON_KEY)
    if recorded and Path(recorded).exists():
        return False
    enable(max(15, int(current.get("StartInterval", 900)) // 60))
    return True


def disable() -> str:
    if not PLIST.exists():
        return "Фоновая проверка и так выключена."
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True, check=False)
    PLIST.unlink()
    return "Фоновая проверка выключена."
