"""Её пинглист: кто сам попросил «ping me» под её работами, и упоминания под новой работой.

Человек попадает в список только по своей явной просьбе в комментарии («ping me», «add me to
the pinglist», «пингани», «добавь в пинглист») или если она добавила его сама. Просьба убрать
(«remove me», «unping», «не пингуй») — убирает. Решает последнее по времени слово: её ручное
удаление не перебивается старым комментарием, а новый комментарий после него — перебивает.

Пинг — это комментарий под её новой работой с @никами: её шаблон слово в слово + ники. Никаких
личных сообщений и notes. Комментарии — черновик, уходят только по её «го».
"""

import re
import time
from typing import Any

from . import auctions, buyers, selling

# DeviantArt не публикует лимит упоминаний, но комментарий с десятками @ников похож на спам и
# может попасть под фильтр. 10 на комментарий и не больше 5 комментариев за раз (50 человек) —
# осторожная граница; между комментариями при отправке нужна пауза (лимиты — в posting).
MAX_PER_COMMENT = 10
MAX_COMMENTS = 5
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,19}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pinglist (
    key TEXT PRIMARY KEY,          -- ник в нижнем регистре
    user TEXT NOT NULL,            -- как пишется
    status TEXT NOT NULL,          -- in | out
    source TEXT NOT NULL,          -- comment | manual
    deviation_id TEXT,             -- под какой работой попросил (da:<id>)
    comment_id TEXT,
    signal_ts REAL NOT NULL,       -- когда сказано последнее слово
    joined_ts REAL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pinglist_sent (
    deviation_id TEXT NOT NULL,
    key TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (deviation_id, key)
);
-- Ники, уже стоящие в черновике пинга (ещё не ушёл): второй черновик их не берёт.
CREATE TABLE IF NOT EXISTS pinglist_pending (
    deviation_id TEXT NOT NULL,
    key TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (deviation_id, key)
);
-- Разобранные комментарии: один и тот же комментарий второй раз ничего не решает.
CREATE TABLE IF NOT EXISTS pinglist_seen (
    comment_id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
"""
# Черновик пинга не планируется (approve_draft — только da_publish): живёт сутки, плюс время самой отправки.
PENDING_TTL_S = 26 * 3600


class PinglistError(ValueError):
    pass


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


# --- Разбор комментариев ---

_PL = r"ping\s*-?\s*list"
_WHOSE = r"(?:the\s+|your\s+|ur\s+|a\s+)?"
OUT = [
    re.compile(r"\bun-?ping\b", re.I),
    re.compile(rf"\b(?:remove|take|delete|drop)\s+me\s+(?:off|from|out\s+of)\s+{_WHOSE}{_PL}", re.I),
    re.compile(r"^\W*(?:please\s+|pls\s+|plz\s+)?(?:remove|take)\s+me(?:\s+off)?"
               r"(?:\s+(?:please|pls|plz|thanks|thank\s+you|ty))?\W*$", re.I),
    re.compile(r"\b(?:don'?t|do\s+not|dont|never|stop|no\s+need\s+to)\s+(?:ever\s+)?(?:ping|tag)(?:ing)?\s+me\b", re.I),
    re.compile(r"\bno\s+more\s+pings?\b", re.I),
    re.compile(rf"\b(?:don'?t|do\s+not|dont)\s+want\s+to\s+be\s+(?:on|in)\s+{_WHOSE}{_PL}", re.I),
    re.compile(r"(?:убер\w*|удал\w*|исключ\w*|выкин\w*|вычеркн\w*)\s+(?:меня\s+)?(?:из\s+)?пинг", re.I),
    re.compile(r"(?<![\w])не\s+(?:надо\s+|нужно\s+)?(?:меня\s+)?(?:пинг\w*|тег\w*)", re.I),
    re.compile(r"больше\s+не\s+(?:пинг|тег)", re.I),
    # Обычай пинглистов: «+» — добавить, «−» — убрать. Голый минус (можно с «pl»/«pinglist») — выход.
    re.compile(r"^\s*[-−–—]\s*(?:pl|ping\s*-?\s*list|пинг-?лист)?\s*[.!]*\s*$", re.I),
]
IN = [
    re.compile(rf"\b(?:add|put)\s+me\s+(?:to|on|in|into|onto)\s+{_WHOSE}{_PL}", re.I),
    re.compile(rf"\b{_PL}\s+(?:pls|please|plz|pl[sz]|me)\b", re.I),
    re.compile(rf"\b(?:can|could|may)\s+i\s+(?:be\s+|get\s+)?(?:on|in|added\s+to|join)\s+{_WHOSE}{_PL}", re.I),
    re.compile(rf"\b(?:i'?d|i\s+would)\s+(?:like|love)\s+to\s+be\s+(?:on|in|added\s+to)\s+{_WHOSE}{_PL}", re.I),
    re.compile(rf"\b{_PL}\b.{{0,25}}\badd\s+me\b", re.I),
    re.compile(r"(?<![\w])добав\w*\s+(?:меня\s+)?в\s+пинг", re.I),
    re.compile(r"(?<![\w])хочу\s+в\s+пинг", re.I),
    re.compile(r"пинг-?лист\w*\s+(?:пожалуйста|плиз|pls)", re.I),
    re.compile(r"\bping[uü]?eame\b|\bagr[eé]game\s+a\s+la\s+ping", re.I),
]
PING_ME = re.compile(r"\bping\s+me\b|(?<![\w])(?:пинган\w*|пингуй(?:те)?)(?:\s+меня)?(?![\w])", re.I)
# После голого «ping me» / «пингани» годится только вежливость или «на будущее / когда будут новые».
# «Ping me if the winner backs out», «пингани, когда оплатишь» — разовая просьба, а не пинглист.
_POLITE = r"(?:please|pls|plz|thanks|thank\s+you|ty|thx|пожалуйста|плиз|пж|спасибо)"
_FUTURE = (r"(?:(?:for|on|in|with|about|when|whenever|if)\s+(?:the\s+|your\s+|ur\s+|any\s+)?"
           r"(?:future|next|new|upcoming|more|later)\b"
           r"|(?:when|whenever|if)\s+(?:you\s+|u\s+)?(?:post|make|upload|have|do|open|drop|release|draw)\w*\s+"
           r"(?:\w+\s+)?(?:more|new|another|again|others?|next|similar)\b"
           r"|(?:на|в|про|когда|если)\s+(?:будут\s+|появятся\s+|выложишь\s+)?"
           r"(?:будущ|следующ|нов|ещ[её]|друг|похож)\w*)")
TAIL_OK = re.compile(rf"^[\W_]*(?:{_POLITE}[\W_]*)*(?:{_FUTURE}.*)?$", re.I | re.S)
ONE_OFF = re.compile(r"(?<![\w])(?:if|when|once|after|если|когда|как\s+только|после)(?![\w]).{0,50}?"
                     r"(?:winner|back(?:s|ed)?\s*out|(?<![\w])ab(?![\w])|auto-?buy|(?<![\w])ends?(?![\w])|ended|"
                     r"is\s+over|paid|(?<![\w])pay|payment|commission|decided|chosen|sold|"
                     r"победител|оплат|законч|кончит|продаш|продан|выбер|выкуп)", re.I | re.S)
# «Did you ping me?», «why did she ping me» — вопрос о прошлом, не просьба.
PAST_Q = re.compile(r"\b(?:did|have|has|why|who)\b[\w\s']*$", re.I)
# «Ping me on discord» — просьба написать в другом месте, а не пинглист.
ELSEWHERE = re.compile(r"^\s*(?:on|at|in|via|through|over)\s+(?:my\s+)?(?:discord|twitter|x|insta\w*|ig|tumblr|"
                       r"telegram|tg|toyhouse|th|notes?|dms?|private\w*|tiktok|bluesky|bsky)\b", re.I)


def classify(text: str | None) -> str | None:
    """«in» — просит добавить, «out» — просит убрать, None — ни то ни другое (или непонятно)."""
    text = (text or "").strip()
    if not text:
        return None
    if any(p.search(text) for p in OUT):
        return "out"
    if any(p.search(text) for p in IN):
        return "in"
    for m in PING_ME.finditer(text):
        tail = text[m.end():]
        if PAST_Q.search(text[max(0, m.start() - 25):m.start()]) or ELSEWHERE.match(tail):
            continue
        if ONE_OFF.search(tail) or not TAIL_OK.match(tail):
            continue  # разовая просьба — пусть добавит сама, если захочет
        return "in"
    return None


def _key(user: str) -> str:
    return user.strip().lstrip("@").lower()


def parse_users(names) -> tuple[list[str], list[str]]:
    """Ники из «@nick», «nick», ссылки на профиль DeviantArt → (правильные, неправильные)."""
    if isinstance(names, str):
        names = re.split(r"[\s,;]+", names)
    ok, bad, seen = [], [], set()
    for raw in names or []:
        name = str(raw).strip()
        if not name:
            continue
        m = re.search(r"deviantart\.com/([^/?#\s]+)", name, re.I)
        name = (m.group(1) if m else name).strip().lstrip("@").rstrip("/")
        if not USERNAME.match(name):
            bad.append(str(raw).strip())
        elif name.lower() not in seen:
            seen.add(name.lower())
            ok.append(name)
    return ok, bad


def _apply(conn, user: str, status: str, source: str, signal_ts: float, now: float, deviation_id: str | None = None,
           comment_id: str | None = None) -> str | None:
    """Записать слово человека/её. Возвращает "added" / "removed" / None (ничего не поменялось)."""
    key = _key(user)
    old = conn.execute("SELECT * FROM pinglist WHERE key = ?", (key,)).fetchone()
    if old and old["signal_ts"] >= signal_ts:
        return None  # уже есть слово новее (или это же самое)
    joined = (old["joined_ts"] if old and old["status"] == "in" else (signal_ts or now)) if status == "in" else \
        (old["joined_ts"] if old else None)
    conn.execute(
        "INSERT INTO pinglist (key, user, status, source, deviation_id, comment_id, signal_ts, joined_ts, updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET user = excluded.user, status = excluded.status, "
        "source = excluded.source, deviation_id = COALESCE(excluded.deviation_id, pinglist.deviation_id), "
        "comment_id = excluded.comment_id, signal_ts = excluded.signal_ts, joined_ts = excluded.joined_ts, "
        "updated_ts = excluded.updated_ts",
        (key, user.strip().lstrip("@"), status, source, deviation_id, comment_id, signal_ts, joined, now))
    was_in = bool(old and old["status"] == "in")
    if status == "in" and not was_in:
        return "added"
    if status == "out" and was_in:
        return "removed"
    return None


def ingest(conn, deviation_id: str, comments: list, owner: str, now: float | None = None) -> dict:
    """Комментарии под её работой (bids.Comment, dict из API/страницы) → пинглист.

    Свои комментарии не считаются. Повторный разбор тех же комментариев ничего не меняет (разобранные
    comment_id запоминаются). Комментарий без даты не может вернуть в список того, о ком уже есть запись
    (её ручное удаление или более новое слово); просьбу убрать без даты — выполняем всегда.
    Возвращает {"added": [ники], "removed": [ники]} — только реальные изменения."""
    now = now or time.time()
    ensure(conn)
    owner_key = _key(owner or "")
    added: list[str] = []
    removed: list[str] = []
    for c in sorted(auctions.to_comments(comments), key=lambda c: (c.posted_ts or 0, c.comment_id or "")):
        if not c.user or _key(c.user) == owner_key or not USERNAME.match(c.user.strip().lstrip("@")):
            continue
        signal = classify(c.text)
        cid = str(c.comment_id or "")
        if not signal:
            if buyers.detect_opt_out(c.text):
                # «stop» под её работой (например, в ответ на предложение): больше не предлагаем.
                # Каждый комментарий решает один раз. Новый комментарий без даты — сказан сейчас (иначе
                # после её opt_in он бы навсегда считался «старым»); без id и даты — «неизвестно когда».
                if cid and conn.execute("SELECT 1 FROM pinglist_seen WHERE comment_id = ?", (cid,)).fetchone():
                    continue
                if cid:
                    conn.execute("INSERT OR IGNORE INTO pinglist_seen (comment_id, ts) VALUES (?, ?)", (cid, now))
                buyers.opt_out(conn, [c.user], "comment_stop", now,
                               signal_ts=c.posted_ts or (now if cid else 0.0))
            continue
        if cid and conn.execute("SELECT 1 FROM pinglist_seen WHERE comment_id = ?", (cid,)).fetchone():
            continue
        if cid:
            conn.execute("INSERT OR IGNORE INTO pinglist_seen (comment_id, ts) VALUES (?, ?)", (cid, now))
        signal_ts = c.posted_ts
        if not signal_ts:
            known = conn.execute("SELECT 1 FROM pinglist WHERE key = ?", (_key(c.user),)).fetchone()
            if signal == "in" and known:
                continue
            signal_ts = now if signal == "out" else 0
        what = _apply(conn, c.user, signal, "comment", signal_ts, now, deviation_id, c.comment_id)
        name = c.user.strip().lstrip("@")
        if what == "added":
            removed = [u for u in removed if _key(u) != _key(name)]
            added.append(name)
        if signal == "out":
            # Ушёл из пинглиста — значит, и предложения ему больше не нужны (пока она сама не вернёт).
            buyers.opt_out(conn, [name], "pinglist_leave", now, signal_ts=signal_ts)
        if what == "removed":
            if any(_key(u) == _key(name) for u in added):
                added = [u for u in added if _key(u) != _key(name)]
            else:
                removed.append(name)
    conn.commit()
    return {"added": added, "removed": removed}


def add(conn, names, now: float | None = None) -> list[str]:
    """Она добавляет сама. Неправильные ники пропускаются (см. parse_users); все неправильные — ошибка."""
    now = now or time.time()
    ok, bad = parse_users(names)
    if not ok and bad:
        raise PinglistError(f"Не ники DeviantArt: {', '.join(bad)}")
    ensure(conn)
    out = [u for u in ok if _apply(conn, u, "in", "manual", now, now) == "added"]
    conn.commit()
    return out


def remove(conn, names, now: float | None = None) -> list[str]:
    now = now or time.time()
    ok, _bad = parse_users(names)
    ensure(conn)
    out = []
    for u in ok:
        row = conn.execute("SELECT user FROM pinglist WHERE key = ?", (_key(u),)).fetchone()
        if _apply(conn, u if not row else row["user"], "out", "manual", now, now) == "removed":
            out.append(row["user"] if row else u)
    conn.commit()
    return out


def members(conn) -> list[dict]:
    ensure(conn)
    rows = conn.execute("SELECT * FROM pinglist WHERE status = 'in' ORDER BY joined_ts, key").fetchall()
    return [{"user": r["user"], "since": r["joined_ts"], "source": r["source"], "deviation_id": r["deviation_id"]}
            for r in rows]


def render_members(rows: list[dict], limit: int = 40) -> str:
    if not rows:
        return "Пинглист пуст."
    names = ", ".join(r["user"] for r in rows[:limit])
    more = f" и ещё {len(rows) - limit}" if len(rows) > limit else ""
    return f"В пинглисте {len(rows)}: {names}{more}"


# --- Пинг под новой работой ---

def mark_pinged(conn, deviation_id: str, users: list[str], now: float | None = None) -> None:
    """После отправки комментария: этих людей под этой работой больше не упоминаем."""
    ensure(conn)
    conn.executemany("INSERT OR IGNORE INTO pinglist_sent (deviation_id, key, ts) VALUES (?, ?, ?)",
                     [(deviation_id, _key(u), now or time.time()) for u in users])
    conn.executemany("DELETE FROM pinglist_pending WHERE deviation_id = ? AND key = ?",
                     [(deviation_id, _key(u)) for u in users])
    conn.commit()


def reserve(conn, deviation_id: str, users: list[str], now: float | None = None) -> None:
    """Черновик пинга создан: эти ники заняты, второй черновик под этой работой их не возьмёт."""
    ensure(conn)
    conn.executemany("INSERT OR REPLACE INTO pinglist_pending (deviation_id, key, ts) VALUES (?, ?, ?)",
                     [(deviation_id, _key(u), now or time.time()) for u in users])
    conn.commit()


def release(conn, deviation_id: str, users: list[str], reserved_until: float | None = None) -> None:
    """Черновик отменён / не ушёл: места освобождаются. reserved_until — только места, занятые не позже
    этого времени (чтобы не освободить ники, которые потом занял другой черновик)."""
    ensure(conn)
    conn.executemany("DELETE FROM pinglist_pending WHERE deviation_id = ? AND key = ? AND ts <= ?",
                     [(deviation_id, _key(u), float("inf") if reserved_until is None else reserved_until)
                      for u in users])
    conn.commit()


def not_wanted(conn, deviation_id: str, users: list[str]) -> list[str]:
    """Перед самой отправкой: кто уже вышел из пинглиста, уже упомянут под этой работой или попал в её
    чёрный список (неоплата, спор)."""
    ensure(conn)
    out = []
    bad = buyers.bad_deals(conn)
    for u in users:
        k = _key(u)
        row = conn.execute("SELECT status FROM pinglist WHERE key = ?", (k,)).fetchone()
        sent = conn.execute("SELECT 1 FROM pinglist_sent WHERE deviation_id = ? AND key = ?", (deviation_id, k)).fetchone()
        if not row or row["status"] != "in" or sent or k in bad:
            out.append(u)
    return out


def _render(template: str, title: str, url: str, users: list[str]) -> str:
    mentions = " ".join(f"@{u}" for u in users)
    text = selling.render_template(template, {"title": title, "url": url, "mentions": mentions})
    return text if "{mentions}" in template else f"{text.rstrip()}\n{mentions}"


def mention_comments(conn, *, deviation_id: str, deviation_url: str, title: str = "",
                     per_comment: int = MAX_PER_COMMENT, max_comments: int = MAX_COMMENTS,
                     reserve_users: bool = False, now: float | None = None) -> dict:
    """Комментарии-пинги под её новой работой: её шаблон «pinglist» + до per_comment @ников в каждом.

    {"ok", "reason", "deviation_id", "deviation_url", "comments": [{"text", "users"}], "left_out": [ники],
     "already": сколько уже упомянуто или стоит в черновике под этой работой,
     "blocked": [(ник, почему)] — в её чёрном списке или с неоплатой/спором: их не отмечаем}
    Под одной работой — не больше MAX_PER_COMMENT × MAX_COMMENTS упоминаний за всё время, не только за раз.
    Работа должна быть её (items.own = 1). reserve_users=True — при создании черновика: ники занимаются,
    второй черновик их не возьмёт (после отправки — mark_pinged, при отмене — release).
    Если в шаблоне нет {mentions}, ники идут новой строкой после её текста."""
    from .posting import COMMENT_MAX

    now = now or time.time()
    ensure(conn)
    per_comment = max(1, min(int(per_comment or MAX_PER_COMMENT), MAX_PER_COMMENT))
    max_comments = max(1, min(int(max_comments or MAX_COMMENTS), MAX_COMMENTS))
    base: dict[str, Any] = {"deviation_id": deviation_id, "deviation_url": deviation_url, "comments": [],
                            "left_out": [], "already": 0, "blocked": []}
    item = conn.execute("SELECT own FROM items WHERE id = ?", (deviation_id,)).fetchone()
    if not item:
        return {**base, "ok": False, "reason": "этой работы нет в базе — сначала обнови свои работы"}
    if not item["own"]:
        return {**base, "ok": False, "reason": "это не её работа — пингую только под её работами"}
    template = selling.get_template(conn, "pinglist")
    if not template:
        return {**base, "ok": False, "reason": "нет её шаблона пинга (слоты {title} {url} {mentions}) — "
                                                  "без её текста не готовлю"}
    if len(_render(template, title, deviation_url, ["x" * 20])) > COMMENT_MAX:
        return {**base, "ok": False, "reason": f"её шаблон с ником длиннее {COMMENT_MAX} знаков — DA не примет"}
    taken = {r["key"] for r in conn.execute("SELECT key FROM pinglist_sent WHERE deviation_id = ?", (deviation_id,))}
    taken |= {r["key"] for r in conn.execute("SELECT key FROM pinglist_pending WHERE deviation_id = ? AND ts > ?",
                                             (deviation_id, now - PENDING_TTL_S))}
    # Её чёрный список, неоплата, спор — даже из пинглиста не отмечаем.
    bad = buyers.bad_deals(conn)
    everyone = [m["user"] for m in members(conn)]
    base["blocked"] = [(u, bad[_key(u)]) for u in everyone if _key(u) in bad]
    everyone = [u for u in everyone if _key(u) not in bad]
    todo = [u for u in everyone if _key(u) not in taken]
    base["already"] = len(taken)
    room = MAX_PER_COMMENT * MAX_COMMENTS - len(taken)
    if not todo:
        return {**base, "ok": False, "reason": "пинглист пуст" if not everyone else "все уже упомянуты под этой работой"}
    if room <= 0:
        return {**base, "ok": False, "left_out": todo,
                "reason": f"под этой работой уже {len(taken)} упоминаний — больше не надо, это похоже на спам"}
    chunks: list[list[str]] = []
    for u in todo:
        if chunks and len(chunks[-1]) < per_comment and \
                len(_render(template, title, deviation_url, chunks[-1] + [u])) <= COMMENT_MAX:
            chunks[-1].append(u)
        else:
            chunks.append([u])
    use: list[list[str]] = []
    left: list[str] = []
    for users in chunks:
        if len(use) < max_comments and sum(len(x) for x in use) + len(users) <= room:
            use.append(users)
        else:
            left += users
    for users in use:
        base["comments"].append({"text": _render(template, title, deviation_url, users), "users": users})
    base["left_out"] = left
    if reserve_users:
        reserve(conn, deviation_id, [u for users in use for u in users], now)
    return {**base, "ok": True, "reason": ""}


# --- Ранний доступ пинглиста ---

EARLY_ACCESS_MAX_H = 48
GROUPS_NOTE = ("группы DA через API выбираются только в момент публикации: при раннем доступе работа выходит "
               "в галерею сразу, а в группы она добавляет вручную позже")


def early_access_plan(publish_ts: float, hours: float) -> dict:
    """Пинглист видит работу раньше всех: пинг сразу после публикации, X/статус/группы — через hours часов.
    hours = 0 — выключено (всё в момент публикации). {"enabled", "ping_at", "public_at", "steps", "note"}"""
    hours = max(0.0, min(float(hours or 0), EARLY_ACCESS_MAX_H))
    ping_at = float(publish_ts)
    public_at = ping_at + hours * 3600
    if not hours:
        return {"enabled": False, "ping_at": ping_at, "public_at": ping_at, "steps": [], "note": ""}
    steps = [
        {"at": ping_at, "what": "пинг пинглиста комментарием под её работой (черновик → её «го»)"},
        {"at": public_at, "what": "пост в X (черновик, «Post» жмёт она)"},
        {"at": public_at, "what": "статус на DeviantArt (черновик → её «го»)"},
        {"at": public_at, "what": "группы DA — вручную, по лимитам групп"},
    ]
    return {"enabled": True, "ping_at": ping_at, "public_at": public_at, "steps": steps, "note": GROUPS_NOTE}
