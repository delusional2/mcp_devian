"""Защита: проверить покупателя перед передачей перса и поискать, не выложил ли кто её дизайн как свой.

Покупатель. Проверяем то, что знаем сами: её чёрный список (с причиной), её отметки по прошлым сделкам
(оплатил, не заплатил, оспорил, вернули деньги, возврат через банк), журнал продаж и, если DeviantArt
подключён, возраст аккаунта DA (один запрос, кэш на неделю, в общем бюджете da_access). Публичные списки
мошенников мы НЕ читаем: даём ссылки (их можно открыть вкладками в её Chrome), смотрит она сама.
Итог всегда говорит, что проверено, а что нет: «упоминаний нет — это не гарантия».
Ничего не публикуем и никому не пишем; её чёрный список — только для неё.

Украденный дизайн. Строим ссылки обратного поиска картинки (Яндекс, Google Lens, TinEye, Bing) и можем
открыть их вкладками в её Chrome — результаты не читаем. Для жалобы даём структуру и факты из базы
(ссылки, даты публикации, когда помощник впервые увидел лот, продажи), а сам текст — её. Для Toyhouse
готового текста нет вовсе: правило 4.8 запрещает ИИ-текст, включая переводы.

Источники — из исследования к 0.5 (ссылки ниже рядом с константами). Пункты, которые не удалось проверить
напрямую (Toyhouse отвечал 403), помечены «по анонсу» / «не проверено».
"""

from __future__ import annotations

import ipaddress
import json
import re
import statistics
import subprocess
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse

from . import evidence as ev

DAY = 86400


class ProtectionError(ValueError):
    pass


# --- Источники и оговорки ---

BOOSTY_FAQ = "https://boosty.to/boosty/posts/de1e6f32-3a80-4824-95bd-a264ff1b00aa"
CARD_DISPUTE_NOTE = ("оплату картой (и через Boosty) покупатель может оспорить через свой банк — обычно примерно "
                     "в течение 120 дней; если возврат придёт после вывода, Boosty может заморозить эту сумму на "
                     f"балансе на 75–90 дней (FAQ Boosty: {BOOSTY_FAQ}). Полностью защититься нельзя, "
                     "но доказательства решают")
TH_SERVICE_REVIEWS = "https://toyhou.se/~forums/5867.service-reviews"
TH_SERVICE_RULES = "https://toyhou.se/~forums/5867.service-reviews/43283.read-first-service-review-rules"
TH_RULES = "https://toyhou.se/~rules"
TH_CODE_OF_CONDUCT = "https://toyhou.se/~pub/code-of-conduct-2610"
TH_BLACKLIST_NOTE = ("на Toyhouse у дизайнера есть свой чёрный список: профиль человека → меню у кнопки Block; "
                     "тогда ему нельзя передать персов, где ты дизайнер (по анонсу Toyhouse, осень 2026). "
                     "Добавляешь сама — помощник этого не делает")
DA_COPYRIGHT_POLICY = "https://www.deviantart.com/about/policy/copyright/"
DA_DMCA_FORM = "https://www.deviantart.com/contact-us?menuId=copyright_dmca_holder#issue-response"
DA_DMCA_EMAIL = "violations@deviantart.com"
X_IP_FORM = "https://help.x.com/forms/ipi"
SCAM_HEURISTIC_SRC = "обсуждения продавцов: забаненные заводят новые аккаунты"

AFTER_DISPUTE = (
    "Что можно сделать после возврата или спора:\n"
    "- Сохрани доказательства: где договорились (комментарий/записка DA), запись в Boosty, дату, сумму, ник.\n"
    f"- Boosty: смотри «Мои доходы → Возвраты»; если деньги заморожены дольше 20–30 дней — пиши в поддержку "
    f"({BOOSTY_FAQ}). Boosty споры не решает — решает банк покупателя.\n"
    f"- Toyhouse: {TH_BLACKLIST_NOTE}.\n"
    f"- Toyhouse Service Reviews: отзыв о сделке с доказательствами, только по делу, без обвинений сверх фактов "
    f"(правила: {TH_SERVICE_RULES}). Пишешь сама, если решишь.\n"
    "- Перс остаётся с тобой как с дизайнером; в твоём чёрном списке помощник предупредит, если этот ник снова "
    "придёт за покупкой.")


# --- Хранилище ---

SCHEMA = """
CREATE TABLE IF NOT EXISTS buyer_blacklist (
    username TEXT PRIMARY KEY,     -- ник в нижнем регистре, без @
    display TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'any',
    reason TEXT NOT NULL,
    added_ts REAL NOT NULL
);
-- Её отметки по сделкам с человеком: оплатил, не заплатил, оспорил, вернули, возврат через банк.
CREATE TABLE IF NOT EXISTS buyer_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    kind TEXT NOT NULL,
    sale_id TEXT,
    note TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS buyer_marks_user ON buyer_marks(username, ts);
-- Что сказал DeviantArt про аккаунт (кэш, чтобы не тратить запросы).
CREATE TABLE IF NOT EXISTS buyer_da_profiles (
    username TEXT PRIMARY KEY,
    found INTEGER NOT NULL,
    user_type TEXT,
    joined_ts REAL,
    is_minor INTEGER,
    deviations INTEGER,
    comments INTEGER,
    checked_ts REAL NOT NULL
);
-- Публичные страницы для ручной проверки: её список, можно добавить свои и убрать встроенные.
CREATE TABLE IF NOT EXISTS scam_lists (
    url TEXT PRIMARY KEY,          -- может содержать {user}
    title TEXT NOT NULL,
    note TEXT,
    builtin INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    added_ts REAL NOT NULL
);
"""

DEFAULT_SCAM_LISTS = (
    (TH_SERVICE_REVIEWS, "Toyhouse: Service Reviews",
     "отзывы о сделках, в том числе PSA о возвратах платежа; ищи ник в заголовках тредов"),
    ("https://www.deviantart.com/artists-beware", "DeviantArt: Artists-Beware",
     "группа-предупреждение; записи в основном 2018–19 и не проверены"),
    ("https://www.deviantart.com/{user}", "Профиль DeviantArt покупателя",
     "давно ли аккаунт, есть ли работы и живые комментарии"),
    ("https://toyhou.se/{user}", "Профиль Toyhouse покупателя", "давно ли аккаунт, есть ли персы и история"),
)

MARKS = {"paid": "оплатил(а)", "no_pay": "не заплатил(а)", "disputed": "открыл(а) спор по платежу",
         "refunded": "ты вернула деньги", "chargeback": "возврат через банк (чарджбэк)"}
# Возврат, который сделала она сама (не успела, передумала), — не вина покупателя: только история, без тревоги.
BAD_MARKS = ("chargeback", "disputed")
NEUTRAL_MARKS = ("refunded",)
PLATFORM_ALIASES = {
    "deviantart": "deviantart", "da": "deviantart", "deviant art": "deviantart", "deviantart.com": "deviantart",
    "девиантарт": "deviantart",
    "toyhouse": "toyhouse", "th": "toyhouse", "toyhou.se": "toyhouse", "toy house": "toyhouse",
    "тойхаус": "toyhouse", "тх": "toyhouse",
    "x": "x", "twitter": "x", "x.com": "x", "твиттер": "x",
}
SEVERITY = ("ok", "caution", "high", "stop")
SEVERITY_TEXT = {
    "stop": "СТОП: не передавай, пока не разберёшься.",
    "high": "Серьёзный повод насторожиться — передавай только после оплаты и с сохранёнными доказательствами.",
    "caution": "Есть на что посмотреть, прежде чем передавать.",
    "ok": "По твоим данным ничего тревожного.",
}
USERNAME = re.compile(r"^[A-Za-z0-9_.\-]{2,40}$")
NEW_ACCOUNT_DAYS = 30
PROFILE_TTL_DAYS = 7
BIG_AMOUNT_RATIO = 2.0


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    now = time.time()
    conn.executemany("INSERT OR IGNORE INTO scam_lists (url, title, note, builtin, hidden, added_ts) "
                     "VALUES (?, ?, ?, 1, 0, ?)", [(u, t, n, now) for u, t, n in DEFAULT_SCAM_LISTS])
    conn.commit()


def norm_user(user: str | None) -> tuple[str, str]:
    """(ключ в нижнем регистре, как написала). Ник — буквы, цифры, _ . -"""
    display = str(user or "").strip().lstrip("@").strip()
    if not USERNAME.match(display):
        raise ProtectionError("Ник — 2–40 символов: латиница, цифры, _ . -")
    return display.lower(), display


def _date(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d.%m.%Y") if ts else "?"


def norm_platform(platform: str | None) -> str:
    """deviantart | toyhouse | x | "" (не знаем). Регистр и привычные сокращения — не важны."""
    p = " ".join(str(platform or "").strip().lower().split())
    return PLATFORM_ALIASES.get(p, "")


# --- Её чёрный список ---

def blacklist_add(conn, user: str, reason: str, platform: str = "any", now: float | None = None) -> dict:
    ensure(conn)
    key, display = norm_user(user)
    reason = (reason or "").strip()
    if not reason:
        raise ProtectionError("Нужна причина — коротко, для тебя самой (её никто, кроме тебя, не увидит)")
    if platform not in ("any", "deviantart", "toyhouse", "x"):
        raise ProtectionError("Площадка: any, deviantart, toyhouse, x")
    conn.execute("INSERT OR REPLACE INTO buyer_blacklist (username, display, platform, reason, added_ts) "
                 "VALUES (?, ?, ?, ?, ?)", (key, display, platform, reason[:300], now or time.time()))
    conn.commit()
    return {"username": key, "display": display, "platform": platform, "reason": reason[:300]}


def blacklist_remove(conn, user: str) -> bool:
    ensure(conn)
    key, _ = norm_user(user)
    cur = conn.execute("DELETE FROM buyer_blacklist WHERE username = ?", (key,))
    conn.commit()
    return cur.rowcount > 0


def blacklist(conn) -> list[dict]:
    ensure(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM buyer_blacklist ORDER BY added_ts DESC")]


def _blacklisted(conn, key: str):
    return conn.execute("SELECT * FROM buyer_blacklist WHERE username = ?", (key,)).fetchone()


# --- Её отметки по сделкам ---

def mark(conn, user: str, kind: str, sale_id: str = "", note: str = "", now: float | None = None) -> int:
    """Её отметка о человеке. С sale_id ник можно не называть — берём покупателя из журнала продаж."""
    ensure(conn)
    if kind not in MARKS:
        raise ProtectionError(f"Отметка: {', '.join(MARKS)}")
    sale_id = (sale_id or "").strip()
    if sale_id:
        row = conn.execute("SELECT buyer FROM sales WHERE id = ?", (sale_id,)).fetchone()
        if not row:
            raise ProtectionError("Такой продажи нет")
        user = user or row["buyer"] or ""
    key, _ = norm_user(user)
    note = (note or "").strip()[:300]
    if sale_id:
        # Отметка по этой продаже уже есть, но без ника (в журнале не было ника) — дописываем ник в неё.
        orphan = conn.execute("SELECT id FROM buyer_marks WHERE sale_id = ? AND username = '' ORDER BY id DESC",
                              (sale_id,)).fetchone()
        if orphan:
            conn.execute("UPDATE buyer_marks SET username = ?, kind = ?, note = COALESCE(NULLIF(?, ''), note) "
                         "WHERE id = ?", (key, kind, note, orphan["id"]))
            conn.commit()
            return int(orphan["id"])
    cur = conn.execute("INSERT INTO buyer_marks (username, kind, sale_id, note, ts) VALUES (?, ?, ?, ?, ?)",
                       (key, kind, sale_id or None, note, now or time.time()))
    conn.commit()
    return int(cur.lastrowid or 0)


def _marked_sale_ids(conn) -> set[str]:
    return {r["sale_id"] for r in conn.execute("SELECT sale_id FROM buyer_marks WHERE sale_id IS NOT NULL")}


def unmark(conn, mark_id: int) -> bool:
    ensure(conn)
    cur = conn.execute("DELETE FROM buyer_marks WHERE id = ?", (int(mark_id),))
    conn.commit()
    return cur.rowcount > 0


def _marks(conn, key: str) -> list:
    return conn.execute("SELECT * FROM buyer_marks WHERE username = ? ORDER BY ts", (key,)).fetchall()


def _ledger(conn, key: str) -> list:
    try:
        return conn.execute("SELECT * FROM sales WHERE lower(ltrim(trim(buyer), '@')) = ? ORDER BY created_ts",
                            (key,)).fetchall()
    except Exception:  # noqa: BLE001 — в старой базе нет таблицы продаж
        return []


# --- Публичные страницы для ручной проверки ---

_BAD_HOST_SUFFIX = (".local", ".localhost", ".localdomain", ".internal", ".lan", ".home", ".arpa")
# Последняя часть домена — только буквы (как в OPEN_TABS_JXA): так не пройдут 127.1, 0x7f.1 и подобные.
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def safe_url(url: str, allow_placeholder: bool = False) -> str | None:
    """Только https на публичный домен: без логина в адресе, без IP, localhost и пробелов. Иначе None.
    Домен — латиницей, последняя часть из букв, без точки в конце (то же правило, что в OPEN_TABS_JXA)."""
    url = (url or "").strip()
    probe = url.replace("{user}", "x") if allow_placeholder else url
    if not probe or len(probe) > 2048 or re.search(r"[\s\"'<>\\]", probe):
        return None
    try:
        p = urlparse(probe)
        port = p.port
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host or p.username or p.password or port not in (None, 443):
        return None
    if host == "localhost" or host.endswith(_BAD_HOST_SUFFIX) or not _HOST.match(host):
        return None
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None
    except ValueError:
        pass
    return url


def scam_list_add(conn, url: str, title: str, note: str = "", now: float | None = None) -> None:
    """Добавить страницу для ручной проверки. {user} в адресе заменится ником покупателя."""
    ensure(conn)
    if not safe_url(url, allow_placeholder=True):
        raise ProtectionError("Нужна обычная ссылка https:// на публичный сайт")
    if not (title or "").strip():
        raise ProtectionError("Нужно название — как эту страницу показывать")
    conn.execute("INSERT INTO scam_lists (url, title, note, builtin, hidden, added_ts) VALUES (?, ?, ?, 0, 0, ?) "
                 "ON CONFLICT(url) DO UPDATE SET title = excluded.title, note = excluded.note, hidden = 0",
                 (url.strip(), title.strip()[:120], (note or "").strip()[:300], now or time.time()))
    conn.commit()


def scam_list_remove(conn, url: str) -> bool:
    """Свою — удалить; встроенную — спрятать (не вернётся при обновлении)."""
    ensure(conn)
    row = conn.execute("SELECT builtin FROM scam_lists WHERE url = ?", ((url or "").strip(),)).fetchone()
    if not row:
        return False
    if row["builtin"]:
        conn.execute("UPDATE scam_lists SET hidden = 1 WHERE url = ?", (url.strip(),))
    else:
        conn.execute("DELETE FROM scam_lists WHERE url = ?", (url.strip(),))
    conn.commit()
    return True


def scam_lists(conn) -> list[dict]:
    ensure(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM scam_lists WHERE hidden = 0 ORDER BY builtin DESC, added_ts")]


_PROFILE_OF = {"https://www.deviantart.com/{user}": "deviantart", "https://toyhou.se/{user}": "toyhouse"}


def manual_check_links(conn, user: str, character_url: str = "", platform: str = "") -> list[dict]:
    """Ссылки, которые она открывает и читает сама: {title, url, note}. Мы эти страницы не читаем.
    platform известна — профиль с тем же ником на другой площадке не даём: там может быть другой человек."""
    from .th_characters import character_base

    _, display = norm_user(user)
    plat = norm_platform(platform)
    out = []
    for r in scam_lists(conn):
        if plat and _PROFILE_OF.get(r["url"], plat) != plat:
            continue
        url = r["url"].replace("{user}", quote(display, safe=""))
        if safe_url(url):
            out.append({"title": r["title"], "url": url, "note": r["note"] or ""})
    base = character_base(character_url) if character_url else None
    if base:
        out.append({"title": "История владельцев перса", "url": f"{base}/ownership",
                    "note": "проверь, что перс на тебе; пометку чёрного списка Toyhouse показывает здесь"})
    return out


# --- DeviantArt: возраст аккаунта (один запрос, кэш) ---

def _joined(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S"):
        try:
            d = datetime.strptime(value.strip(), fmt)
            return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()
        except ValueError:
            continue
    return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_da_profile(body: dict) -> dict:
    """Ответ GET /user/profile/{name}?expand=user.details → то, что нужно для проверки."""
    raw_user = body.get("user") if isinstance(body, dict) else None
    user: dict = raw_user if isinstance(raw_user, dict) else {}
    raw_details = user.get("details")
    details: dict = raw_details if isinstance(raw_details, dict) else {}
    raw_stats = body.get("stats") if isinstance(body, dict) else None
    stats: dict = raw_stats if isinstance(raw_stats, dict) else {}
    # Возраст DA отдаёт в user.details.age (если человек его не скрыл); is_minor — на случай, если поле есть.
    minor = user.get("is_minor")
    age = _int(details.get("age"))
    if isinstance(minor, bool):
        is_minor = int(minor)
    elif age is not None and 0 < age < 120:
        is_minor = int(age < 18)
    else:
        is_minor = None
    return {"found": 1 if user.get("username") or user.get("userid") else 0,
            "user_type": user.get("type") if isinstance(user.get("type"), str) else None,
            "joined_ts": _joined(details.get("joindate")),
            "is_minor": is_minor,
            "deviations": _int(stats.get("user_deviations")), "comments": _int(stats.get("user_comments"))}


def da_fetcher(client):
    """fetch(name) для check_buyer: один GET профиля через DAClient (бюджет и пауза da_access — внутри)."""
    from . import da_access

    def fetch(name: str) -> dict:
        with da_access.task("проверка покупателя"):
            raw = getattr(client, "user_profile_raw", None)
            if raw:
                return raw(name)
            return client._request("GET", f"/user/profile/{quote(name, safe='')}",  # noqa: SLF001
                                   params={"expand": "user.details"})
    return fetch


def _cached_profile(conn, key: str):
    return conn.execute("SELECT * FROM buyer_da_profiles WHERE username = ?", (key,)).fetchone()


def da_profile(conn, user: str, fetch=None, now: float | None = None) -> tuple[dict | None, str | None]:
    """(профиль, почему не проверили). Свежий кэш — без запроса; без fetch — только кэш."""
    from . import da_api

    now = now or time.time()
    key, display = norm_user(user)
    row = _cached_profile(conn, key)
    if row and now - row["checked_ts"] < PROFILE_TTL_DAYS * DAY:
        return dict(row), None
    # Старый кэш отдаём только с пометкой: за неделю статус аккаунта мог измениться.
    old = {**dict(row), "stale": True} if row else None
    if fetch is None:
        return (old, None) if old else (None, "аккаунт DeviantArt не проверяла: DeviantArt не подключён")
    try:
        prof = parse_da_profile(fetch(display))
    except da_api.NotConnected:
        return (old, None) if old else (None, "аккаунт DeviantArt не проверяла: DeviantArt не подключён")
    except da_api.RateLimited:
        return (old, None) if old else (
            None, "аккаунт DeviantArt не проверяла: DeviantArt просит притормозить, попробую позже")
    except da_api.DAApiError as e:
        if e.status in (400, 404):
            prof = {"found": 0, "user_type": None, "joined_ts": None, "is_minor": None, "deviations": None,
                    "comments": None}
        else:
            return (old, None) if old else (None, f"аккаунт DeviantArt не проверяла: {str(e)[:80]}")
    prof.update(username=key, checked_ts=now)
    conn.execute("INSERT OR REPLACE INTO buyer_da_profiles (username, found, user_type, joined_ts, is_minor, "
                 "deviations, comments, checked_ts) VALUES (:username, :found, :user_type, :joined_ts, :is_minor, "
                 ":deviations, :comments, :checked_ts)", prof)
    conn.commit()
    return prof, None


def _profile_findings(prof: dict, display: str, now: float,
                      same_person: bool = True) -> tuple[list[tuple[str, str, str]], list[str]]:
    """([(уровень, код, текст)], [что проверено]). Коды: missing, banned, new, minor, empty.

    same_person=False — площадка покупателя неизвестна: ник на DA может принадлежать другому человеку,
    поэтому находки не выше «осторожно» и с оговоркой."""
    found, checked = _profile_raw(prof, display, now)
    if prof.get("stale"):
        checked = [f"{c} — данные от {_date(prof.get('checked_ts'))}, старше недели" for c in checked]
    if not same_person:
        found = [(min(lv, "caution", key=SEVERITY.index), code,
                  f"если это тот же человек на DeviantArt: {text}") for lv, code, text in found]
    return found, checked


def _profile_raw(prof: dict, display: str, now: float) -> tuple[list[tuple[str, str, str]], list[str]]:
    found: list[tuple[str, str, str]] = []
    if not prof.get("found"):
        found.append(("caution", "missing", f"на DeviantArt ник {display} не нашёлся — может, удалён, переименован "
                                 "или это ник с другой площадки"))
        return found, ["аккаунт DeviantArt (не нашёлся)"]
    checked = ["аккаунт DeviantArt (возраст, статус)"]
    if prof.get("user_type") == "banned":
        found.append(("high", "banned", "DeviantArt показывает этот аккаунт как заблокированный (banned)"))
    joined = prof.get("joined_ts")
    if joined:
        days = max(0, int((now - joined) // DAY))
        if days < NEW_ACCOUNT_DAYS:
            found.append(("caution", "new", f"аккаунту DeviantArt {days} дн (с {_date(joined)}) — "
                          + ev.heuristic("у новых аккаунтов меньше истории, а забаненные заводят новые",
                                         SCAM_HEURISTIC_SRC)))
    else:
        checked[0] = "аккаунт DeviantArt (статус; дату регистрации DeviantArt не отдал)"
    if prof.get("is_minor") == 1:
        found.append(("caution", "minor", "по данным DeviantArt ему младше 18 — "
                      + ev.heuristic("платёж картой за несовершеннолетнего чаще оспаривают родители",
                                     "практика продавцов")))
    if prof.get("deviations") == 0 and prof.get("comments") == 0:
        found.append(("caution", "empty", "профиль DeviantArt пустой: ни работ, ни комментариев"))
    return found, checked


# --- Крупная ли сумма для неё ---

def _amount_check(conn, amount: float | None, currency: str | None,
                  exclude_sale_id: str = "") -> tuple[tuple[str, str] | None, str | None]:
    """(находка, «не проверила»). Сравниваем с её обычной ценой только при n ≥ MIN_SIDE.
    Обычная цена — только реально оплаченные продажи (gross, та же валюта), без проверяемой."""
    if amount is None or not currency:
        return None, None
    try:
        rows = conn.execute("SELECT amount, created_ts FROM sales WHERE currency = ? AND amount IS NOT NULL "
                            "AND paid_ts IS NOT NULL AND status IN ('paid', 'transferred', 'done') AND id != ? "
                            "AND COALESCE(item_id, '') NOT LIKE 'comm:%'",
                            (currency, exclude_sale_id or "")).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    from .sales import money

    if len(rows) < ev.MIN_SIDE:
        return None, ev.refuse(f"крупная ли сумма для тебя (продаж в {currency})", len(rows), ev.MIN_SIDE)
    vals = [r["amount"] for r in rows]
    med = statistics.median(vals)
    e = ev.span([r["created_ts"] for r in rows], "her_sales", f"её продажи в {currency}", currency, "observed",
                measure="price", value=med, interval=ev.median_ci(vals))
    if med > 0 and amount >= BIG_AMOUNT_RATIO * med:
        return ("caution", f"сумма {money(amount, currency)} в {amount / med:.1f} раза выше твоей обычной "
                           f"(медиана {money(med, currency)} {ev.tag(e)}) — крупную оплату тоже оспаривают"), None
    return None, None


# --- Проверка покупателя ---

def check_buyer(conn, user: str, *, platform: str = "", character_url: str = "", amount: float | None = None,
                currency: str | None = None, sale_id: str = "", fetch=None, now: float | None = None) -> dict:
    """Проверка перед передачей. platform: deviantart | toyhouse | x | "" (регистр и сокращения DA/TH — не важны).
    DeviantArt смотрим только для deviantart и для неизвестной площадки (тогда находки DA — «если это тот же
    человек», не выше «осторожно»). fetch(name) -> тело профиля DA (da_fetcher(client)) или None — только кэш.
    sale_id — продажа, которую передаёт: сумма/валюта берутся из неё, и она не входит в «обычную цену».

    {"buyer", "platform", "severity": ok|caution|high|stop, "findings", "good", "history", "checked",
     "not_checked", "links", "notes"}"""
    ensure(conn)
    now = now or time.time()
    key, display = norm_user(user)
    plat = norm_platform(platform)
    found: list[tuple[str, str]] = []
    good: list[str] = []
    history: list[str] = []
    checked = ["твой чёрный список", "твои отметки по сделкам", "журнал продаж"]
    not_checked: list[str] = []

    bl = _blacklisted(conn, key)
    if bl:
        found.append(("stop", f"{bl['display']} в твоём чёрном списке с {_date(bl['added_ts'])}: {bl['reason']}"))

    marks = _marks(conn, key)
    for m in marks:
        what = MARKS.get(m["kind"], m["kind"])
        note = f" — {m['note']}" if m["note"] else ""
        if m["kind"] in BAD_MARKS:
            found.append(("high", f"{_date(m['ts'])}: {what}{note}"))
        elif m["kind"] == "no_pay":
            found.append(("caution", f"{_date(m['ts'])}: {what}{note}"))
        elif m["kind"] in NEUTRAL_MARKS:
            history.append(f"{_date(m['ts'])}: {what}{note} — это не про покупателя; если возврат был из-за "
                           "него (спор, чарджбэк), поменяй отметку")
        else:
            good.append(f"твоя отметка {_date(m['ts'])}: {what}{note}")

    rows = _ledger(conn, key)
    paid = [r for r in rows if r["status"] in ("paid", "transferred", "done") and r["paid_ts"]]
    trust = [r for r in rows if r["status"] == "transferred" and not r["paid_ts"]]
    marked = _marked_sale_ids(conn)
    cancelled = [r for r in rows if r["status"] == "cancelled" and r["id"] not in marked]
    if paid:
        good.append(f"оплатил(а) у тебя {len(paid)} раз(а) ({_date(paid[0]['created_ts'])}–"
                    f"{_date(paid[-1]['created_ts'])}, по журналу продаж)")
    for r in trust:
        found.append(("caution", f"уже есть перс, переданный без оплаты: «{r['title']}» "
                                 f"({_date(r['created_ts'])}, [{r['id']}])"))
    if cancelled:
        found.append(("caution", f"отменённых продаж с ним/ней без твоей отметки: {len(cancelled)} (почему — "
                                 "журнал не хранит; если был возврат или спор, отметь — buyers:mark)"))
    if not rows and not bl and not marks:
        good.append("раньше у тебя не покупал(а) — истории нет ни хорошей, ни плохой")

    th_note = "аккаунт Toyhouse: у Toyhouse нет API — открой профиль сама (ссылка ниже)"
    if plat in ("toyhouse", "x"):
        not_checked.append(th_note if plat == "toyhouse" else
                           "аккаунт X: не смотрю — открой профиль сама; DeviantArt по этому нику не проверяла, "
                           "там может быть другой человек")
    else:
        prof, why = da_profile(conn, display, fetch=fetch, now=now)
        if prof:
            pf, pc = _profile_findings(prof, display, now, same_person=plat == "deviantart")
            found += [(lv, text) for lv, _, text in pf]
            checked += pc
            if not plat:
                not_checked.append("на какой площадке покупатель, не сказано: аккаунт DeviantArt с тем же ником "
                                   "может быть чужим (скажи площадку — проверю точнее)")
        elif why:
            not_checked.append(why)
        not_checked.append(th_note)

    sale_row = None
    if sale_id:
        try:
            sale_row = conn.execute("SELECT amount, currency FROM sales WHERE id = ?", (sale_id.strip(),)).fetchone()
        except Exception:  # noqa: BLE001
            sale_row = None
    if sale_row and amount is None:
        amount, currency = sale_row["amount"], sale_row["currency"]
    hit, refuse = _amount_check(conn, amount, currency, exclude_sale_id=(sale_id or "").strip())
    if hit:
        found.append(hit)
    if refuse:
        not_checked.append(refuse)
    not_checked.append("публичные списки мошенников и отзывы: я их не читаю — открой ссылки ниже")

    severity = max((lv for lv, _ in found), key=SEVERITY.index, default="ok")
    return {"buyer": display, "platform": plat, "severity": severity, "findings": [t for _, t in found],
            "good": good, "history": history, "checked": checked, "not_checked": not_checked,
            "links": manual_check_links(conn, display, character_url, plat),
            "notes": [CARD_DISPUTE_NOTE] + ([TH_BLACKLIST_NOTE] if plat in ("toyhouse", "") else [])}


def render_check(res: dict) -> str:
    lines = [f"Проверка покупателя {res['buyer']} перед передачей", f"Итог: {SEVERITY_TEXT[res['severity']]}"]
    if res["findings"]:
        lines.append("Нашла:")
        lines += [f"- {f}" for f in res["findings"]]
    if res["good"]:
        lines.append("Хорошее:")
        lines += [f"- {g}" for g in res["good"]]
    if res.get("history"):
        lines.append("Для сведения:")
        lines += [f"- {h}" for h in res["history"]]
    lines.append("Проверила: " + ", ".join(res["checked"]) + ".")
    lines.append("Не проверяла:")
    lines += [f"- {n}" for n in res["not_checked"]]
    if res["links"]:
        lines.append("Открой сама по ссылкам (эти страницы я не читаю):")
        lines += [f"- {lk['title']}: {lk['url']}" + (f" — {lk['note']}" if lk["note"] else "") for lk in res["links"]]
    lines.append("Упоминаний нет — это не гарантия: в публичных списках ищешь ты, а забаненные быстро меняют ники.")
    lines.append("Перед передачей: оплата видна в Boosty (не скриншот и не обещание), ник в журнале тот же, "
                 "договорённость сохранена.")
    lines += [f"Помни: {n}." for n in res["notes"]]
    return "\n".join(lines)


# --- Хуки: передача перса и статусы продаж ---

def transfer_warnings(conn, buyer: str, platform: str = "toyhouse", now: float | None = None) -> list[str]:
    """Хук перед передачей: только локальные данные и кэш DA, без сети. Пусто — ничего не нашли.

    platform — где передаёшь. Для Toyhouse (по умолчанию: передача персов идёт там) кэш DeviantArt НЕ смотрим:
    тот же ник на DA может быть другим человеком. Для deviantart — смотрим; для неизвестной — с оговоркой."""
    ensure(conn)
    now = now or time.time()
    try:
        key, display = norm_user(buyer)
    except ProtectionError:
        return []
    plat = norm_platform(platform)
    out = []
    bl = _blacklisted(conn, key)
    if bl:
        out.append(f"{bl['display']} в твоём чёрном списке: {bl['reason']}.")
    for m in _marks(conn, key):
        if m["kind"] in BAD_MARKS or m["kind"] == "no_pay":
            out.append(f"Раньше у {display}: {MARKS[m['kind']]} ({_date(m['ts'])}).")
    row = _cached_profile(conn, key) if plat in ("deviantart", "") else None
    if row:
        prof = dict(row)
        if now - prof["checked_ts"] >= PROFILE_TTL_DAYS * DAY:
            prof["stale"] = True
        out += [f"{t[0].upper()}{t[1:]}" + (f" (данные DA от {_date(prof['checked_ts'])})." if prof.get("stale")
                                          else ".")
                for _, code, t in _profile_findings(prof, display, now, same_person=plat == "deviantart")[0]
                if code in ("banned", "new", "minor")]
    if out:
        out.append("Подробнее — buyers:check.")
    return out


SALE_STATUS_MARKS = {"refunded": ("cancelled", "refunded"), "chargeback": ("cancelled", "chargeback"),
                     "disputed": (None, "disputed"), "no_pay": ("cancelled", "no_pay")}


def split_status(status: str) -> tuple[str | None, str | None]:
    """sales:status с её словами → (статус журнала или None — не менять, отметка или None)."""
    return SALE_STATUS_MARKS.get(status, (status, None))


def on_sale_status(conn, sale_id: str, status: str, note: str = "", now: float | None = None) -> str | None:
    """Хук после sales:status: возврат/спор/неоплата → её отметка о покупателе и что делать дальше."""
    """Никогда не падает из-за ника: если в журнале у продажи нет ника DA/Toyhouse (пусто, кириллица, пробелы),
    отметка всё равно сохраняется — по продаже, без ника, — и ответ просит назвать ник (buyers:mark допишет его
    в ту же отметку). Ошибка — только если такой продажи нет."""
    ensure(conn)
    _, kind = split_status(status)
    if not kind:
        return None
    sale_id = (sale_id or "").strip()
    row = conn.execute("SELECT buyer FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if not row:
        raise ProtectionError("Такой продажи нет")
    ask = ""
    try:
        norm_user(row["buyer"])
        mark(conn, "", kind, sale_id=sale_id, note=note, now=now)
    except ProtectionError:
        conn.execute("INSERT INTO buyer_marks (username, kind, sale_id, note, ts) VALUES ('', ?, ?, ?, ?)",
                     (kind, sale_id, (note or "").strip()[:300], now or time.time()))
        conn.commit()
        who = f"«{row['buyer']}»" if (row["buyer"] or "").strip() else "не указан"
        ask = (f"\nПокупатель в этой продаже {who} — это не ник DA/Toyhouse, поэтому предупредить в следующий раз "
               f"не смогу. Назови ник: buyers:mark, users=<ник>, ref={sale_id} — допишу его в эту отметку.")
    if kind == "no_pay":
        return "Отметила: не заплатил(а). Если снова придёт за покупкой — предупрежу." if not ask else \
            "Отметила по продаже: не заплатил(а)." + ask
    if kind == "refunded":
        return ("Отметила: ты вернула деньги. Покупателю это в минус не идёт; если возврат был из-за спора "
                "или чарджбэка — отметь так (sales:status chargeback / disputed).")
    return f"Отметила: {MARKS[kind]}. {CARD_DISPUTE_NOTE}.{ask}\n{AFTER_DISPUTE}"


# --- Её Chrome: только открыть вкладки ---

MAX_TABS = 6
# argv: сколько вкладок максимум, затем адреса. Скрипт ничего не выполняет на страницах и ничего не читает:
# открывает вкладки в её обычном окне и показывает первую новую. Адреса ещё раз проверяются здесь.
OPEN_TABS_JXA = r"""
function run(argv) {
  const ok = /^https:\/\/[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:\/[^\s"'<>\\]*)?$/;
  const max = Number(argv[0]) || 0;
  const urls = argv.slice(1).filter(function (u) { return ok.test(u) && u.length <= 2048; }).slice(0, max);
  const skipped = argv.length - 1 - urls.length;
  if (!urls.length) return JSON.stringify({state: 'nothing', opened: 0, skipped: skipped});
  const chrome = Application('Google Chrome');
  if (!chrome.running()) { chrome.launch(); }
  let modes = [];
  for (let i = 0; i < 20; i++) {
    try { modes = chrome.windows.mode(); } catch (e) { modes = []; }
    if (modes.indexOf('normal') >= 0) break;
    delay(0.25);
  }
  if (modes.indexOf('normal') < 0) {
    chrome.Window({mode: 'normal'}).make();
    delay(0.5);
    modes = chrome.windows.mode();
  }
  const win = chrome.windows.byId(chrome.windows.id()[modes.indexOf('normal')]);
  const before = win.tabs.length;
  let opened = 0;
  for (const u of urls) { win.tabs.push(chrome.Tab({url: u})); opened++; }
  try { win.activeTabIndex = before + 1; } catch (e) {}
  chrome.activate();
  return JSON.stringify({state: 'opened', opened: opened, skipped: skipped});
}
"""


def _tab_urls(urls) -> list[str]:
    out = []
    for u in urls or []:
        s = safe_url(u)
        if s and s not in out and "#" not in s:
            out.append(s)
    return out[:MAX_TABS]


def open_tabs_argv(urls) -> list[str]:
    """Команда osascript (строка; здесь не запускается). Только безопасные https-адреса, не больше MAX_TABS."""
    return ["osascript", "-l", "JavaScript", "-e", OPEN_TABS_JXA, str(MAX_TABS), *_tab_urls(urls)]


def open_tabs(urls, runner=None, lock_wait_s: float | None = None) -> dict:
    """Открыть ссылки вкладками в её Chrome — только по её просьбе. Страницы не читаем.

    {"state": opened|nothing, "opened", "skipped", "message"}; настройка Chrome — ChromeSetupError."""
    from . import her_chrome

    safe = _tab_urls(urls)
    if not safe:
        return {"state": "nothing", "opened": 0, "skipped": len(list(urls or [])),
                "message": "Нечего открывать: ссылки не прошли проверку (нужны https на публичный сайт)."}
    if not her_chrome.chrome_installed():
        raise her_chrome.ChromeSetupError(her_chrome.NOT_INSTALLED_HINT)
    wait = her_chrome.LOCK_WAIT_S if lock_wait_s is None else lock_wait_s
    try:
        with her_chrome.chrome_turn(wait):
            done = (runner or subprocess.run)(open_tabs_argv(safe), capture_output=True, text=True,
                                              timeout=her_chrome.LAUNCH_WAIT_S + her_chrome.ERROR_EXTRA_S, check=False)
    except subprocess.TimeoutExpired as e:
        raise her_chrome.ToyhouseError("Chrome не ответил вовремя (часть вкладок могла открыться)") from e
    except OSError as e:
        raise her_chrome.ToyhouseError(f"не получилось запустить osascript: {e}") from e
    if done.returncode != 0:
        raise her_chrome.classify_error(done.stderr or done.stdout)
    try:
        answer = json.loads(done.stdout.strip() or "{}")
    except json.JSONDecodeError:
        answer = {"state": "opened", "opened": len(safe)}
    n = int(answer.get("opened") or 0)
    answer["skipped"] = int(answer.get("skipped") or 0) + max(0, len(list(urls or [])) - len(safe))
    answer["message"] = (f"Открыла {n} вкладок в твоём Chrome. Я их не читаю — смотришь ты." if n
                         else "Ничего не открылось.")
    return answer


# --- Обратный поиск картинки ---

REVERSE_ENGINES = (
    ("Яндекс", "https://yandex.ru/images/search?rpt=imageview&url={u}", "из России обычно работает надёжнее всех"),
    ("Google Lens", "https://lens.google.com/uploadbyurl?url={u}", ""),
    ("TinEye", "https://tineye.com/search?url={u}", "ищет точные копии и перезаливы"),
    ("Bing", "https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{u}",
     "формат ссылки Bing не документирован — может не сработать"),
)
UPLOAD_PAGES = (
    ("Яндекс", "https://yandex.ru/images/"),
    ("Google Lens", "https://lens.google.com/"),
    ("TinEye", "https://tineye.com/"),
    ("Bing", "https://www.bing.com/visualsearch"),
)
TINEYE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp")


_PAGE_HOSTS = ("deviantart.com", "www.deviantart.com", "toyhou.se", "www.toyhou.se", "x.com", "twitter.com",
               "www.x.com", "www.twitter.com")
NEED_IMAGE_ADDRESS = ("это адрес страницы, а не картинки — поисковики по нему ничего не найдут. Открой работу, "
                      "правый клик по картинке → «Копировать адрес изображения» и пришли его")


def is_page_url(url: str) -> bool:
    """Страница DA/Toyhouse/X (работа, галерея, профиль), а не файл картинки: картинки там лежат на других
    адресах (wixmp.com у DA, f2.toyhou.se у Toyhouse, pbs.twimg.com у X)."""
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower()
    return host in _PAGE_HOSTS and not p.path.lower().endswith(TINEYE_EXT)


def check_image_url(url: str) -> tuple[str, list[str]]:
    """(адрес, оговорки). Адрес картинки должен быть публичным https — поисковики скачивают его сами."""
    s = safe_url(url)
    if not s:
        raise ProtectionError("Нужна публичная ссылка https:// на картинку (не файл, не локальный адрес)")
    if is_page_url(s):
        raise ProtectionError(NEED_IMAGE_ADDRESS[0].upper() + NEED_IMAGE_ADDRESS[1:])
    notes = []
    p = urlparse(s)
    if "token=" in (p.query or ""):
        notes.append("в ссылке есть token= — такие адреса DeviantArt могут перестать открываться; "
                     "если поиск не найдёт картинку, возьми адрес заново или ссылку с Toyhouse")
    path = p.path.lower()
    if "." in path.rsplit("/", 1)[-1] and not path.endswith(TINEYE_EXT):
        notes.append("TinEye принимает только JPEG, PNG, GIF, BMP, TIFF и WebP")
    return s, notes


def reverse_search_links(image_url: str) -> list[dict]:
    """[{engine, url, note}] — ссылки, которые она открывает сама. Сеть не трогаем, результаты не читаем."""
    s, _ = check_image_url(image_url)
    u = quote(s, safe="")
    return [{"engine": name, "url": tpl.format(u=u), "note": note} for name, tpl, note in REVERSE_ENGINES]


def _looks_local(ref: str) -> bool:
    return ref.startswith(("/", "~", "file:")) or bool(re.match(r"^[A-Za-z]:\\", ref))


def original_facts(conn, ref: str) -> dict:
    """Что база знает про её оригинал: {"url", "title", "image_url", "facts": [...], "notes": [...]}."""
    from .sales import DA_ART_URL
    from .th_characters import character_base, character_id

    out: dict[str, Any] = {"url": ref, "title": "", "image_url": None, "facts": [], "notes": [], "sales": []}
    m = DA_ART_URL.match(ref)
    if m:
        try:
            item = conn.execute("SELECT * FROM items WHERE id = ?", (f"da:{m.group(1)}",)).fetchone()
        except Exception:  # noqa: BLE001
            item = None
        if item:
            from .palette import thumb_url

            out["title"] = item["title"]
            try:
                extra = json.loads(item["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                extra = {}
            out["image_url"] = thumb_url(extra if isinstance(extra, dict) else {})
            if item["published_ts"]:
                out["facts"].append(f"опубликовано на DeviantArt {_date(item['published_ts'])} "
                                    "(дата DeviantArt; точнее — на странице работы)")
            out["facts"].append(f"помощник впервые увидел работу {_date(item['first_seen_ts'])} "
                                "(запись в её базе)")
            if out["image_url"]:
                out["notes"].append("ищу по картинке из ленты — это маленькое превью; по полноразмерной найдётся "
                                    "больше — можешь прислать её адрес")
        else:
            out["notes"].append("этой работы нет в базе — даты возьми со страницы работы")
        sale_filter = ("item_id = ?", f"da:{m.group(1)}")
    elif character_id(ref):
        out["url"] = character_base(ref) or ref
        sale_filter = ("character_url LIKE ?", f"%toyhou.se/{character_id(ref)}.%")
    else:
        return out
    try:
        rows = conn.execute(f"SELECT * FROM sales WHERE {sale_filter[0]} AND status != 'cancelled'",
                            (sale_filter[1],)).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        who = f" покупателю {r['buyer']}" if r["buyer"] else ""
        out["facts"].append(f"продан{who} {_date(r['created_ts'])} (журнал продаж, [{r['id']}])")
        out["sales"].append({"id": r["id"], "buyer": r["buyer"] or "", "created_ts": r["created_ts"]})
    return out


def theft_search(conn, ref: str, image_url: str = "", now: float | None = None) -> dict:
    """Ссылки обратного поиска для её работы. ref — лот DA, перс Toyhouse, адрес картинки или путь к файлу.

    {"ref", "local", "image_url", "links": [{engine, url, note}], "open_urls", "facts", "notes"}"""
    from .sales import DA_ART_URL
    from .th_characters import character_id

    ref = (ref or "").strip()
    image_url = (image_url or "").strip()
    res: dict[str, Any] = {"ref": ref, "local": False, "image_url": None, "links": [], "open_urls": [], "facts": [],
                           "notes": []}
    if not ref and not image_url:
        raise ProtectionError("Нужна ссылка на лот DA, перса Toyhouse или картинку — или путь к файлу")
    if ref and _looks_local(ref) and not image_url:
        res["local"] = True
        res["links"] = [{"engine": n, "url": u, "note": ""} for n, u in UPLOAD_PAGES]
        res["open_urls"] = [u for _, u in UPLOAD_PAGES]
        res["notes"].append("это файл на твоём компьютере: открой страницу поиска и перетащи файл в окно "
                            "(файл никуда не отправляю сама)")
        return res
    if ref and (DA_ART_URL.match(ref) or character_id(ref)):
        info = original_facts(conn, ref)
        res["facts"], res["notes"] = info["facts"], info["notes"]
        image_url = image_url or info["image_url"] or ""
        if not image_url:
            res["notes"].append("нужен адрес изображения: открой работу, правый клик по картинке → "
                                "«Копировать адрес изображения» и пришли его")
            return res
    elif ref and not image_url:
        image_url = ref
    if is_page_url(image_url):
        res["notes"].append(NEED_IMAGE_ADDRESS)
        return res
    s, notes = check_image_url(image_url)
    res["image_url"] = s
    res["notes"] += notes
    res["links"] = reverse_search_links(s)
    res["open_urls"] = [lk["url"] for lk in res["links"]]
    return res


def render_theft_search(res: dict) -> str:
    lines = ["Поиск копий твоей картинки"]
    if res["local"]:
        lines.append("Открой страницу и перетащи файл в окно поиска:")
    elif res["links"]:
        lines.append("Открой ссылки — результаты смотришь ты, я их не читаю:")
    lines += [f"- {lk['engine']}: {lk['url']}" + (f" — {lk['note']}" if lk["note"] else "") for lk in res["links"]]
    if res["facts"]:
        lines.append("Что есть в базе про оригинал (пригодится как доказательство):")
        lines += [f"- {f}" for f in res["facts"]]
    lines += [f"Заметка: {n}." for n in res["notes"]]
    lines.append("Нашла похожее — сохрани скриншот копии с адресом и датой, потом images:theft_report.")
    return "\n".join(lines)


# --- Набор для жалобы ---

_DA_OWNER = re.compile(r"^https?://(?:www\.)?deviantart\.com/([A-Za-z0-9_-]+)(?:/|$)", re.I)
# Профиль Toyhouse: toyhou.se/<ник>. Адреса персов (toyhou.se/123.name) и служебные (~...) — не профили.
_TH_PROFILE = re.compile(r"^https://(?:www\.)?toyhou\.se/(?!\d+(?:\.|/?$))([A-Za-z0-9_.-]+)/?$", re.I)

COMMON_STEPS = [
    "Сначала сохрани копию: скриншоты страницы с адресом и датой — удалённое потом не вернуть.",
    "Собери ссылки на свой оригинал: самую раннюю публикацию (DA или Toyhouse) с датой.",
    "Если с копии стёрта подпись или водяной знак — скриншот рядом с твоим оригиналом.",
    "Публично не обвиняй без доказательств (по анонсу Toyhouse, новые правила поведения запрещают это "
    "с 01.12.2026: " + TH_CODE_OF_CONDUCT + ").",
]
PLATFORM_STEPS = {
    "deviantart": [
        f"Форма DMCA DeviantArt (нужен вход): {DA_DMCA_FORM}; или письмо на {DA_DMCA_EMAIL} "
        f"(в правилах DA уведомление по почте просят приложить PDF-файлом — сверься со страницей правил).",
        f"Правила: {DA_COPYRIGHT_POLICY}. Подаёт только автор (или её представитель).",
        "В уведомлении шесть частей: подпись (полное имя), ссылки на твой оригинал, ссылки на копию, "
        "твои контакты, заявление о добросовестности, заявление о точности под страхом ответственности.",
        "Два последних заявления — твоё действие: отмечаешь сама и только если всё так. "
        "Заведомо ложная жалоба — ответственность по §512(f).",
        "Если у тебя есть DeviantArt Protect — загляни и в его уведомления: он ищет похожие работы сам "
        "(что именно он сканирует и как быстро DA разбирает жалобы — смотри у DA, здесь не проверено).",
        "Перерисовка дизайна (не перезалив) — случай спорнее: опиши, что именно совпадает.",
    ],
    "toyhouse": [
        "Report в боковом меню перса или профиля — или тикет HelpDesk.",
        "В тикете — ссылки на все доказательства: оригинал с датой, копия, скриншоты. "
        "Поддельные доказательства запрещены.",
        f"Текст тикета — только своими словами: правило 4.8 Toyhouse считает ИИ-текст (и переводы) украденным "
        f"контентом ({TH_RULES}). Поэтому готового текста здесь нет — только факты.",
        "Модерация бывает медленной: если тишина — вежливо напомни в том же тикете.",
    ],
    "x": [
        f"Форма X о нарушении авторских прав: {X_IP_FORM} (адрес не удалось проверить напрямую).",
        "Нужны ссылки на твой оригинал и на твит с копией.",
    ],
    "other": [
        "Найди на сайте раздел Report / DMCA / Contact и приложи ссылки на оригинал и копию.",
    ],
}

RU_SKELETON = (
    "Каркас — впиши своими словами то, что в скобках:\n"
    "Здравствуйте! Я автор дизайна «[название]». Мой оригинал: [ссылка], опубликован [дата].\n"
    "Копия: [ссылка на копию]. [Что совпадает: дизайн, перезалив, стёрта подпись — как ты это видишь.]\n"
    "Я не давала разрешения [выкладывать / выдавать за свой]. Прошу [убрать / вернуть подпись].\n"
    "[Твоё имя и контакт]"
)
EN_HEADINGS = {
    "deviantart": "Fields for the DeviantArt DMCA form",
    "x": "Fields for the X copyright report form",
    "other": "Fields for the site's copyright / DMCA report",
}
EN_SKELETON = (
    "{heading} (facts only; fill the brackets yourself):\n"
    "Original work: {original}\n"
    "Infringing work: {copy}\n"
    "Description: [what was copied — e.g. 'my adoptable design reuploaded as their own']\n"
    "Full name (signature): [your full legal name]\n"
    "Contact: [your email]\n"
    "Good-faith and accuracy statements: if the form asks for them, confirm them yourself only if true."
)


def _platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host == "deviantart.com" or host.endswith(".deviantart.com"):
        return "deviantart"
    if host == "toyhou.se" or host.endswith(".toyhou.se"):
        return "toyhouse"
    if host in ("x.com", "twitter.com") or host.endswith((".x.com", ".twitter.com")):
        return "x"
    return "other"


def report_kit(conn, original: str, copy_url: str, now: float | None = None) -> dict:
    """Структура жалобы и факты из базы. Площадка — по адресу копии. Текст жалобы — её.

    {"platform", "original", "copy", "facts", "checklist", "templates": {ru, en} (не для Toyhouse), "warnings"}"""
    now = now or time.time()
    copy = safe_url(copy_url)
    if not copy:
        raise ProtectionError("Нужна ссылка https:// на копию")
    orig = (original or "").strip()
    platform = _platform_of(copy)
    from .th_characters import character_id

    info: dict[str, Any] = (original_facts(conn, orig) if orig
                            else {"url": "", "facts": [], "notes": [], "sales": []})
    facts = [f"твой оригинал: {info['url'] or '[ссылка на оригинал]'}"] + info["facts"]
    facts.append(f"копия: {copy} (ты заметила {_date(now)} — сохрани скриншот с датой)")
    warnings = []
    orig_cid, copy_cid = character_id(orig), character_id(copy)
    if orig_cid and orig_cid == copy_cid:
        warnings.append("копия — это тот же перс Toyhouse, что и твой оригинал (тот же номер): жаловаться на него "
                        "как на кражу нельзя; если перс продан — он принадлежит покупателю, проверь "
                        "«История владельцев» (/ownership)")
    sold = info.get("sales") or []
    if sold:
        buyers = ", ".join(sorted({s["buyer"] for s in sold if s["buyer"]})) or "покупателю"
        warnings.append(f"этот перс продан ({buyers}, {_date(sold[-1]['created_ts'])}, по журналу продаж): "
                        "выкладывать его и перепродавать — обычно право владельца, в том числе следующего. "
                        "Жалоба на законного владельца — ложная (для DA это §512(f)); сначала проверь историю "
                        "владельцев и свои условия продажи — жалуйся, только если тебя убрали из дизайнеров или "
                        "кто-то выдаёт твой дизайн за свой")
    m = _DA_OWNER.match(copy) or _TH_PROFILE.match(copy)
    if m:
        owner = m.group(1).lower()
        bought = [r for r in _ledger(conn, owner) if r["status"] != "cancelled"]
        if bought:
            warnings.append(f"{m.group(1)} покупал(а) у тебя ({len(bought)} раз, последний — "
                            f"{_date(bought[-1]['created_ts'])}): если это купленный перс, выкладывать его — "
                            "его право; проверь, что это не он, и свои условия продажи")
    templates = {}
    if platform != "toyhouse":
        templates = {"ru": RU_SKELETON, "en": EN_SKELETON.format(heading=EN_HEADINGS[platform],
                                                                 original=info["url"] or "[link]", copy=copy)}
    return {"platform": platform, "original": info["url"], "copy": copy, "facts": facts,
            "checklist": COMMON_STEPS + PLATFORM_STEPS[platform], "templates": templates, "warnings": warnings,
            "notes": info["notes"]}


PLATFORM_NAMES = {"deviantart": "DeviantArt", "toyhouse": "Toyhouse", "x": "X", "other": "сайт с копией"}


def render_report_kit(kit: dict) -> str:
    lines = [f"Жалоба на копию: {PLATFORM_NAMES[kit['platform']]}. Подаёшь сама — я собираю факты и порядок."]
    lines += [f"Внимание: {w}." for w in kit["warnings"]]
    lines.append("Факты:")
    lines += [f"- {f}" for f in kit["facts"]]
    lines.append("Порядок:")
    lines += [f"{i}. {s}" for i, s in enumerate(kit["checklist"], 1)]
    if kit["templates"]:
        lines += ["", kit["templates"]["ru"], "", kit["templates"]["en"]]
    else:
        lines.append("Текст пишешь своими словами — так безопаснее для твоего аккаунта Toyhouse.")
    lines += [f"Заметка: {n}." for n in kit.get("notes") or []]
    return "\n".join(lines)
