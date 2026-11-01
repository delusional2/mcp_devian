"""Черновики публикаций и журнал отправок.

Правило одно: на сайт уходит только черновик, который ей показали целиком и на который
она ответила «го». Черновик забирается на отправку атомарно (UPDATE ... WHERE status='pending'
в BEGIN IMMEDIATE), поэтому даже два процесса send-draft не отправят одно и то же дважды.

Пока черновик отправляется, в sent_ts лежит момент, когда его забрали (потом — время итога).
Если процесс умер посреди отправки, через STUCK_AFTER_S черновик считается «неизвестно, ушёл ли».

Её «го» с временем превращает черновик в запланированный (status 'scheduled', время в send_at):
его забирают так же атомарно, но не раньше send_at и не позже недели от создания.
"""

import json
import sqlite3
import time
import uuid

# Черновик живёт сутки: вчерашнее «го» к сегодняшнему треду не относится.
DRAFT_TTL_S = 24 * 3600
# Запланировать можно не дальше недели от черновика: дальше картинку и группы стоит показать заново.
SCHEDULE_MAX_AGE_S = 7 * 24 * 3600
# Если в назначенное время мак спал, публикация уходит при ближайшей проверке — но не позже суток после времени.
SCHEDULE_GRACE_S = 24 * 3600
# Самая долгая отправка (отклик в Chrome, загрузка картинки) укладывается в несколько минут.
STUCK_AFTER_S = 20 * 60
UNKNOWN_RESULT = "неизвестно, отправилось ли — проверь на сайте, прежде чем пробовать ещё раз"
EXPIRED_SCHEDULE = ("не ушёл: назначенное время прошло больше суток назад (мак спал или был выключен) — "
                    "подготовь черновик заново")
EXPIRED_PENDING = "устарел: «го» не было больше суток"
WAITING = ("pending", "scheduled")


class DraftError(Exception):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Колонка send_at появилась в 0.4: добавляем в старую базу, если её ещё нет."""
    if "send_at" in {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}:
        return
    try:
        conn.execute("ALTER TABLE drafts ADD COLUMN send_at REAL")
        conn.commit()
    except sqlite3.OperationalError:  # другой процесс успел добавить её раньше
        conn.rollback()
        if "send_at" not in {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}:
            raise


def create(conn: sqlite3.Connection, kind: str, payload: dict, preview: str, now: float | None = None) -> str:
    draft_id = uuid.uuid4().hex[:8]
    conn.execute("INSERT INTO drafts (id, kind, status, created_ts, payload, preview) VALUES (?, ?, 'pending', ?, ?, ?)",
                 (draft_id, kind, now or time.time(), json.dumps(payload, ensure_ascii=False), preview))
    conn.commit()
    return draft_id


def reap_stuck(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Отправка, которая висит дольше STUCK_AFTER_S, — процесс умер. Честно: «неизвестно, ушло ли»."""
    now = now or time.time()
    cur = conn.execute("UPDATE drafts SET status = 'unknown', result = ? WHERE status = 'sending' "
                       "AND COALESCE(sent_ts, created_ts) < ?", (UNKNOWN_RESULT, now - STUCK_AFTER_S))
    conn.commit()
    return cur.rowcount


def get(conn: sqlite3.Connection, draft_id: str, now: float | None = None) -> sqlite3.Row | None:
    ensure_schema(conn)
    reap_stuck(conn, now)
    return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id.strip(),)).fetchone()


def take_for_sending(conn: sqlite3.Connection, draft_id: str, kind: str, now: float | None = None) -> dict:
    """Забрать черновик на отправку. Бросает DraftError, если его нельзя отправлять.

    Забирает ровно один вызывающий: проверка и смена статуса идут в одной записывающей транзакции,
    а смена статуса проверяется по числу изменённых строк.
    """
    now = now or time.time()
    ensure_schema(conn)
    reap_stuck(conn, now)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id.strip(),)).fetchone()
        if not row:
            raise DraftError("Такого черновика нет. Сначала подготовь его и покажи ей.")
        if row["kind"] != kind:
            raise DraftError("Это черновик другого типа.")
        status = row["status"]
        if status not in WAITING:
            raise DraftError(f"Этот черновик уже {STATUS_NAMES.get(status, status)}.")
        if status == "scheduled":
            if _schedule_expired(row, now):
                conn.execute("UPDATE drafts SET status = 'cancelled', result = ? WHERE id = ?",
                             (EXPIRED_SCHEDULE, row["id"]))
                conn.commit()
                raise DraftError(f"Черновик {EXPIRED_SCHEDULE}.")
            if row["send_at"] is not None and row["send_at"] > now:
                raise DraftError("Этот черновик запланирован на потом — уйдёт сам в своё время.")
        elif now - row["created_ts"] > DRAFT_TTL_S:
            conn.execute("UPDATE drafts SET status = 'cancelled', result = ? WHERE id = ?", (EXPIRED_PENDING, row["id"]))
            conn.commit()
            raise DraftError("Черновик устарел (старше суток). Подготовь новый и покажи ей заново.")
        else:
            planned = json.loads(row["payload"]).get("planned_send_at")
            if planned and float(planned) > now:
                raise DraftError("Этот черновик она хотела выложить позже — его «го» оформляется через "
                                 "schedule_draft, тогда он уйдёт сам в своё время.")
        cur = conn.execute("UPDATE drafts SET status = 'sending', sent_ts = ? WHERE id = ? AND status = ?",
                           (now, row["id"], status))
        if cur.rowcount != 1:
            raise DraftError("Этот черновик уже отправляется.")
        conn.commit()
        return json.loads(row["payload"])
    finally:
        if conn.in_transaction:
            conn.rollback()


def finish(conn: sqlite3.Connection, draft_id: str, ok: bool, result: str, now: float | None = None,
           status: str | None = None) -> None:
    conn.execute("UPDATE drafts SET status = ?, result = ?, sent_ts = ? WHERE id = ?",
                 (status or ("sent" if ok else "failed"), result, now or time.time(), draft_id))
    conn.commit()


def fail_if_pending(conn: sqlite3.Connection, draft_id: str, result: str) -> bool:
    """Отправка не запустилась (процесс упал до того, как забрал черновик): закрыть его,
    чтобы позже он не ушёл сам собой без нового взгляда на черновик."""
    cur = conn.execute("UPDATE drafts SET status = 'failed', result = ? WHERE id = ? AND status = 'pending'",
                       (result, draft_id))
    conn.commit()
    return cur.rowcount > 0


def requeue(conn: sqlite3.Connection, draft_id: str, result: str) -> None:
    """Отправка даже не началась (например, её Chrome занят): черновик снова ждёт, его «го» в силе."""
    conn.execute("UPDATE drafts SET status = 'pending', result = ?, sent_ts = NULL WHERE id = ? AND status = 'sending'",
                 (result, draft_id))
    conn.commit()


def note_waiting(conn: sqlite3.Connection, draft_id: str, result: str) -> None:
    """Пояснение к черновику, который всё ещё ждёт (почему не ушёл прямо сейчас)."""
    conn.execute("UPDATE drafts SET result = ? WHERE id = ? AND status IN ('pending', 'scheduled')",
                 (result, draft_id))
    conn.commit()


def cancel(conn: sqlite3.Connection, draft_id: str) -> bool:
    cur = conn.execute("UPDATE drafts SET status = 'cancelled' WHERE id = ? AND status IN ('pending', 'scheduled')",
                       (draft_id,))
    conn.commit()
    return cur.rowcount > 0


# --- Расписание ---

def schedule(conn: sqlite3.Connection, draft_id: str, send_at: float, now: float | None = None) -> None:
    """Её «го» со временем: черновик уйдёт сам, когда придёт send_at. Можно перенести, пока не ушёл."""
    now = now or time.time()
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id.strip(),)).fetchone()
    if not row:
        raise DraftError("Такого черновика нет.")
    if row["status"] not in WAITING:
        raise DraftError(f"Этот черновик уже {STATUS_NAMES.get(row['status'], row['status'])}.")
    if row["status"] == "pending" and now - row["created_ts"] > DRAFT_TTL_S:
        raise DraftError("Черновик устарел (старше суток). Подготовь новый и покажи ей заново.")
    if send_at - row["created_ts"] > SCHEDULE_MAX_AGE_S:
        raise DraftError("Запланировать можно не дальше недели от черновика.")
    cur = conn.execute("UPDATE drafts SET status = 'scheduled', send_at = ? WHERE id = ? AND status = ?",
                       (max(send_at, now), row["id"], row["status"]))
    conn.commit()
    if cur.rowcount != 1:
        raise DraftError("Этот черновик как раз отправляется.")


def _schedule_expired(row, now: float) -> bool:
    send_at = row["send_at"] if row["send_at"] is not None else row["created_ts"]
    return now - send_at > SCHEDULE_GRACE_S


def expire_scheduled(conn: sqlite3.Connection, now: float | None = None) -> list[str]:
    """Запланированные, чьё время прошло больше суток назад (мак спал), не отправляем: отменяем с причиной."""
    now = now or time.time()
    ensure_schema(conn)
    ids = [r["id"] for r in conn.execute("SELECT id FROM drafts WHERE status = 'scheduled' "
                                         "AND COALESCE(send_at, created_ts) < ?", (now - SCHEDULE_GRACE_S,))]
    for draft_id in ids:
        conn.execute("UPDATE drafts SET status = 'cancelled', result = ? WHERE id = ? AND status = 'scheduled'",
                     (EXPIRED_SCHEDULE, draft_id))
    conn.commit()
    return ids


def expire_pending(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Черновики без «го» дольше суток закрываем: вчерашний черновик к сегодняшнему дню не относится."""
    now = now or time.time()
    cur = conn.execute("UPDATE drafts SET status = 'cancelled', result = ? WHERE status = 'pending' AND created_ts < ?",
                       (EXPIRED_PENDING, now - DRAFT_TTL_S))
    conn.commit()
    return cur.rowcount


def due(conn: sqlite3.Connection, now: float | None = None) -> list[sqlite3.Row]:
    """Запланированные, чьё время пришло: по времени, внутри партии — по порядку."""
    now = now or time.time()
    ensure_schema(conn)
    return conn.execute("SELECT * FROM drafts WHERE status = 'scheduled' AND COALESCE(send_at, 0) <= ? "
                        "ORDER BY send_at, created_ts, COALESCE(json_extract(payload, '$.batch_index'), 0)",
                        (now,)).fetchall()


def inflight(conn: sqlite3.Connection, kind: str, exclude: str | None = None) -> list[tuple[float, dict]]:
    """Черновики, которые прямо сейчас отправляются: для лимитов это уже занятые места."""
    rows = conn.execute("SELECT id, sent_ts, created_ts, payload FROM drafts WHERE kind = ? AND status = 'sending'",
                        (kind,)).fetchall()
    out = []
    for r in rows:
        if r["id"] == exclude:
            continue
        try:
            out.append((r["sent_ts"] or r["created_ts"], json.loads(r["payload"])))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


# --- Журнал отправок ---

def log_sent(conn: sqlite3.Connection, kind: str, target: str, ref: str | None, ok: bool, detail: str = "",
             now: float | None = None) -> int:
    cur = conn.execute("INSERT INTO sent_log (kind, target, ref, ts, ok, detail) VALUES (?, ?, ?, ?, ?, ?)",
                       (kind, target, ref, now or time.time(), int(ok), detail[:500]))
    conn.commit()
    return int(cur.lastrowid or 0)


def reserve(conn: sqlite3.Connection, kind: str, target: str, ref: str | None, now: float | None = None) -> int:
    """Занять место в лимитах ДО необратимого шага: если процесс умрёт посреди отправки,
    лимиты всё равно будут считать, что отправка была."""
    return log_sent(conn, kind, target, ref, True, "attempt", now)


def settle(conn: sqlite3.Connection, row_id: int, detail: str) -> None:
    conn.execute("UPDATE sent_log SET detail = ? WHERE id = ?", (detail[:500], row_id))
    conn.commit()


def release(conn: sqlite3.Connection, row_id: int) -> None:
    """Точно ничего не ушло — место в лимитах освобождаем."""
    conn.execute("DELETE FROM sent_log WHERE id = ?", (row_id,))
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
                "cancelled": "отменён", "pending": "ждёт её «го»", "scheduled": "запланирован",
                "unknown": "неизвестно, ушёл ли — проверь на сайте",
                "partial": "сделан не полностью — что получилось, а что нет, в итоге"}

# Тип черновика словами (для списков черновиков); чего нет — показывается как есть.
KIND_NAMES = {"da_publish": "публикация DA", "da_status": "статус DA", "da_journal": "журнал DA",
              "da_offer": "предложение под её работой", "da_ping": "пинг", "th_reply": "отклик Toyhouse",
              "da_comment_reminder": "напоминание об оплате"}
