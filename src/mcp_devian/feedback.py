"""Её отзывы о помощнике. Лежат только у неё на маке, никуда сами не отправляются."""

import json
import os
import tempfile
import threading
import time
from datetime import datetime

from . import __version__
from .config import DATA_DIR

FEEDBACK_FILE = DATA_DIR / "feedback.jsonl"
# Спрашивать мягко и редко: не чаще раза в несколько дней.
ASK_EVERY_S = 3 * 86400
_LOCK = threading.Lock()


def add(text: str, topic: str = "общее", rating: int | None = None, now: float | None = None) -> int:
    entry = {"ts": now or time.time(), "version": __version__, "topic": topic.strip() or "общее",
             "rating": rating, "text": text.strip()}
    with _LOCK:
        FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return len(entries())


def entries() -> list[dict]:
    try:
        lines = FEEDBACK_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def should_ask(now: float | None = None, configured_since: float | None = None) -> bool:
    """Пора ли мягко спросить, как ей помощник."""
    now = now or time.time()
    if configured_since and now - configured_since < 86400:
        return False  # первый день — пусть просто попользуется
    last = max((e.get("ts", 0) for e in entries()), default=0)
    return now - last >= ASK_EVERY_S


def mark_asked(now: float | None = None) -> None:
    """Спросили, а она не захотела отвечать — тоже считается, чтобы не спрашивать снова завтра."""
    add("", topic="спросили, без ответа", now=now)


def report(summary: str = "") -> str:
    items = [e for e in entries() if e.get("text")]
    if not items:
        return "Отзывов пока нет."
    lines = [f"Отзывы о помощнике по адоптам (версия {__version__}), всего {len(items)}:", ""]
    for e in reversed(items):
        when = datetime.fromtimestamp(e.get("ts", 0)).strftime("%d.%m.%Y")
        rating = f" · оценка {e['rating']}/5" if e.get("rating") else ""
        lines.append(f"— {when} · {e.get('topic', 'общее')}{rating} (v{e.get('version', '?')})")
        lines.append(f"  {e['text']}")
    if summary:
        lines += ["", summary]
    return "\n".join(lines)


def clear_reported() -> int:
    """После отправки: убрать отзывы из файла (копия остаётся рядом)."""
    with _LOCK:
        if not FEEDBACK_FILE.exists():
            return 0
        n = len([e for e in entries() if e.get("text")])
        backup = FEEDBACK_FILE.with_name("feedback.sent.jsonl")
        with open(backup, "a", encoding="utf-8") as out:
            out.write(FEEDBACK_FILE.read_text(encoding="utf-8"))
        fd, tmp = tempfile.mkstemp(dir=FEEDBACK_FILE.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # Метка времени остаётся, чтобы не спрашивать снова сразу после отправки.
            fh.write(json.dumps({"ts": time.time(), "version": __version__, "topic": "отправлено", "text": ""}) + "\n")
        os.replace(tmp, FEEDBACK_FILE)
        return n
