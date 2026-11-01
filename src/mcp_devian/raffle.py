"""Раффл (розыгрыш) её адопта или арта — бесплатный вход, один способ участия, проверяемая жеребьёвка.

Правила, которые соблюдаем (DA и X): приз — адопт или арт, не деньги и не поинты (в 2015 DA снимал
раффлы на деньги/поинты); участие бесплатное; один аккаунт на человека; один способ входа (фав работы или
«+» в комментариях её журнала/работы); никаких «кто больше репостнет/напишет». «Подпишись, чтобы
участвовать» в правилах DA не описано — только как необязательное и с пометкой.

Жеребьёвка commit-reveal: при создании — секретное зерно seed и его sha256 (commitment), commitment она
публикует в правилах. После окончания: участники в нижнем регистре по алфавиту, list_hash = sha256 списка
(по строке на ник), победитель = HMAC-SHA256(ключ seed, сообщение list_hash) как число по модулю числа
участников. Seed публикуется с итогом — кто угодно может проверить (verify).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import datetime

from . import selling

SCHEMA = """
CREATE TABLE IF NOT EXISTS raffles (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prize TEXT NOT NULL,
    rules TEXT NOT NULL,
    entry_method TEXT NOT NULL,     -- fave | comment_plus
    watch_required INTEGER NOT NULL DEFAULT 0,
    deviation_id TEXT,              -- UUID её работы или журнала, где идёт раффл
    journal_url TEXT,
    ends_ts REAL NOT NULL,
    seed_hex TEXT NOT NULL,
    commitment TEXT NOT NULL,
    status TEXT NOT NULL,           -- open | ended | drawn
    created_ts REAL NOT NULL,
    winner TEXT,
    list_hash TEXT,
    drawn_ts REAL,
    read_ts REAL,                   -- когда начат текущий проход чтения участников
    read_dev TEXT,                  -- по какой работе он идёт
    read_offset INTEGER,            -- откуда продолжить (следующая страница API)
    read_done INTEGER               -- 1 — проход дочитан до конца (has_more = false)
);
-- Сырые записи текущего прохода: фавы и комментарии «+»/«-», по страницам, через несколько сборов.
CREATE TABLE IF NOT EXISTS raffle_raw (
    raffle_id TEXT NOT NULL,
    item_key TEXT NOT NULL,         -- id комментария или fave:<ник> — повторы при сдвиге страниц не считаем
    username TEXT NOT NULL,
    ts REAL,
    watching INTEGER,
    mark TEXT NOT NULL,             -- fave | + | -
    PRIMARY KEY (raffle_id, item_key)
);
CREATE TABLE IF NOT EXISTS raffle_entries (
    raffle_id TEXT NOT NULL,
    username TEXT NOT NULL,         -- как пишется
    ts REAL,
    valid INTEGER NOT NULL,
    reason TEXT,                    -- почему не участвует или на что обратить внимание
    PRIMARY KEY (raffle_id, username)
);
"""
TEMPLATE_SLOTS = {"raffle": ("title", "prize", "ends", "method", "rules", "commitment"),
                  "raffle_winner": ("winner", "title", "prize", "seed", "commitment", "list_hash", "count",
                                    "entrants", "verify")}
RAFFLE_REQUIRED = ("rules",)   # {ends} и {commitment} — по желанию: без них они есть в {rules}
WINNER_REQUIRED = ("winner", "seed", "list_hash")
METHODS = {"fave": "fave", "plus": "comment_plus", "comment_plus": "comment_plus", "+": "comment_plus"}
MAX_DAYS = 60
# Деньги и поинты: суммы («500 points», «20 USD», «500 руб») и слова-деньги. «Point the fox» и «Рубиновый
# дракон» — не деньги: «point» без числа и «руб» внутри слова не считаем.
MONEY_PRIZE = re.compile(
    r"\d[\d\s,.]*\s*(?:k\s*)?(?:points?|pts|usd|eur|руб\w*|р\.|поинт\w*|долл\w*|евро)(?!\w)"
    r"|\b(?:da|deviantart|core)\s+points?\b|\bpoints?\s+(?:prize|giveaway|raffle)\b"
    r"|\b(?:money|cash|dollars?|paypal)\b|\bруб(?:л\w*)?(?!\w)|\bпоинт|\bденьг|\bдоллар|\$|€|₽", re.I)
RANKED = re.compile(r"\b(?:most|больше\s+всех|больше\s+всего|top)\b.{0,30}?"
                    r"(?:repost|retweet|\brt\b|share|comment|коммент|репост|ретвит)", re.I | re.S)
PLUS = re.compile(r"^\s*\+(?!\s*\d)")
MINUS = re.compile(r"^\s*-\s*(?:$|withdraw|out|не\s+участвую)", re.I)
WATCH_FLAG = ("«правило DA не описано»: «подпишись, чтобы участвовать» в правилах DeviantArt не описано — "
              "в правилах раффла стоит как необязательное, участников без подписки не исключаю")
ALT_FLAG = "похожий ник у другого участника — возможно, второй аккаунт; решать ей"
LAW_NOTE = "Розыгрыши должны соответствовать законам её страны и стран участников — это не юридический совет."
VERIFY_RECIPE = ("How to verify: (1) sha256 of the seed text (its 32 hex characters as ASCII) must equal the "
                 "commitment published in the rules. (2) Write the entrants in lowercase without @, sorted A→Z, joined "
                 "with a newline, no newline at the end; list_hash = sha256 of that UTF-8 text, as lowercase hex. "
                 "(3) Winner index = HMAC-SHA256 with key = the seed hex text as ASCII (not decoded to bytes) and "
                 "message = the list_hash hex text as ASCII; read the hex digest as a number, mod {count}; "
                 "count from 0 in the sorted list.")
PAGE = 50
_READ_COLS = (("read_ts", "REAL"), ("read_dev", "TEXT"), ("read_offset", "INTEGER"), ("read_done", "INTEGER"))


class RaffleError(ValueError):
    pass


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(raffles)")}
    for col, typ in _READ_COLS:  # базы, созданные до чтения по страницам
        if col not in have:
            conn.execute(f"ALTER TABLE raffles ADD COLUMN {col} {typ}")


def commitment_of(seed_hex: str) -> str:
    return hashlib.sha256(seed_hex.encode("ascii")).hexdigest()


def list_hash(entrants) -> str:
    return hashlib.sha256("\n".join(entrants).encode("utf-8")).hexdigest()


def pick_index(seed_hex: str, lhash: str, n: int) -> int:
    digest = hmac.new(seed_hex.encode("ascii"), lhash.encode("ascii"), hashlib.sha256).hexdigest()
    return int(digest, 16) % n


def normalize_entrants(entrants) -> list[str]:
    return sorted({str(e).strip().lstrip("@").lower() for e in entrants if str(e).strip()})


def verify(seed: str, commitment: str, entrants, winner: str) -> bool:
    """Проверка жеребьёвки любым человеком: зерно совпадает с обещанным и даёт этого победителя."""
    names = normalize_entrants(entrants)
    if not names or commitment_of(seed) != str(commitment).lower():
        return False
    return names[pick_index(seed, list_hash(names), len(names))] == str(winner).strip().lstrip("@").lower()


def _fmt_end(ts: float, tz: str) -> str:
    from .posting import her_zone

    dt = datetime.fromtimestamp(ts, her_zone(tz))
    off = dt.strftime("%z")
    return f"{dt.strftime('%d.%m.%Y %H:%M')} {dt.tzname()} (UTC{off[:3]}:{off[3:]})"


def rules_text(method: str, ends_text: str, commitment: str, watch_required: bool,
               with_ends: bool = True, with_commitment: bool = True) -> str:
    """Правила по-английски. with_ends/with_commitment = False — когда в её шаблоне есть свои {ends}/{commitment}:
    тогда в правилах эти строки не повторяем (время и 64-значный хеш дважды в публичном тексте)."""
    how = ("fave this deviation" if method == "fave"
           else "comment “+” under this post (one comment is enough; “-” to withdraw)")
    lines = ["• Free to enter.",
             "• One entry per person, one account per person — alt accounts may be disqualified.",
             f"• How to enter: {how}."]
    lines.append(f"• Ends: {ends_text}. Later entries don't count." if with_ends
                 else "• Entries after the end don't count.")
    lines.append("• Everyone has the same chance: no extra entries for reposts, shares or comments.")
    lines.append(f"• Winner is drawn by a verifiable method. Commitment (sha256 of the secret seed): {commitment}. "
                 "The seed is revealed with the result." if with_commitment
                 else "• Winner is drawn by a verifiable method; the seed behind the commitment is revealed with "
                      "the result.")
    if watch_required:
        lines.append("• Optional: watching me is appreciated, but not required to win.")
    return "\n".join(lines)


def _slots_problem(text: str, kind: str, required) -> str:
    slots = re.findall(r"\{(\w*)\}", text or "")
    unknown = sorted({s for s in slots if s not in TEMPLATE_SLOTS[kind]})
    if unknown:
        return f"неизвестные поля {', '.join('{' + u + '}' for u in unknown)}"
    missing = [s for s in required if s not in slots]
    if missing:
        return f"в шаблоне нужны поля {' '.join('{' + m + '}' for m in missing)}"
    return ""


def create(conn, title: str, prize: str, ends, entry_method: str = "fave", watch_required: bool = False,
           template: str | None = None, tz: str = "Europe/Moscow", now: float | None = None) -> dict:
    """Новый раффл: {ok, id, text (черновик правил её шаблоном), commitment, flags} или {ok: False, reason}."""
    from .posting import parse_when

    ensure(conn)
    now = now or time.time()
    title, prize = " ".join(str(title or "").split()), " ".join(str(prize or "").split())
    if not prize:
        return {"ok": False, "reason": "Что разыгрываем? Нужен приз — её адопт или арт."}
    if MONEY_PRIZE.search(prize):
        return {"ok": False, "reason": "Приз — только адопт или арт, не деньги и не поинты: раффлы на деньги/поинты "
                                       "DeviantArt снимал. Пусть разыграет персонажа или рисунок."}
    method = METHODS.get(str(entry_method or "fave").strip().lower())
    if not method:
        return {"ok": False, "reason": "Способ участия один: fave (фав работы) или plus («+» в комментариях)."}
    try:
        ends_ts = parse_when(ends, tz, now)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    if ends_ts <= now + 3600:
        return {"ok": False, "reason": "Окончание должно быть в будущем (хотя бы через час)."}
    if ends_ts - now > MAX_DAYS * 86400:
        return {"ok": False, "reason": f"Раффл дольше {MAX_DAYS} дней — слишком долго, люди забудут."}
    template = (template if template is not None else selling.get_template(conn, "raffle")) or ""
    if not template.strip():
        return {"ok": False, "reason": "Нет её шаблона раффла: попроси текст её словами и сохрани set_template"
                                       "('raffle', …), поля {title} {prize} {ends} {method} {rules} {commitment}; "
                                       "{rules} обязательно (в нём уже есть конец и commitment)."}
    problem = _slots_problem(template, "raffle", RAFFLE_REQUIRED)
    if problem:
        return {"ok": False, "reason": f"Шаблон раффла: {problem}."}
    if RANKED.search(template):
        return {"ok": False, "reason": "В шаблоне похоже на «кто больше репостнет/напишет — выиграл»: так нельзя "
                                       "(X и DA считают это накруткой). У всех должен быть одинаковый шанс."}
    seed = secrets.token_hex(16)
    commitment = commitment_of(seed)
    ends_text = _fmt_end(ends_ts, tz)
    rules = rules_text(method, ends_text, commitment, bool(watch_required))
    slots = set(re.findall(r"\{(\w*)\}", template))
    public_rules = rules_text(method, ends_text, commitment, bool(watch_required),
                              with_ends="ends" not in slots, with_commitment="commitment" not in slots)
    method_text = "fave this deviation" if method == "fave" else "comment “+”"
    text = selling.render_template(template, {"title": title or prize, "prize": prize, "ends": ends_text,
                                              "method": method_text, "rules": public_rules, "commitment": commitment})
    raffle_id = "r" + uuid.uuid4().hex[:7]
    conn.execute("INSERT INTO raffles (id, title, prize, rules, entry_method, watch_required, ends_ts, seed_hex, "
                 "commitment, status, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                 (raffle_id, title or prize, prize, rules, method, int(bool(watch_required)), ends_ts, seed,
                  commitment, now))
    conn.commit()
    flags = ["Риск: вторые аккаунты и «пустые» участники — при сборе помечу похожие ники, решать ей.", LAW_NOTE]
    if watch_required:
        flags.insert(0, WATCH_FLAG)
    return {"ok": True, "id": raffle_id, "text": text, "commitment": commitment, "ends_ts": ends_ts,
            "ends_text": ends_text, "entry_method": method, "flags": flags}


def get(conn, raffle_id: str):
    ensure(conn)
    return conn.execute("SELECT * FROM raffles WHERE id = ?", (str(raffle_id or "").strip(),)).fetchone()


def attach(conn, raffle_id: str, deviation_id: str, url: str = "") -> bool:
    """Где идёт раффл: её журнал (после отправки черновика журнала) или её работа (для фавов)."""
    ensure(conn)
    cur = conn.execute("UPDATE raffles SET deviation_id = ?, journal_url = COALESCE(NULLIF(?, ''), journal_url) "
                       "WHERE id = ? AND status = 'open'", (deviation_id, url, raffle_id))
    conn.commit()
    return cur.rowcount > 0


def withdraw(conn, raffle_id: str, username: str, now: float | None = None) -> None:
    """Человек попросил не участвовать: при сборе и жеребьёвке его нет."""
    ensure(conn)
    conn.execute("INSERT OR REPLACE INTO raffle_entries (raffle_id, username, ts, valid, reason) "
                 "VALUES (?, ?, ?, 0, 'withdrew')", (raffle_id, str(username).strip().lstrip("@"), now or time.time()))
    conn.commit()


def _stem(name: str) -> str:
    return re.sub(r"[\d_\-.]+", "", name.lower())


def _read_pages(conn, r, client, pages: int, now: float) -> bool:
    """Дочитать участников страницами (≤ pages запросов за раз), продолжая с места прошлого сбора.

    Проход, начатый до окончания раффла, после окончания начинается заново: итог — только по проходу,
    который весь прочитан ПОСЛЕ окончания. Конец списка — по has_more от API, а не по «упёрлись в лимит».
    True — текущий проход дочитан."""
    rid, dev = r["id"], r["deviation_id"]
    restart = (r["read_ts"] is None or r["read_dev"] != dev
               or (r["read_ts"] < r["ends_ts"] <= now)
               or (bool(r["read_done"]) and now < r["ends_ts"]))
    if restart:
        conn.execute("DELETE FROM raffle_raw WHERE raffle_id = ?", (rid,))
        conn.execute("UPDATE raffles SET read_ts = ?, read_dev = ?, read_offset = 0, read_done = 0 WHERE id = ?",
                     (now, dev, rid))
        conn.commit()
        offset, done = 0, False
    else:
        offset, done = int(r["read_offset"] or 0), bool(r["read_done"])
    calls = 0
    while not done and calls < pages:
        calls += 1
        rows: list[tuple] = []
        if r["entry_method"] == "comment_plus":
            comments, nxt = client.comments_page(dev, offset, PAGE)
            for c in comments:
                mark = "-" if MINUS.match(c.text or "") else "+" if PLUS.match(c.text or "") else ""
                if mark and c.user:
                    rows.append((rid, f"c:{c.comment_id}", c.user, c.posted_ts, None, mark))
        else:
            faves, nxt = client.whofaved_page(dev, offset, PAGE)
            for f in faves:
                w = f.get("is_watching")
                rows.append((rid, "fave:" + f["username"].lower(), f["username"], f.get("ts") or 0.0,
                             None if w is None else int(bool(w)), "fave"))
        conn.executemany("INSERT OR IGNORE INTO raffle_raw (raffle_id, item_key, username, ts, watching, mark) "
                         "VALUES (?, ?, ?, ?, ?, ?)", rows)
        done = nxt is None
        offset = offset if nxt is None else int(nxt)
        conn.execute("UPDATE raffles SET read_offset = ?, read_done = ? WHERE id = ?", (offset, int(done), rid))
        conn.commit()  # прочитанное сохраняется, даже если следующая страница упадёт
    return done


def collect(conn, raffle_id: str, client, now: float | None = None, max_calls: int = 3, owner: str = "") -> dict:
    """Собрать участников с ЕЁ работы/журнала: {ok, entrants, flagged, excluded, complete, text}.

    comment_plus — комментарии «+» (последний «-» от того же человека — вышел); fave — кто фавнул.
    Чтение по страницам, с продолжением при следующем сборе (не больше max_calls запросов за раз).
    Повторы по нику (без регистра) — один раз; её саму, вышедших и пришедших после окончания не берём.
    «Не предлагай мне» (opt-out предложений) — не выход из раффла: такие люди участвуют как все.
    Похожие ники только помечаем. Пока проход после окончания не дочитан, раффл не считается законченным."""
    ensure(conn)
    now = now or time.time()
    r = get(conn, raffle_id)
    if not r:
        return {"ok": False, "text": "Такого раффла нет."}
    if not r["deviation_id"]:
        return {"ok": False, "text": "Не знаю, где идёт раффл: сначала опубликуй журнал (или привяжи её работу)."}
    owner = owner or str((client.whoami() or {}).get("username") or "")
    dev = r["deviation_id"]
    author = str((client.deviations_brief([dev]).get(dev) or {}).get("author") or "")
    if not owner or author.casefold() != owner.casefold():
        return {"ok": False, "text": "Это не её работа/журнал (или автора не видно) — участников не собираю."}
    pages = max(1, int(max_calls) - 1)
    complete = _read_pages(conn, r, client, pages, now)
    r = get(conn, raffle_id)
    final = complete and r["read_ts"] is not None and r["read_ts"] >= r["ends_ts"]
    found: dict[str, dict] = {}
    raw = conn.execute("SELECT username, ts, watching, mark FROM raffle_raw WHERE raffle_id = ? "
                       "ORDER BY COALESCE(ts, 0), item_key", (raffle_id,)).fetchall()
    for row in raw:
        key = (row["username"] or "").lower()
        if row["mark"] == "-":
            if key in found:
                found[key]["withdrew"] = True
        elif key not in found:
            w = row["watching"]
            found[key] = {"username": row["username"], "ts": row["ts"] or 0.0,
                          "watching": None if w is None else bool(w)}
    withdrew = {row[0].lower() for row in conn.execute("SELECT username FROM raffle_entries WHERE raffle_id = ? "
                                                       "AND reason = 'withdrew'", (raffle_id,))}
    out = withdrew
    entries: list[tuple[str, float, int, str]] = []
    for key, e in found.items():
        if key == owner.lower():
            continue
        if key in out or e.get("withdrew"):
            reason = "вышел(ла) из раффла"
            valid = 0
        elif e["ts"] and e["ts"] > r["ends_ts"]:
            valid, reason = 0, "после окончания"
        else:
            valid, reason = 1, ""
            if r["watch_required"] and e.get("watching") is False:
                reason = "не подписан(а) — правило необязательное, не исключаю"
        entries.append((e["username"], e["ts"], valid, reason))
    stems: dict[str, int] = {}
    for name, _, valid, _ in entries:
        s = _stem(name)
        if valid and len(s) >= 3:
            stems[s] = stems.get(s, 0) + 1
    entries = [(n, ts, v, (reason + "; " if reason else "") + ALT_FLAG if v and stems.get(_stem(n), 0) > 1 else reason)
               for n, ts, v, reason in entries]
    conn.execute("DELETE FROM raffle_entries WHERE raffle_id = ? AND COALESCE(reason, '') != 'withdrew'", (raffle_id,))
    conn.executemany("INSERT OR REPLACE INTO raffle_entries (raffle_id, username, ts, valid, reason) "
                     "VALUES (?, ?, ?, ?, ?)", [(raffle_id, n, ts, v, reason) for n, ts, v, reason in entries
                                                if n.lower() not in withdrew])
    if final and r["status"] == "open":
        conn.execute("UPDATE raffles SET status = 'ended' WHERE id = ? AND status = 'open'", (raffle_id,))
    conn.commit()
    valid_names = [n for n, _, v, _ in entries if v]
    flagged = [(n, reason) for n, _, v, reason in entries if v and reason]
    excluded = [(n, reason) for n, _, v, reason in entries if not v]
    lines = [f"Раффл {raffle_id}: участников {len(valid_names)}" + ("" if complete else " (прочитано не всё)")]
    lines += [f"  ⚠ {n}: {why}" for n, why in flagged]
    lines += [f"  — {n}: {why}" for n, why in excluded]
    if not complete:
        lines.append("Прочитала не всех — продолжу с этого места при следующем сборе; жеребьёвка только после "
                     "полного сбора.")
    elif now < r["ends_ts"]:
        lines.append("Раффл ещё идёт — жеребьёвка после окончания.")
    return {"ok": True, "entrants": valid_names, "flagged": flagged, "excluded": excluded, "complete": complete,
            "text": "\n".join(lines)}


def draw(conn, raffle_id: str, now: float | None = None) -> dict:
    """Жеребьёвка (только после окончания и полного сбора): {ok, winner, index, list_hash, seed, entrants}.
    Повторный вызов даёт тот же итог — перебросить нельзя."""
    ensure(conn)
    r = get(conn, raffle_id)
    if not r:
        return {"ok": False, "text": "Такого раффла нет."}
    if r["status"] not in ("ended", "drawn"):
        return {"ok": False, "text": "Раффл ещё не закончен или участники собраны не полностью — сначала сбор "
                                     "после окончания."}
    names = normalize_entrants(row[0] for row in conn.execute(
        "SELECT username FROM raffle_entries WHERE raffle_id = ? AND valid = 1", (raffle_id,)))
    if not names:
        return {"ok": False, "text": "Участников нет — разыгрывать некого."}
    lhash = list_hash(names)
    if r["status"] == "drawn" and r["list_hash"] != lhash:
        return {"ok": False, "text": "Список участников изменился после жеребьёвки — итог остаётся прежним: "
                                     f"{r['winner']}. Перебрасывать нельзя."}
    idx = pick_index(r["seed_hex"], lhash, len(names))
    winner = names[idx]
    if r["status"] != "drawn":
        conn.execute("UPDATE raffles SET status = 'drawn', winner = ?, list_hash = ?, drawn_ts = ? WHERE id = ?",
                     (winner, lhash, now or time.time(), raffle_id))
        conn.commit()
    return {"ok": True, "winner": winner, "index": idx, "list_hash": lhash, "seed": r["seed_hex"],
            "commitment": r["commitment"], "entrants": names, "title": r["title"], "prize": r["prize"]}


def announcement(conn, raffle_id: str, template: str | None = None) -> dict:
    """Текст итога её шаблоном raffle_winner: победитель, seed, list_hash и как проверить. Только черновик."""
    res = draw(conn, raffle_id)
    if not res["ok"]:
        return res
    template = (template if template is not None else selling.get_template(conn, "raffle_winner")) or ""
    if not template.strip():
        return {**res, "ok": False, "text": "Нет её шаблона итога: set_template('raffle_winner', …), поля {winner} "
                                            "{seed} {list_hash} обязательны, можно {entrants} {verify} {commitment}."}
    problem = _slots_problem(template, "raffle_winner", WINNER_REQUIRED)
    if problem:
        return {**res, "ok": False, "text": f"Шаблон итога: {problem}."}
    from . import buyers

    verify_text = VERIFY_RECIPE.format(count=len(res["entrants"]))
    # Просил(а) не отмечать — ник без @: итог видно, уведомления нет; написать ему(ей) — ей самой.
    quiet = buyers.is_opted_out(conn, res["winner"])
    text = selling.render_template(template, {
        "winner": ("" if quiet else "@") + res["winner"], "title": res["title"], "prize": res["prize"], "seed": res["seed"],
        "commitment": res["commitment"], "list_hash": res["list_hash"], "count": len(res["entrants"]),
        "entrants": ", ".join(res["entrants"]), "verify": verify_text})
    notes = []
    if quiet:
        notes.append("победитель(ница) просил(а) не отмечать — ник без @; сообщи о выигрыше сама")
    if "{entrants}" not in template:
        notes.append("без списка участников ({entrants}) проверить жеребьёвку нельзя — добавь его или опубликуй рядом")
    if "{verify}" not in template:
        notes.append("рецепт проверки ({verify}) стоит добавить, чтобы любой мог пересчитать")
    return {**res, "text": text, "notes": notes}


ANNOUNCE_NOTE = "рецепт проверки ({verify}, англ.)"


def prepare_announcement(conn, cfg, raffle_id: str, kind: str = "journal", now: float | None = None,
                         title: str = ""):
    """Черновик итога (журнал или статус) — уходит только по её «го». (id или None, текст).
    title — её название журнала; без него помощник предложит «Raffle results: …» и пометит это в превью."""
    from . import posting

    res = announcement(conn, raffle_id)
    if not res.get("ok"):
        return None, res.get("text") or "Не получилось."
    tpl = selling.get_template(conn, "raffle_winner") or ""
    parts = [ANNOUNCE_NOTE] if "{verify}" in tpl else []
    if kind == "status":
        draft_id, preview = posting.prepare_status(conn, cfg, res["text"], now=now, helper_note=", ".join(parts))
    else:
        her_title = " ".join(str(title or "").split())
        parts += ([] if her_title else ["название «Raffle results: …»"]) + ["теги raffle, results"]
        draft_id, preview = posting.prepare_journal(
            conn, cfg, (her_title or f"Raffle results: {res['title']}")[:posting.TITLE_MAX], res["text"],
            ["raffle", "results"], now=now, helper_note=", ".join(parts))
    quiet = [n for n in res.get("notes") or [] if "не отмечать" in n]
    return draft_id, preview + "".join(f"\n! {n}" for n in quiet) if draft_id else preview
