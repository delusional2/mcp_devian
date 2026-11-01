"""Проверка в отдельном процессе: Claude Desktop обрывает долгие вызовы инструментов.

check_now запускает `python -m mcp_devian check ... --job <id>`, ждёт до ~45 секунд
и, если не успело, просит Claude спросить check_status позже.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .config import DATA_DIR

JOB_FILE = DATA_DIR / "check-job.json"
STALE_AFTER_S = 15 * 60

# MCPServer выполняет параллельные вызовы в разных потоках: старт задачи — целиком под замком.
_LOCK = threading.RLock()


def _read(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or JOB_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict, path: Path | None = None) -> None:
    path = path or JOB_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".job-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        # Наш ребёнок, который уже завершился, висит зомби, и kill(0) его «видит»: сначала подбираем.
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done:
            return False
    except ChildProcessError:
        pass  # не наш процесс (сервер перезапускали) — проверяем обычным способом
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def current(now: float | None = None) -> dict:
    job = _read()
    now = now or time.time()
    starting = not job.get("pid") and now - job.get("started", 0) < 10
    dead = not starting and not _alive(job.get("pid"))
    if job.get("state") == "running" and (dead or now - job.get("started", 0) > STALE_AFTER_S):
        job.update(state="failed", lines=job.get("lines") or ["проверка оборвалась"], finished=now)
        _write(job)
    return job


def start(source: str, now: float | None = None) -> dict:
    """Запустить проверку, если никакая ещё не идёт. Возвращает состояние задачи (своей или уже идущей)."""
    with _LOCK:
        job = current(now)
        if job.get("state") == "running":
            return job
        job_id = uuid.uuid4().hex[:12]
        # Сначала пишем «идёт», потом запускаем: иначе быстрый процесс мог бы успеть
        # записать «готово» раньше нас, и мы бы затёрли его результат.
        job = {"id": job_id, "state": "running", "source": source, "pid": None,
               "started": now or time.time(), "lines": []}
        _write(job)
        logs = DATA_DIR / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "check-job.log", "a") as log_file:
            proc = subprocess.Popen([sys.executable, "-m", "mcp_devian", "check", source, "--job", job_id],
                                    stdout=log_file, stderr=log_file, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        latest = _read()
        if latest.get("id") == job_id and latest.get("state") == "running":
            latest["pid"] = proc.pid
            _write(latest)
        return latest


def finish(job_id: str | None, lines: list[str], ok: bool = True) -> bool:
    """Записать итог, только если файл всё ещё про эту задачу."""
    with _LOCK:
        job = _read()
        if job_id and job.get("id") != job_id:
            return False
        job.update(state="done" if ok else "failed", lines=lines, finished=time.time(), pid=None)
        _write(job)
        return True


def wait(timeout_s: float = 45, poll_s: float = 1.5, job_id: str | None = None) -> dict:
    deadline = time.time() + timeout_s
    job = current()
    while job.get("state") == "running" and time.time() < deadline:
        time.sleep(poll_s)
        job = current()
    if job_id and job.get("id") not in (job_id, None):
        return {"state": "failed", "lines": ["результат этой проверки перезаписан более новой"]}
    return job
