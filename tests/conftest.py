import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Своя папка данных на весь прогон: тесты не должны писать в настоящую
# ~/Library/Application Support/mcp_devian. Ставится до импорта mcp_devian.
os.environ["MCP_DEVIAN_DATA"] = tempfile.mkdtemp(prefix="mcp-devian-tests-")
os.environ.pop("MCP_DEVIAN_SETTINGS", None)

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def no_real_osascript(monkeypatch):
    """Тесты не должны трогать настоящий Safari и Центр уведомлений."""
    real = subprocess.run

    def guarded(cmd, *args, **kwargs):
        if cmd and cmd[0] in ("osascript", "launchctl"):
            raise AssertionError(f"тест вызвал настоящий {cmd[0]}")
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """И настоящий Chrome бота тоже не запускаем: тесты подсовывают свой browser=."""
    from mcp_devian import toyhouse

    def refuse(*a, **k):
        raise AssertionError("тест попытался открыть настоящий Chrome бота")

    monkeypatch.setattr(toyhouse, "open_browser", refuse)


@pytest.fixture(autouse=True)
def no_real_spawns(monkeypatch):
    """Фоновые проверки и окно входа запускаются через Popen: в тестах — только подменённый."""
    real = subprocess.Popen

    def guarded(cmd, *args, **kwargs):
        # Так запускаются фоновые проверки и окно входа: [python, "-m", "mcp_devian", ...].
        if isinstance(cmd, (list, tuple)) and "mcp_devian" in cmd:
            raise AssertionError("тест попытался запустить настоящий процесс mcp_devian")
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)
