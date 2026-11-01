"""Её тёплые контакты: только люди, которые сами имели дело с НЕЙ за последние полгода.

Источники (только база, без сети): её продажи (покупатели), ставки на её лотах, её пинглист, фавы её
свежих лотов (refresh_favers, API whofaved) и авторы LF-тредов Toyhouse, которым она отвечала.
Чужие ватчеры и случайные люди сюда не попадают.

Кому писать нельзя: кто попросил не писать (opt-out навсегда, пока она сама не вернёт), кому меньше 18
по данным DeviantArt (is_minor = 1), кому она уже писала за последние cooldown дней. Возраст неизвестен —
можно, но с пометкой «возраст неизвестен»: она берёт деньги через Boosty.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from . import taxonomy

DAY = 86400
WINDOW_DAYS = 180
COOLDOWN_DAYS = 14
FAVER_DAYS = 14
ROLES = ("buyer", "bidder", "pinglist", "faver", "commenter", "th_lf")
# Личные предложения — только им: покупал(а), ставил(а) на ЕЁ лоты, сам(а) вступил(а) в пинглист.
# Фав, комментарий, LF-тред — интерес, но не повод писать лично (на DA это незваная реклама).
DM_ROLES = ("buyer", "bidder", "pinglist")
ROLE_NAMES = {"buyer": "покупал(а)", "bidder": "ставил(а)", "pinglist": "в пинглисте", "faver": "фавнул(а)",
              "commenter": "комментировал(а)", "th_lf": "LF Toyhouse"}
# Вес события для интересов: покупка говорит о вкусе больше, чем ставка, ставка — больше, чем фав.
INTEREST_WEIGHT = {"buyer": 3, "bidder": 2, "faver": 1}
INTEREST_CATS = ("species", "body", "styles", "themes")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    username TEXT PRIMARY KEY,     -- ник в нижнем регистре
    display TEXT NOT NULL,
    platforms TEXT,                -- JSON: ["deviantart", "toyhouse", "x"]
    first_ts REAL,
    last_interaction_ts REAL,      -- последнее его действие у НЕЁ (покупка, ставка, фав, пинглист)
    last_contact_ts REAL,          -- когда она писала ему последний раз (предложение, отклик)
    interests TEXT,                -- JSON: {"species:moth": 5, ...} — взвешенно по покупкам/ставкам/фавам
    roles TEXT,                    -- JSON: ["buyer", "bidder", ...]
    dm_roles TEXT,                 -- JSON: {"deviantart": ["bidder"], "toyhouse": ["buyer"]} — личные роли по площадкам
    is_minor INTEGER,              -- 1 / 0 по данным DeviantArt, NULL — неизвестно
    opted_out_ts REAL,
    opt_out_reason TEXT,
    opt_in_ts REAL,                -- она вернула человека: старые «stop» до этого момента не считаются
    note TEXT,
    dirty INTEGER DEFAULT 1        -- 1 — есть новые события: sync пересчитает роли и интересы
);
-- До какой записи sent_log sync уже учёл её предложения (last_contact_ts).
CREATE TABLE IF NOT EXISTS buyers_sync (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sent_log_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_events (
    username TEXT NOT NULL,
    kind TEXT NOT NULL,            -- buyer | bidder | pinglist | faver | commenter | th_lf
    ref TEXT NOT NULL,             -- лот (da:<id>), продажа, тред
    ts REAL NOT NULL,
    platform TEXT,                 -- где было: deviantart | toyhouse | other | x
    PRIMARY KEY (username, kind, ref)
);
CREATE INDEX IF NOT EXISTS contact_events_user ON contact_events(username, ts);
-- Когда последний раз читали фавы лота: по кругу, а не всегда одни и те же свежие.
CREATE TABLE IF NOT EXISTS faver_scan (
    item_id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
"""
_ALTER = {"opt_in_ts": "REAL", "dirty": "INTEGER DEFAULT 1", "dm_roles": "TEXT"}


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    for col, typ in _ALTER.items():
        if col not in have:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {typ}")
    if "dm_roles" not in have:
        conn.execute("UPDATE contacts SET dirty = 1")  # личные роли по площадкам посчитает следующий sync
    if "platform" not in {r[1] for r in conn.execute("PRAGMA table_info(contact_events)")}:
        conn.execute("ALTER TABLE contact_events ADD COLUMN platform TEXT")


def key(user: str | None) -> str:
    return (user or "").strip().lstrip("@").lower()


def _platform_for(kind: str, platform: str | None = None) -> str:
    if platform:
        return platform
    return "toyhouse" if kind == "th_lf" else "deviantart"


def _upsert_contact(conn, user: str, platform: str | None = None, is_minor: int | None = None) -> None:
    k = key(user)
    if not k:
        return
    row = conn.execute("SELECT platforms, is_minor FROM contacts WHERE username = ?", (k,)).fetchone()
    if not row:
        conn.execute("INSERT INTO contacts (username, display, platforms, interests, roles, is_minor) "
                     "VALUES (?, ?, ?, '{}', '[]', ?)",
                     (k, user.strip().lstrip("@"), json.dumps([platform] if platform else []), is_minor))
        return
    plats = _json_list(row["platforms"])
    if platform and platform not in plats:
        plats.append(platform)
    # «Младше 18» не стирается: 1 не меняется ни на 0, ни на «неизвестно».
    conn.execute("UPDATE contacts SET platforms = ?, is_minor = CASE WHEN is_minor = 1 THEN 1 "
                 "ELSE COALESCE(?, is_minor) END WHERE username = ?", (json.dumps(plats), is_minor, k))


def _json_list(raw) -> list:
    try:
        val = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return val if isinstance(val, list) else []


def _json_dict(raw) -> dict:
    try:
        val = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def add_event(conn, user: str, kind: str, ref: str, ts: float, platform: str | None = None,
              is_minor: int | None = None) -> bool:
    """Записать его действие у неё. Повтор того же (user, kind, ref) ничего не меняет (кроме более раннего ts)."""
    k = key(user)
    if not k or kind not in ROLES:
        return False
    ensure(conn)
    plat = _platform_for(kind, platform)
    _upsert_contact(conn, user, plat, is_minor)
    cur = conn.execute("INSERT OR IGNORE INTO contact_events (username, kind, ref, ts, platform) VALUES (?, ?, ?, ?, ?)",
                       (k, kind, str(ref), float(ts or 0), plat))
    if cur.rowcount:
        conn.execute("UPDATE contacts SET dirty = 1 WHERE username = ?", (k,))
    return cur.rowcount > 0


# --- Теги лотов → интересы ---

def tags_flat(t: taxonomy.Tags) -> list[str]:
    out = []
    for cat in INTEREST_CATS:
        out += [f"{cat}:{v}" for v in getattr(t, cat)]
    return out


def item_tags(conn, item_id: str | None, title: str = "") -> list[str]:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone() if item_id else None
    if row:
        return tags_flat(taxonomy.tag_item(row, use_snippet=False))
    return tags_flat(taxonomy.tag_text(title or "")) if title else []


def _interests(conn, username: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for ev in conn.execute("SELECT kind, ref FROM contact_events WHERE username = ? AND kind IN ('buyer','bidder','faver')",
                           (username,)):
        ref = ev["ref"]
        title = ""
        if not ref.startswith(("da:", "th:")):
            s = conn.execute("SELECT item_id, title FROM sales WHERE id = ?", (ref,)).fetchone()
            ref, title = (s["item_id"], s["title"]) if s else (None, "")
        for t in item_tags(conn, ref, title):
            out[t] = out.get(t, 0) + INTEREST_WEIGHT[ev["kind"]]
    return out


def _recompute(conn, username: str, now: float) -> None:
    evs = conn.execute("SELECT kind, MIN(ts) AS lo, MAX(ts) AS hi FROM contact_events WHERE username = ? GROUP BY kind",
                       (username,)).fetchall()
    roles = [r for r in ROLES if any(e["kind"] == r for e in evs)]
    first = min((e["lo"] for e in evs if e["lo"]), default=None)
    last = max((e["hi"] for e in evs if e["hi"]), default=None)
    contact = conn.execute(
        "SELECT MAX(ts) FROM sent_log WHERE ok = 1 AND kind IN ('da_offer', 'offer_manual') AND target = ?",
        (username,)).fetchone()[0]
    # Личные роли по площадкам: покупка на Toyhouse или вне площадок (other) — не повод отмечать на DA.
    # Событие без площадки (старая запись) — площадка неизвестна, личного канала не даёт.
    dm: dict[str, list[str]] = {}
    for e in conn.execute(f"SELECT DISTINCT kind, platform FROM contact_events WHERE username = ? AND kind IN "
                          f"({','.join('?' * len(DM_ROLES))}) AND platform IS NOT NULL", (username, *DM_ROLES)):
        lst = dm.setdefault(e["platform"], [])
        if e["kind"] not in lst:
            lst.append(e["kind"])
    conn.execute("UPDATE contacts SET roles = ?, first_ts = ?, last_interaction_ts = ?, interests = ?, dm_roles = ?, "
                 "last_contact_ts = MAX(COALESCE(last_contact_ts, 0), COALESCE(?, 0)) WHERE username = ?",
                 (json.dumps(roles), first, last, json.dumps(_interests(conn, username), ensure_ascii=False),
                  json.dumps({p: sorted(v) for p, v in sorted(dm.items())}), contact, username))
    conn.execute("UPDATE contacts SET last_contact_ts = NULL WHERE username = ? AND last_contact_ts = 0", (username,))


PAID_STATUSES = ("paid", "transferred", "done")


def _sales_events(conn, me: str) -> int:
    """Покупатель — только кто оплатил. Выиграл, но не оплатил (won) — как ставивший, не покупатель.
    Площадка — как в журнале: продажа вне DA/Toyhouse (other: X, Telegram, VK) остаётся other и личного
    канала на DA не даёт. Продажа отменена (возврат, чарджбэк, неоплата) — роль «покупатель» по ней снимаем."""
    n = 0
    valid: set[tuple[str, str]] = set()
    for r in conn.execute("SELECT id, item_id, buyer, platform, status, created_ts FROM sales "
                          "WHERE buyer IS NOT NULL AND buyer != '' AND status != 'cancelled'"):
        if key(r["buyer"]) == me:
            continue
        plat = r["platform"] if r["platform"] in ("deviantart", "toyhouse", "x") else "other"
        kind = "buyer" if r["status"] in PAID_STATUSES else "bidder"
        ref = str(r["item_id"] or r["id"])
        if kind == "buyer":
            valid.add((key(r["buyer"]), ref))
        n += add_event(conn, r["buyer"], kind, ref, r["created_ts"], plat)
    for ev in conn.execute("SELECT username, ref FROM contact_events WHERE kind = 'buyer'").fetchall():
        if (ev["username"], ev["ref"]) not in valid:
            conn.execute("DELETE FROM contact_events WHERE username = ? AND kind = 'buyer' AND ref = ?",
                         (ev["username"], ev["ref"]))
            conn.execute("UPDATE contacts SET dirty = 1 WHERE username = ?", (ev["username"],))
    return n


def _bidder_events(conn, me: str) -> int:
    n = 0
    rows = conn.execute("SELECT a.deviation_id, a.extra, a.updated_ts FROM auctions a JOIN items i "
                        "ON i.id = a.deviation_id WHERE i.own = 1").fetchall()
    for r in rows:
        bidders = _json_dict(r["extra"]).get("bidders") or []
        for b in bidders if isinstance(bidders, list) else []:
            user = b.get("user") if isinstance(b, dict) else None
            if not user or key(user) == me:
                continue
            plat = "toyhouse" if str(r["deviation_id"]).startswith("th:") else "deviantart"
            n += add_event(conn, user, "bidder", r["deviation_id"], b.get("ts") or r["updated_ts"], plat)
    return n


def _pinglist_events(conn, me: str) -> int:
    from . import pinglist

    n = 0
    for m in pinglist.members(conn):
        if key(m["user"]) != me:
            n += add_event(conn, m["user"], "pinglist", "pinglist", m["since"] or 0)
    return n


def _th_lf_events(conn, me: str, th_me: str = "") -> int:
    n = 0
    rows = conn.execute("SELECT s.target, MAX(s.ts) AS ts FROM sent_log s WHERE s.kind = 'th_reply' AND s.ok = 1 "
                        "GROUP BY s.target").fetchall()
    for r in rows:
        tid = str(r["target"] or "")
        tid = tid if tid.startswith("th:") else f"th:{tid}"
        item = conn.execute("SELECT author FROM items WHERE id = ?", (tid,)).fetchone()
        if item and item["author"] and key(item["author"]) not in (me, key(th_me)):
            n += add_event(conn, item["author"], "th_lf", tid, r["ts"], "toyhouse")
    return n


def sync(conn, now: float | None = None, my_username: str = "", th_username: str = "") -> dict:
    """Собрать контакты из базы. Только база, без сети; повторный вызов ничего не дублирует.
    Возвращает {"new_events": n, "contacts": всего}."""
    now = now or time.time()
    ensure(conn)
    from . import pinglist, selling

    pinglist.ensure(conn)
    selling.ensure(conn)
    me = key(my_username)
    n = _sales_events(conn, me) + _bidder_events(conn, me) + _pinglist_events(conn, me) + _th_lf_events(conn, me,
                                                                                                      th_username)
    # Пересчёт только тех, у кого что-то изменилось: новые события (dirty) или её новые предложения в sent_log.
    # Пересчитывать всех каждый проход — секунды под блокировкой базы при тысячах контактов.
    row = conn.execute("SELECT sent_log_id FROM buyers_sync WHERE id = 1").fetchone()
    last_id = int(row[0]) if row else 0
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM sent_log").fetchone()[0]
    todo = {r[0] for r in conn.execute("SELECT username FROM contacts WHERE COALESCE(dirty, 1) = 1")}
    todo |= {key(r[0]) for r in conn.execute("SELECT DISTINCT target FROM sent_log WHERE id > ? AND ok = 1 AND kind IN "
                                             "('da_offer', 'offer_manual')", (last_id,))}
    have = {r[0] for r in conn.execute("SELECT username FROM contacts")} if todo else set()
    for username in sorted(todo & have):
        _recompute(conn, username, now)
        conn.execute("UPDATE contacts SET dirty = 0 WHERE username = ?", (username,))
    conn.execute("INSERT INTO buyers_sync (id, sent_log_id) VALUES (1, ?) "
                 "ON CONFLICT(id) DO UPDATE SET sent_log_id = excluded.sent_log_id", (top,))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM contacts WHERE last_interaction_ts IS NOT NULL").fetchone()[0]
    return {"new_events": n, "contacts": total}


# --- Фавы её свежих лотов (API) ---

def _faver_rows(resp) -> list[dict]:
    if isinstance(resp, dict):
        resp = resp.get("results") or []
    out = []
    for r in resp or []:
        if not isinstance(r, dict):
            continue
        nested = r.get("user")
        user: dict = nested if isinstance(nested, dict) else r
        name = user.get("username")
        if not name:
            continue
        minor = user.get("is_minor")
        if minor is True or minor == 1:
            flag: int | None = 1
        elif isinstance(nested, dict) and "is_minor" in nested and minor is not None:
            flag = 0   # ответ API как есть: поле пришло и сказало «нет»
        else:
            # Плоские строки DAClient.whofaved делают bool(...) — отсутствующее поле стало бы False.
            # «Нет» там не отличить от «неизвестно», поэтому верим только «да».
            flag = None
        out.append({"user": str(name), "ts": r.get("time") or r.get("ts"), "is_minor": flag})
    return out


FAVERS_PER_CALL = 50  # одна страница /deviation/whofaved = один запрос


def refresh_favers(conn, client, now: float | None = None, max_calls: int = 3, my_username: str = "") -> dict:
    """Кто фавнул её лоты за последние FAVER_DAYS дней: client.whofaved — только чтение, по одной странице
    (один запрос) на лот, не больше max_calls. Лоты по кругу: сначала ни разу не читанные, потом давно читанные.
    Лимит или нет входа — стоп; другая ошибка DeviantArt (лот удалён) — пропускаем лот, идём дальше."""
    from . import da_access, da_api

    now = now or time.time()
    ensure(conn)
    me = key(my_username)
    rows = conn.execute("SELECT i.id, i.extra FROM items i LEFT JOIN faver_scan f ON f.item_id = i.id "
                        "WHERE i.source = 'deviantart' AND i.own = 1 AND COALESCE(i.published_ts, i.first_seen_ts) >= ? "
                        "ORDER BY COALESCE(f.ts, 0), COALESCE(i.published_ts, i.first_seen_ts) DESC",
                        (now - FAVER_DAYS * DAY,)).fetchall()
    stats: dict[str, Any] = {"calls": 0, "favers": 0, "errors": 0, "stopped": None}
    with da_access.task("buyers", low=True):
        for r in rows:
            if stats["calls"] >= max(0, int(max_calls)):
                break
            uuid = _json_dict(r["extra"]).get("uuid")
            if not uuid:
                continue
            stats["calls"] += 1
            try:
                resp = client.whofaved(uuid, max_users=FAVERS_PER_CALL)
            except (da_api.RateLimited, da_api.NotConnected) as e:
                stats["stopped"] = type(e).__name__
                break
            except da_api.DAApiError:
                stats["errors"] += 1
                resp = []
            conn.execute("INSERT INTO faver_scan (item_id, ts) VALUES (?, ?) "
                         "ON CONFLICT(item_id) DO UPDATE SET ts = excluded.ts", (r["id"], now))
            for f in _faver_rows(resp):
                if key(f["user"]) == me:
                    continue
                stats["favers"] += add_event(conn, f["user"], "faver", r["id"], f["ts"] or now,
                                             "deviantart", f["is_minor"])
                if f["is_minor"] is not None:
                    _upsert_contact(conn, f["user"], "deviantart", f["is_minor"])
            conn.commit()
    conn.commit()
    return stats


# --- Просьбы не писать ---

_POLITE = r"(?:please|pls|plz|thanks|thank\s+you|ty|thx|it|now|already)"
_STOP = [
    re.compile(rf"^\W*(?:please\s+|pls\s+|plz\s+)?stop(?:\W+{_POLITE})*\W*$", re.I),
    re.compile(r"^\W*(?:please\s+|pls\s+|plz\s+)?remove\s+me\b", re.I),
    re.compile(r"\bstop\s+(?:tagging|pinging|messaging|mentioning|dming|dm'?ing|sending|contacting|"
               r"spamming|writing\s+to)\s+me\b", re.I),
    re.compile(r"\bno\s+thanks?\b(?!\s+to\b)|\bno\s+thank\s+you\b|\bnot\s+interested\b", re.I),
    re.compile(r"\b(?:don'?t|do\s+not|dont)\s+(?:ever\s+)?(?:tag|ping|mention|message|dm|contact)\s+me\b", re.I),
    re.compile(r"\bunsubscribe\b", re.I),
    re.compile(r"(?<![\w])не\s+(?:надо\s+)?(?:меня\s+)?(?:тегай|тегать|отмечай|отмечать|пингуй|пинговать)\w*",
               re.I),
    # «не пиши» — только о ней самой: «не пиши мне», «не надо мне писать», «больше не пишите» в конце.
    # «Не пиши так мелко», «не надо писать цену» — не про неё.
    re.compile(r"(?<![\w])не\s+(?:надо\s+)?(?:мне\s+)?(?:пиши|пишите|писать)\s+мне(?![\w])", re.I),
    re.compile(r"(?<![\w])не\s+(?:надо\s+)?(?:мне\s+)?(?:пиши|пишите|писать)(?:\s+(?:мне|больше))*\W*$", re.I),
    re.compile(r"(?<![\w])отпиши(?:те)?(?![\w])", re.I),
    re.compile(r"^\W*стоп\W*$", re.I),
]


def detect_opt_out(text: str | None) -> bool:
    """«stop», «no thanks», «don't tag me», «unsubscribe», «не тегай», «не пиши», «отпишите»."""
    t = (text or "").strip()
    return bool(t) and any(p.search(t) for p in _STOP)


def opt_out(conn, users, reason: str = "", now: float | None = None, signal_ts: float | None = None) -> list[str]:
    """Больше не предлагать этим людям ничего (навсегда, пока она сама не вернёт через opt_in).
    signal_ts — когда человек это сказал (0 — неизвестно когда): сказанное до её opt_in не считается.
    None — решила она сама, применяется всегда."""
    now = now or time.time()
    ensure(conn)
    names = [users] if isinstance(users, str) else list(users or [])
    out = []
    for u in names:
        k = key(u)
        if not k:
            continue
        _upsert_contact(conn, str(u))
        row = conn.execute("SELECT opted_out_ts, opt_in_ts FROM contacts WHERE username = ?", (k,)).fetchone()
        if row["opted_out_ts"] or (row["opt_in_ts"] and signal_ts is not None and signal_ts <= row["opt_in_ts"]):
            continue
        conn.execute("UPDATE contacts SET opted_out_ts = ?, opt_out_reason = ? WHERE username = ?",
                     (now, (reason or "")[:100], k))
        out.append(str(u).strip().lstrip("@"))
    conn.commit()
    return out


def opt_in(conn, users, now: float | None = None) -> list[str]:
    """Она сама возвращает человека (он написал, что снова хочет предложения)."""
    now = now or time.time()
    ensure(conn)
    names = [users] if isinstance(users, str) else list(users or [])
    out = []
    for u in names:
        cur = conn.execute("UPDATE contacts SET opted_out_ts = NULL, opt_out_reason = NULL, opt_in_ts = ? "
                           "WHERE username = ? AND opted_out_ts IS NOT NULL", (now, key(u)))
        if cur.rowcount:
            out.append(str(u).strip().lstrip("@"))
    conn.commit()
    return out


def is_opted_out(conn, user: str) -> bool:
    ensure(conn)
    row = conn.execute("SELECT opted_out_ts FROM contacts WHERE username = ?", (key(user),)).fetchone()
    return bool(row and row["opted_out_ts"])


# --- Кому можно писать ---

def contact(conn, user: str) -> dict | None:
    ensure(conn)
    row = conn.execute("SELECT * FROM contacts WHERE username = ?", (key(user),)).fetchone()
    return _as_dict(row) if row else None


def _as_dict(row) -> dict:
    d = dict(row)
    d["roles"] = _json_list(d.get("roles"))
    d["platforms"] = _json_list(d.get("platforms"))
    d["interests"] = _json_dict(d.get("interests"))
    d["dm_roles"] = {p: [r for r in v if r in DM_ROLES] for p, v in _json_dict(d.get("dm_roles")).items()
                     if isinstance(v, list)}
    return d


def dm_roles(c: dict) -> dict[str, set[str]]:
    """Личные роли (покупал, ставил, пинглист) по площадкам. В старой строке без dm_roles — только если площадка
    одна (deviantart или toyhouse): иначе не угадываем, где человек покупал."""
    if "dm_roles" in c and isinstance(c["dm_roles"], dict):
        return {p: set(v) & set(DM_ROLES) for p, v in c["dm_roles"].items() if v}
    plats = set(c.get("platforms") or [])
    roles = set(c.get("roles") or []) & set(DM_ROLES)
    if roles and len(plats) == 1 and plats <= {"deviantart", "toyhouse"}:
        return {plats.pop(): roles}
    return {}


def dm_ok(c: dict) -> bool:
    """Есть личная роль на DeviantArt или Toyhouse — там, где ей есть куда написать."""
    r = dm_roles(c)
    return bool(r.get("deviantart") or r.get("toyhouse"))


BAD_NO_PAY = "не заплатил(а) у тебя — лично не предлагаю"
BAD_DISPUTE = "был спор или возврат через банк — лично не предлагаю"
BAD_LISTED = "в твоём чёрном списке — лично не предлагаю"


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def bad_deals(conn) -> dict[str, str]:
    """Ник → почему ей нельзя готовить ему предложение: её чёрный список, неоплата, спор или чарджбэк
    (её отметки protection). Только чтение; таблиц ещё нет — пусто."""
    out: dict[str, str] = {}
    if _has_table(conn, "buyer_marks"):
        for r in conn.execute("SELECT username, kind FROM buyer_marks WHERE username != '' AND kind IN "
                              "('no_pay', 'chargeback', 'disputed') ORDER BY kind = 'no_pay'"):
            out.setdefault(r["username"], BAD_NO_PAY if r["kind"] == "no_pay" else BAD_DISPUTE)
    if _has_table(conn, "buyer_blacklist"):
        for r in conn.execute("SELECT username FROM buyer_blacklist"):
            out[r["username"]] = BAD_LISTED
    return out


def bad_deal(conn, user: str) -> str | None:
    return bad_deals(conn).get(key(user))


def exclusion(c: dict, now: float, window_days: int = WINDOW_DAYS, cooldown_days: int = COOLDOWN_DAYS) -> str | None:
    """Почему этому человеку сейчас нельзя писать (или None — можно)."""
    if c.get("opted_out_ts"):
        return "просил(а) не писать" + (f" ({c['opt_out_reason']})" if c.get("opt_out_reason") else "")
    if c.get("bad_deal"):
        return c["bad_deal"]
    if c.get("is_minor") == 1:
        return "младше 18 по данным DeviantArt"
    last = c.get("last_interaction_ts")
    if not last or now - last > window_days * DAY:
        return f"не было дел с тобой {window_days} дн"
    lc = c.get("last_contact_ts")
    if lc and now - lc < cooldown_days * DAY:
        return f"ты писала {int((now - lc) // DAY)} дн назад (пауза {cooldown_days} дн)"
    ld = c.get("last_drafted_ts")
    if ld and now - ld < cooldown_days * DAY:
        return (f"текст ему(ей) готовила {int((now - ld) // DAY)} дн назад — могла отправить, не отметив "
                f"(пауза {cooldown_days} дн)")
    return None


def _drafted_map(conn) -> dict[str, float]:
    """Ник → когда ей последний раз готовили ему личное сообщение/ответ (pending или sent в outreach_log).
    Неподтверждённый черновик тоже держит паузу: она могла отправить и не отметить."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outreach_log'").fetchone():
        return {}
    return {r[0]: r[1] for r in conn.execute("SELECT username, MAX(ts) FROM outreach_log "
                                             "WHERE status IN ('pending', 'sent') GROUP BY username")}


def eligible(conn, now: float | None = None, window_days: int = WINDOW_DAYS,
             cooldown_days: int = COOLDOWN_DAYS) -> list[dict]:
    """Все, кому можно предложить: общался(ась) с ней за window_days, не просил(а) не писать, не младше 18
    (неизвестный возраст — можно, с пометкой age_unknown), она не писала ему(ей) cooldown_days."""
    now = now or time.time()
    ensure(conn)
    out = []
    drafted = _drafted_map(conn)
    bad = bad_deals(conn)
    for row in conn.execute("SELECT * FROM contacts WHERE last_interaction_ts IS NOT NULL "
                            "ORDER BY last_interaction_ts DESC"):
        c = _as_dict(row)
        c["last_drafted_ts"] = drafted.get(c["username"])
        c["bad_deal"] = bad.get(c["username"])
        if exclusion(c, now, window_days, cooldown_days) is None:
            c["age_unknown"] = c.get("is_minor") is None
            out.append(c)
    return out


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "—"
    d = int(max(0, now - ts) // DAY)
    return "сегодня" if d == 0 else f"{d} дн"


def _date(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%d.%m.%Y")


def interest_words(interests: dict[str, int], limit: int = 3) -> list[str]:
    top = sorted(interests.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [k.split(":", 1)[-1] for k, _ in top]


def render_crm(conn, now: float | None = None, limit: int = 20, window_days: int = WINDOW_DAYS,
               cooldown_days: int = COOLDOWN_DAYS) -> str:
    """Короткий список её контактов: роли, интересы, когда последний раз были дела и когда она писала,
    и почему кому-то сейчас писать нельзя."""
    now = now or time.time()
    ensure(conn)
    rows = [_as_dict(r) for r in conn.execute(
        "SELECT * FROM contacts WHERE last_interaction_ts IS NOT NULL OR opted_out_ts IS NOT NULL "
        "ORDER BY COALESCE(last_interaction_ts, 0) DESC")]
    drafted = _drafted_map(conn)
    bad = bad_deals(conn)
    for c in rows:
        c["last_drafted_ts"] = drafted.get(c["username"])
        c["bad_deal"] = bad.get(c["username"])
    if not rows:
        return ("Контактов пока нет: ни продаж, ни ставок на её лотах, ни пинглиста, ни фавов в базе. "
                "Вывода нет (n=0, источник: crm).")
    ok = [c for c in rows if exclusion(c, now, window_days, cooldown_days) is None and dm_ok(c)]
    lines = [f"Тёплые контакты: {len(rows)}, предложить лично можно {len(ok)}."]
    for c in rows[:max(1, limit)]:
        roles = ", ".join(ROLE_NAMES.get(r, r) for r in c["roles"]) or "—"
        likes = ", ".join(interest_words(c["interests"])) or "интересы неизвестны"
        bits = [f"{c['display']} — {roles}; любит: {likes}",
                f"дела: {_ago(c.get('last_interaction_ts'), now)}, писала: {_ago(c.get('last_contact_ts'), now)}"]
        why = exclusion(c, now, window_days, cooldown_days)
        if why:
            bits.append(f"нельзя: {why}")
        elif not set(c["roles"]) & set(DM_ROLES):
            bits.append("лично не пишем: не покупал(а), не ставил(а) и не в пинглисте")
        elif not dm_ok(c):
            bits.append("лично не пишем: покупал(а) вне DeviantArt и Toyhouse — ник там может быть чужим")
        elif c.get("is_minor") is None:
            bits.append("возраст неизвестен")
        lines.append("· " + "; ".join(bits))
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    ts = [c["last_interaction_ts"] for c in rows if c.get("last_interaction_ts")]
    period = f"{_date(min(ts))}–{_date(max(ts))}" if ts else "период неизвестен"
    lines.append(f"(n={len(rows)}, {period}, источник: crm — её продажи, ставки на её лотах, пинглист, фавы, "
                 f"наблюдение; окно {window_days} дн, пауза {cooldown_days} дн)")
    return "\n".join(lines)
