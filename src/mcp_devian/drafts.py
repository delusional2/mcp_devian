"""Черновики публикаций и журнал отправок.

Правило одно: на сайт уходит только черновик, который ей показали целиком и на который
она ответила «го». Отправка переводит черновик в sending под замком, поэтому двойной
вызов не отправит одно и то же дважды.
"""

import json
import sqlite3
import threading
import time
import uuid

# Черновик живёт сутки: вчерашнее «го» к сегодняшнему треду не относится.
DRAFT_TTL_S = 24 * 3600
_LOCK = threading.Lock()


class DraftError(Exception):
    pass


def create(conn: sqlite3.Connection, kind: str, payload: dict, preview: str, now: float | None = None) -> str:
    draft_id = uuid.uuid4().hex[:8]
    conn.execute("INSERT INTO drafts (id, kind, status, created_ts, payload, preview) VALUES (?, ?, 'pending', ?, ?, ?)",
                 (draft_id, kind, now or time.time(), json.dumps(payload, ensure_ascii=False), preview))
    conn.commit()
    return draft_id


def get(conn: sqlite3.Connection, draft_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id.strip(),)).fetchone()


def take_for_sending(conn: sqlite3.Connection, draft_id: str, kind: str, now: float | None = None) -> dict:
    """Забрать черновик на отправку. Бросает DraftError, если его нельзя отправлять."""
    now = now or time.time()
    with _LOCK:
        row = get(conn, draft_id)
        if not row:
            raise DraftError("Такого черновика нет. Сначала подготовь его и покажи ей.")
        if row["kind"] != kind:
            raise DraftError("Это черновик другого типа.")
        if row["status"] != "pending":
            raise DraftError(f"Этот черновик уже {STATUS_NAMES.get(row['status'], row['status'])}.")
        if now - row["created_ts"] > DRAFT_TTL_S:
            conn.execute("UPDATE drafts SET status = 'cancelled', result = 'устарел' WHERE id = ?", (row["id"],))
            conn.commit()
            raise DraftError("Черновик устарел (старше суток). Подготовь новый и покажи ей заново.")
        conn.execute("UPDATE drafts SET status = 'sending' WHERE id = ? AND status = 'pending'", (row["id"],))
        conn.commit()
        return json.loads(row["payload"])


def finish(conn: sqlite3.Connection, draft_id: str, ok: bool, result: str, now: float | None = None) -> None:
    conn.execute("UPDATE drafts SET status = ?, result = ?, sent_ts = ? WHERE id = ?",
                 ("sent" if ok else "failed", result, now or time.time(), draft_id))
    conn.commit()


def cancel(conn: sqlite3.Connection, draft_id: str) -> bool:
    cur = conn.execute("UPDATE drafts SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (draft_id,))
    conn.commit()
    return cur.rowcount > 0


def log_sent(conn: sqlite3.Connection, kind: str, target: str, ref: str | None, ok: bool, detail: str = "",
             now: float | None = None) -> None:
    conn.execute("INSERT INTO sent_log (kind, target, ref, ts, ok, detail) VALUES (?, ?, ?, ?, ?, ?)",
                 (kind, target, ref, now or time.time(), int(ok), detail[:500]))
    conn.commit()


def count_sent(conn: sqlite3.Connection, kind: str, since: float, target: str | None = None,
               ref: str | None = None) -> int:
    sql, params = "SELECT COUNT(*) FROM sent_log WHERE kind = ? AND ok = 1 AND ts >= ?", [kind, since]
    if target is not None:
        sql += " AND target = ?"
        params.append(target)
    if ref is not None:
        sql += " AND ref = ?"
        params.append(ref)
    return conn.execute(sql, params).fetchone()[0]


def last_sent(conn: sqlite3.Connection, kind: str) -> float | None:
    row = conn.execute("SELECT MAX(ts) FROM sent_log WHERE kind = ? AND ok = 1", (kind,)).fetchone()
    return row[0] if row else None


STATUS_NAMES = {"sending": "отправляется", "sent": "отправлен", "failed": "не отправился",
                "cancelled": "отменён", "pending": "ждёт её «го»"}
