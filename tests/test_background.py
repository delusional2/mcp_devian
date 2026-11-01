import plistlib
import subprocess

import pytest

from mcp_devian import background


@pytest.fixture
def launchctl(tmp_path, monkeypatch):
    monkeypatch.setattr(background, "PLIST", tmp_path / "agent.plist")
    monkeypatch.setattr(background, "LOG_DIR", tmp_path / "logs")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(background.subprocess, "run", run)
    return calls


def test_command_survives_missing_python():
    cmd = background.command("/Users/x/Library/Application Support/Claude/Claude Extensions/e/.venv/bin/python")
    assert cmd[:2] == ["/bin/sh", "-c"]
    # Путь с пробелами в кавычках, без Python — тихий выход.
    assert "'/Users/x/Library/Application Support/" in cmd[2] and "|| exit 0" in cmd[2]
    done = subprocess.run(background.command("/nonexistent/python"), capture_output=True, check=False)
    assert done.returncode == 0


def test_enable_refresh_disable(launchctl, monkeypatch):
    assert "включена" in background.enable(20)
    plist = plistlib.loads(background.PLIST.read_bytes())
    assert plist["StartInterval"] == 1200 and plist["ProgramArguments"] == background.command()
    assert not background.refresh()  # путь тот же — ничего не трогаем

    # Другой живой экземпляр не отбирает задачу, пока записанный Python на месте.
    monkeypatch.setattr(background.sys, "executable", "/new/.venv/bin/python")
    assert not background.refresh()
    # Расширение обновили, старого Python больше нет — переписываем.
    old = plistlib.loads(background.PLIST.read_bytes())
    old[background.PYTHON_KEY] = "/gone/.venv/bin/python"
    background.PLIST.write_bytes(plistlib.dumps(old))
    assert background.refresh()
    plist = plistlib.loads(background.PLIST.read_bytes())
    assert "/new/.venv/bin/python" in plist["ProgramArguments"][2] and plist["StartInterval"] == 1200

    assert "выключена" in background.disable()
    assert not background.PLIST.exists() and not background.refresh()
