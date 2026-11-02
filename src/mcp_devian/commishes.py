"""ych.commishes.com: её аукционы адоптов и YCH, небольшая выборка рынка, советы — бережно и без входа.

Что проверено на сайте 02.11.2026 (tests/fixtures_commishes):

* Код в ссылке /auction/show/{код}/{slug}/ — номер аукциона в base32 (0-9A-V): int(код, 32).
* Публичный JSON /auction/show/{код}/.json (им пользуется сам сайт) — всё нужное за один запрос: SB, шаг,
  автобай, слоты, ставки со временем, конец (endsunix). Валюты в нём нет: все цены на сайте — в $.
* Страница аукциона — только ради вкладки-категории и значка «Sniping protection enabled — Bids in the last
  10 minutes extend the auction» (у каждого аукциона свой). Ключу settings «security.sniping_disabled» не
  верим: у аукциона со значком он «ON» — смысл не проверен.
* RSS: в общей ленте /home/index.rss у всех ссылок номер 0 — её не используем. Ленты категорий
  /category/{…}/index.rss — с настоящими номерами. Дата «Ends»/pubDate в лентах на 1–3 дня раньше настоящего
  конца — как конец не используем никогда. ttl ленты 1800 с.
* Профиль /user/{ник}/ — текущие аукционы карточками (номер, текущая ставка, конец). Прошлые — JSON
  /user/history/{ник}.json?until=… (видели только в коде сайта, форма ответа не проверена — разбираем осторожно).
* Правила сайта — короткие выдержки в tests/fixtures_commishes/site_rules.txt (баннер главной и скрипт формы
  ставки): AI-картинки запрещены; ставка окончательная, победитель обязан купить; ставка, равная уже сделанной,
  отбрасывается («Bid collission»); подсказка ставки — по таблице {0: 1, 5: 2.5, 10: 5, 50: 10, 100: 20,
  300: 50, 1000: 100}, где берётся шаг самой большой границы строго меньше суммы.

Этикет (robots.txt: Allow /, use=reference, ai-train=no): помощник одной художницы. Честный User-Agent
(USER_AGENT: без «Mozilla», с версией), не больше PER_HOUR запросов за скользящий час на всё (её аукционы, рынок, профиль),
не чаще раза в MIN_GAP_S, кэш, редиректы не проходим (значит, нужен вход — не наше), после 403/429/5xx и
сбоя сети — пауза (сбой сети тоже идёт в бюджет: запрос мог дойти). 4xx и редирект помним ERROR_TTL_S —
не спрашиваем тот же адрес на каждом проходе. Без входа, без websocket, ставок помощник не делает никогда
(адрес /bids/post.json закрыт в клиенте).
Чужие данные храним только для её аналитики и недолго (prune): JSON аукционов (там ники) — минуты,
лоты рынка — KEEP_MARKET_DAYS, без текста описаний.

Бюджет и паузы лежат в тех же таблицах, что у da_access (da_budget/da_cooldown), со своим kind="commishes":
новой схемы и миграций не нужно; da_access смотрит только на свои kind.
"""

from __future__ import annotations

import contextlib
import html as html_lib
import json
import logging
import re
import sqlite3
import statistics
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.entities import name2codepoint
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError as XmlParseError
from defusedxml.ElementTree import fromstring as safe_fromstring

from . import __version__, auctions, bids, checklist, classify, da_access, db, evidence, sales

log = logging.getLogger(__name__)

HOST = "ych.commishes.com"
BASE = f"https://{HOST}"
SOURCE = "commishes"          # items.source, sales.platform, runs.source
KIND = "commishes"            # da_budget.kind / da_cooldown.kind
PLACE = "ych.commishes"
# Без «Mozilla/Macintosh»: фильтры сайта не должны принимать помощника за браузер (robots.txt: use=reference).
USER_AGENT = f"mcp_devian/{__version__} (personal assistant of one seller; read-only, no bids, no login)"
CATEGORIES = ("furry", "brony", "human", "adopt", "crafts")
CATEGORY_NAMES = {"furry": "Furry", "brony": "Pony", "human": "Human", "adopt": "Adoptables", "crafts": "Crafts"}

# Бюджет: на всё про ych.commishes вместе.
PER_HOUR = 20
LOW_RESERVE = 6               # последние запросы часа — только её аукционам, не рынку
MIN_GAP_S = 3.0
WINDOW_S = 3600
COOLDOWN_STEPS_S = (30 * 60, 3600, 2 * 3600, 4 * 3600, 8 * 3600)
SERVER_PAUSE_S = 15 * 60      # 5xx: сайт прилёг — пауза без наращивания ступени
RETRY_AFTER_MAX_S = 24 * 3600
FORGET_LEVEL_S = 24 * 3600

# Кэш: RSS — по ttl ленты, профиль — столько же, страница аукциона (категория, значок) — надолго.
RSS_TTL_S = 1800
PROFILE_TTL_S = 1800
PAGE_TTL_S = 12 * 3600
JSON_TTL_S = 120              # чат и фон в одну минуту не читают один аукцион дважды
KEEP_CACHE_S = 86400
KEEP_JSON_S = 600             # тела JSON аукционов (с никами ставящих) дольше не держим
ERROR_TTL_S = 6 * 3600        # 4xx/редирект: тот же адрес снова не раньше чем через 6 ч
MARKET_ERROR_TTL_S = 7 * 86400  # чужой лот не читается — дольше прохода рынка (6 ч), иначе спросим снова
NET_PAUSE_S = 10 * 60         # сеть/таймаут: до конца прохода и ещё немного — не трогаем
KEEP_MARKET_DAYS = 35         # чужие лоты для статистики (смотрим 30 дней); её — не трогаем

# Её аукционы: когда перечитывать.
NEAR_END_S = 30 * 60
NEAR_STEP_S = 10 * 60
AFTER_END_S = 60
POST_END_MAX_S = 6 * 3600     # итог так и не виден — перечитываем всё реже, но не реже раза в 6 ч
FAR_REREAD_S = 6 * 3600       # если профиль не читали (только ссылки) — далеко от конца раз в 6 ч
GONE_REREAD_S = 2 * 3600      # лот пропал с профиля задолго до конца — проверить не чаще раза в 2 ч
ERR_GONE_AFTER = 4            # после конца JSON столько раз подряд не разобрался (410, не JSON…) — закрываем
MARKET_JSON_PER_RUN = 3       # итогов чужих лотов за проход (низкий приоритет)
MARKET_SAMPLE_DAYS = 30
PRUNE_SOURCE = "commishes_prune"   # runs.source: когда чистили последний раз (чат и фон — общий)
PRUNE_EVERY_S = 86400
# Дата «Ends» в RSS на 1–3 дня раньше настоящего конца (видели 1 и 3 дня): итог чужого лота читаем не раньше
# этой даты + 3 дня, а дальше — по endsunix из JSON. Как конец её не используем.
RSS_END_LAG_S = 3 * 86400

SITE_STEPS = ((1000, 100), (300, 50), (100, 20), (50, 10), (10, 5), (5, 2.5), (0, 1))
_B32 = "0123456789ABCDEFGHIJKLMNOPQRSTUV"
_CODE = re.compile(r"^[0-9A-Va-v]{1,13}$")
AUCTION_PATH = re.compile(r"^/auction/show/([0-9A-Va-v]{1,13})(?:/[^/?#]*)?/?$")
USERNAME = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9_.-]{1,40}$")  # не «..» (иначе путь /user/../)
# Пути, которые помощник никогда не трогает: ставки, подписки, вход.
FORBIDDEN_PATHS = re.compile(r"^/(?:bids|user/subscribe|account|login|logout)\b", re.I)

AI_RULE = "на сайте запрещены AI-картинки и нейросетевые фильтры (баннер ych.commishes, 02.11.2026)"
FINAL_RULE = "ставки окончательные: победитель обязан купить (правило сайта, форма ставки)"
EQUAL_BID_RULE = "ставку, равную уже сделанной, сайт отбрасывает"
LAVA_UNVERIFIED = ("Lava.top — не проверено: им пользуется другая продавщица с сайта (PayPal/карта), "
                   "условия для тебя не проверяла.")


class CommishesError(Exception):
    pass


class Unavailable(CommishesError):
    """4xx (кроме 403/429) или редирект (нужен вход): адрес не читается и, скорее всего, не начнёт.
    status — код ответа."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


@dataclass
class Block:
    why: str          # cooldown | budget | net
    until_ts: float
    limit: int | None = None

    @property
    def message(self) -> str:
        when = da_access.until_text(self.until_ts)
        if self.why == "cooldown":
            return f"ych.commishes просил притормозить — не трогаю до {when}"
        if self.why == "net":
            return f"ych.commishes не отвечает (сеть или таймаут) — попробую снова после {when}"
        return f"лимит запросов к ych.commishes ({self.limit} в час) на сейчас исчерпан — остальное после {when}"


class Throttled(CommishesError):
    """Запрос не отправлен (пауза или свой бюджет) или сайт только что попросил притормозить."""

    def __init__(self, block: Block):
        super().__init__(block.message)
        self.block = block


# --- номера и адреса ---

def code_to_id(code: str) -> int:
    if not _CODE.match(code or ""):
        raise ValueError(f"не похоже на код аукциона ych.commishes: {code!r}")
    return int(code, 32)


def id_to_code(n: int) -> str:
    if n < 0:
        raise ValueError("номер аукциона — неотрицательное число")
    out = ""
    while True:
        n, r = divmod(n, 32)
        out = _B32[r] + out
        if not n:
            return out


def auction_url(code: str) -> str:
    return f"{BASE}/auction/show/{code.upper()}/"


def json_path(code: str) -> str:
    return f"/auction/show/{code.upper()}/.json"


def page_path(code: str) -> str:
    return f"/auction/show/{code.upper()}/"


def parse_url(url: str) -> tuple[int, str] | None:
    """(номер, КОД) по ссылке на аукцион ych.commishes. Ссылки общей ленты с номером 0 — None."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != HOST:
        return None
    m = AUCTION_PATH.match(parts.path)
    if not m:
        return None
    code = m.group(1).upper()
    n = int(code, 32)
    return (n, code) if n else None


def _item_id(auction_id: int, slot_id: int | None = None) -> str:
    return f"cm:{auction_id}" if slot_id is None else f"cm:{auction_id}:{slot_id}"


# --- бережный клиент ---

def make_http(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}, timeout=20,
                        follow_redirects=False, transport=transport)


CACHE_SCHEMA = ("CREATE TABLE IF NOT EXISTS cm_cache (path TEXT PRIMARY KEY, ts REAL NOT NULL, body TEXT NOT NULL)")


def _ensure_cache(conn: sqlite3.Connection) -> None:
    conn.execute(CACHE_SCHEMA)


class Client:
    """Все запросы к ych.commishes.com. conn — база помощника (бюджет, пауза, кэш)."""

    def __init__(self, conn: sqlite3.Connection, http: httpx.Client | None = None,
                 clock: Callable[[], float] | None = None, sleep: Callable[[float], object] | None = None):
        self.conn = conn
        self.http = http or make_http()
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.sent = 0
        _ensure_cache(conn)

    # что можно

    def block(self, low: bool = False) -> Block | None:
        now = self.clock()
        row = self.conn.execute("SELECT until_ts, status FROM da_cooldown WHERE kind = ?", (KIND,)).fetchone()
        if row and now < row["until_ts"]:
            return Block("net" if row["status"] == 0 else "cooldown", row["until_ts"])
        rows = self.conn.execute("SELECT ts, cost FROM da_budget WHERE kind = ? AND ts > ? ORDER BY ts",
                                 (KIND, now - WINDOW_S)).fetchall()
        limit = PER_HOUR - (LOW_RESERVE if low else 0)
        used = sum(r["cost"] for r in rows)
        if used + 1 <= limit:
            return None
        need, freed = used + 1 - limit, 0.0
        for r in rows:
            freed += r["cost"]
            if freed >= need:
                return Block("budget", r["ts"] + WINDOW_S, limit)
        return Block("budget", now + WINDOW_S, limit)

    def room(self, low: bool = False) -> float:
        b = self.block(low)
        if b and b.why == "cooldown":
            return 0.0
        used = self.conn.execute("SELECT COALESCE(SUM(cost), 0) FROM da_budget WHERE kind = ? AND ts > ?",
                                 (KIND, self.clock() - WINDOW_S)).fetchone()[0]
        return max(0.0, PER_HOUR - (LOW_RESERVE if low else 0) - float(used))

    # запрос

    def _cached(self, path: str, ttl: float) -> str | None:
        if ttl <= 0:
            return None
        row = self.conn.execute("SELECT ts, body FROM cm_cache WHERE path = ?", (path,)).fetchone()
        if row and self.clock() - row["ts"] < ttl:
            return row["body"]
        return None

    def _unavailable(self, path: str, status: int, when: float | None = None) -> Unavailable:
        seen = f" (проверяла в {da_access.until_text(when, self.clock())})" if when else ""
        if 300 <= status < 400:
            return Unavailable(f"ych.commishes перенаправил {path} (наверное, нужен вход) — не иду туда{seen}", status)
        if status != 404:
            return Unavailable(f"ych.commishes ответил {status} на {path}{seen}", status)
        return Unavailable(f"на ych.commishes нет {path}{seen}", status)

    def get(self, path: str, *, ttl: float, purpose: str = "", low: bool = False,
            err_ttl: float = ERROR_TTL_S) -> str:
        """Текст ответа 200. Сначала кэш (моложе ttl), потом бюджет и пауза; Throttled — запрос не отправлен,
        сайт попросил притормозить или не отвечает (сеть: пауза NET_PAUSE_S — до конца прохода не трогаем);
        Unavailable — 4xx (кроме 403/429) или редирект (помним err_ttl: тот же адрес снова не спрашиваем)."""
        if not path.startswith("/") or path.startswith("//") or FORBIDDEN_PATHS.match(path):
            raise ValueError(f"не тот адрес для помощника: {path!r}")
        drop_old_json(self.conn, self.clock())
        hit = self._cached(path, ttl)
        if hit is not None:
            return hit
        miss = self.conn.execute("SELECT ts, body FROM cm_cache WHERE path = ?", ("!" + path,)).fetchone()
        if miss and err_ttl > 0 and self.clock() - miss["ts"] < err_ttl:
            raise self._unavailable(path, _int(miss["body"]) or 404, miss["ts"])
        wait = self._reserve(purpose, low)
        if wait > 0:
            self.sleep(wait)
        try:
            resp = self.http.get(BASE + path)
        except httpx.HTTPError as e:
            # Запрос мог дойти (таймаут) — он уже в бюджете; пауза — чтобы проход не долбил медленный сайт.
            self.sent += 1
            until = self._pause(0, None)
            log.warning("ych.commishes: %s на %s", type(e).__name__, path)
            raise Throttled(Block("net", until)) from e
        self.sent += 1
        status = resp.status_code
        if status in (403, 429) or status >= 500:
            until = self._pause(status, da_access.retry_after_s(resp.headers, self.clock()))
            raise Throttled(Block("cooldown", until))
        if 300 <= status < 500:  # 404, 410, 401, редирект…: тот же адрес снова не раньше err_ttl
            self._store("!" + path, str(status))
            raise self._unavailable(path, status)
        if status >= 400:
            raise CommishesError(f"ych.commishes ответил {status} на {path}")
        self.conn.execute("UPDATE da_cooldown SET level = 0 WHERE kind = ? AND until_ts <= ?", (KIND, self.clock()))
        self.conn.execute("DELETE FROM cm_cache WHERE path = ?", ("!" + path,))
        self._store(path, resp.text)
        return resp.text

    def _reserve(self, purpose: str, low: bool) -> float:
        """Место в бюджете — до запроса, в одной записывающей транзакции с проверкой: чат и фон (два процесса,
        одна база) не пройдут проверку оба. Запись ставим на время отправки (сейчас + ожидание MIN_GAP_S)."""
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            block = self.block(low)
            if block:
                raise Throttled(block)
            now = self.clock()
            last = self.conn.execute("SELECT MAX(ts) FROM da_budget WHERE kind = ?", (KIND,)).fetchone()[0]
            wait = max(0.0, MIN_GAP_S - (now - last)) if last is not None else 0.0
            self.conn.execute("INSERT INTO da_budget (ts, kind, cost, purpose) VALUES (?, ?, 1, ?)",
                              (now + wait, KIND, purpose or None))
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return wait

    def _store(self, path: str, body: str) -> None:
        self.conn.execute("INSERT INTO cm_cache (path, ts, body) VALUES (?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                          "ts = excluded.ts, body = excluded.body", (path, self.clock(), body))
        self.conn.commit()

    def _pause(self, status: int, retry_after: float | None) -> float:
        now = self.clock()
        wait = min(float(retry_after or 0), RETRY_AFTER_MAX_S)
        row = self.conn.execute("SELECT * FROM da_cooldown WHERE kind = ?", (KIND,)).fetchone()
        level = row["level"] if row and now - row["since_ts"] < FORGET_LEVEL_S else 0
        if status == 0:          # сеть или таймаут: короткая пауза без наращивания ступени
            step, new_level = NET_PAUSE_S, level
        elif status >= 500:
            step, new_level = SERVER_PAUSE_S, level
        else:
            step, new_level = COOLDOWN_STEPS_S[min(level, len(COOLDOWN_STEPS_S) - 1)], level + 1
        until = now + max(step, wait)
        if row and row["until_ts"] > now:
            # Пауза уже идёт (другой процесс получил 429 с Retry-After): не укорачиваем её и не меняем
            # «просил притормозить» на «сеть» из-за запроса, ушедшего до той паузы.
            until = max(until, float(row["until_ts"]))
            if row["status"] in (403, 429) and (status == 0 or status >= 500):
                status, new_level = row["status"], max(new_level, row["level"])
        self.conn.execute(
            "INSERT INTO da_cooldown (kind, until_ts, level, since_ts, status) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET until_ts = excluded.until_ts, level = excluded.level, "
            "since_ts = excluded.since_ts, status = excluded.status", (KIND, until, new_level, now, status))
        self.conn.commit()
        log.warning("ych.commishes: %s, пауза до %s", status, da_access.until_text(until, now))
        return until


def health_line(conn: sqlite3.Connection, now: float | None = None) -> str:
    now = time.time() if now is None else now
    used = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM da_budget WHERE kind = ? AND ts > ?",
                        (KIND, now - WINDOW_S)).fetchone()[0]
    line = f"Запросы к ych.commishes за час: {float(used):g} из {PER_HOUR}."
    row = conn.execute("SELECT until_ts, status FROM da_cooldown WHERE kind = ? AND until_ts > ?",
                       (KIND, now)).fetchone()
    if row:
        why = "Сайт не отвечал" if row["status"] == 0 else "Сайт просил притормозить"
        line += f" {why} — жду до {da_access.until_text(row['until_ts'], now)}."
    return line


def prune(conn: sqlite3.Connection, now: float | None = None) -> None:
    """Кэш — не дольше суток (JSON аукционов — минуты); чужие лоты — KEEP_MARKET_DAYS (ai-train=no: копить
    незачем). Её лоты не трогаем."""
    now = time.time() if now is None else now
    _ensure_cache(conn)
    conn.execute("DELETE FROM cm_cache WHERE ts < ?", (now - KEEP_CACHE_S,))
    drop_old_json(conn, now)
    conn.execute("UPDATE items SET snippet = NULL WHERE source = ? AND own = 0 AND snippet IS NOT NULL", (SOURCE,))
    conn.execute("DELETE FROM items WHERE source = ? AND own = 0 AND updated_ts < ? "
                 "AND NOT EXISTS (SELECT 1 FROM sales s WHERE s.item_id = items.id)",
                 (SOURCE, now - KEEP_MARKET_DAYS * 86400))
    conn.commit()


def drop_old_json(conn: sqlite3.Connection, now: float, path: str | None = None) -> None:
    """Тела JSON аукционов (там ники ставящих) — не дольше KEEP_JSON_S; path — этот сразу (чужой аукцион:
    ставки показали, ники хранить незачем). Дёшево — на каждом запросе и каждой чистке, не раз в сутки."""
    _ensure_cache(conn)
    if path:
        conn.execute("DELETE FROM cm_cache WHERE path = ?", (path,))
    conn.execute("DELETE FROM cm_cache WHERE path LIKE '/auction/show/%/.json' AND ts < ?", (now - KEEP_JSON_S,))
    conn.commit()


def maybe_prune(conn: sqlite3.Connection, now: float | None = None) -> bool:
    """Чистка раз в сутки — из любого пути (чат, фон), даже если ник не указан и фон выключен.
    Старые JSON аукционов (ники) убираем при каждом вызове."""
    now = time.time() if now is None else now
    drop_old_json(conn, now)
    last = db.last_run(conn, PRUNE_SOURCE)
    if last and last["last_run_ts"] and now - last["last_run_ts"] < PRUNE_EVERY_S:
        return False
    prune(conn, now)
    db.record_run(conn, PRUNE_SOURCE, None, now=now)
    return True


# --- текст описаний: SB/MI/AB, ступени цен, тип лота ---

# «1,500» и «1 500» — тысячи; «12,5» и «12.50» — дробь. Число не обрезаем на середине (иначе «1,500$» стало бы
# 1.5, а «10 000 ₽» — $10): после суммы не может идти ещё « 000» или «k» (5k — это не $5, тысяч не угадываем).
_AMT_RAW = (r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?"
            r"|\d{1,5}(?:[.,]\d{1,2})?)(?!\d)(?![.,]\d)(?![ \u00a0\u202f]\d{3}\b)(?!\s?[kкKК]\b)")
# После суммы — другая валюта (поинты DA, ₽, €…): это не $, такую сумму не берём.
_NOT_USD = r"(?!\s?(?:pts?\b|points?\b|da\s?(?:pts?|points?)\b|dap\b|₽|р\.|руб|rub\b|rur\b|€|eur|£|gbp\b|coins?\b|ac\b))"
_AMT = _AMT_RAW + _NOT_USD
_LABEL_SB = r"(?:sb|start(?:ing)?\s*bid|start(?:ing)?\s*price|min(?:imum)?\s*bid(?!\s*inc))"
_LABEL_MI = r"(?:mi|mb|min(?:imum)?\s*(?:bid\s*)?inc(?:rease|rement)?|min(?:imum)?(?!\s*bid)|inc(?:rease|rement)?)"


def _labelled(label: str) -> re.Pattern:
    # «SB: 10$», «SB - $15», «♢ SB: $15», «SB $10», «SB 10$». Без разделителя — только с $.
    return re.compile(r"(?<![\w])" + label + r"\s*(?:[:=\-–]\s*\$?\s?" + _AMT + r"|\$\s?" + _AMT + r"|" + _AMT_RAW
                      + r"\s?\$)", re.I)


SB_TEXT = _labelled(_LABEL_SB)
MI_TEXT = _labelled(_LABEL_MI)
TIER_LINE = re.compile(r"^\s*(?:[-•♢*➤]\s*)?(?:\$\s?" + _AMT + r"|" + _AMT + r"\s?\$)\s*[-–—:]\s*(\S.{0,60}?)\s*$")
SB_TIER = re.compile(r"^\s*(?:[-•♢*➤]\s*)?sb\s*[-–—:]\s*([^\d$\s].{0,40}?)\s*$", re.I)


def _num(raw: str) -> float:
    raw = re.sub(r"[ \u00a0\u202f]", "", raw)  # «1 500» → 1500
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?", raw):
        return float(raw.replace(",", ""))
    return float(raw.replace(",", "."))


def _first_amount(pattern: re.Pattern, text: str) -> float | None:
    m = pattern.search(text or "")
    if not m:
        return None
    raw = next(g for g in m.groups() if g)
    return _num(raw)


def text_prices(text: str) -> dict:
    """SB/MI/AB и ступени из свободного текста (в RSS других цифр нет). Сайт считает в $, но описания часто
    скопированы с DA: сумма с «pts», «₽», «€» после неё — не $, её не берём."""
    text = text or ""
    tiers: list[tuple[float, str]] = []
    sb_label = None
    for line in text.splitlines():
        m = TIER_LINE.match(line)
        if m and not re.match(r"\s*(?:[-•♢*➤]\s*)?(?:sb|mi|mb|ab\d?|min)\b", line, re.I):
            tiers.append((_num(m.group(1) or m.group(2)), m.group(3)))
            continue
        s = SB_TIER.match(line)
        if s:
            sb_label = s.group(1)
    return {"sb": _first_amount(SB_TEXT, text), "mi": _first_amount(MI_TEXT, text),
            "ab": auctions.ab_price(text, "usd"), "tiers": tiers, "sb_label": sb_label}


YCH_WORD = re.compile(r"\bychs?\b", re.I)
YCH_TYPES: tuple[tuple[str, str], ...] = (
    ("couple", r"\b(?:couples?|duo|pair(?!\s+of\b)|two\s+characters|2\s*chars?)\b"),
    ("ref", r"\bref(?:erence)?s?(?:\s*sheets?)?\b"),
    ("chibi", r"\bchibis?\b"),
    ("icon", r"\b(?:icons?|pfps?|avatars?|emotes?|stickers?)\b"),
    ("headshot", r"\b(?:head\s*shots?|busts?)\b"),
    ("halfbody", r"\b(?:half[\s-]?bod(?:y|ies)|waist[\s-]?up|thigh[\s-]?up)\b"),
    ("fullbody", r"\b(?:full[\s-]?bod(?:y|ies)|full\s+figure)\b"),
    ("sketch", r"\bsketch(?:es|y)?\b"),
)
YCH_NAMES = {"couple": "пары", "ref": "рефы", "chibi": "чиби", "icon": "иконки", "headshot": "хедшоты",
             "halfbody": "по пояс", "fullbody": "в полный рост", "sketch": "скетчи", None: "без типа"}
KIND_NAMES = {"adopt": "адопты", "ych": "YCH", "commission": "коммишены", "other": "другое"}
YCH_LEAD = 200   # дальше в описании — бонусы («AB + free Portrait») и правила


# Всё после «+», «free», «bonus» — бонусы к лоту («Fullbody + free icon»), а не его тип.
_BONUS = re.compile(r"\+|\b(?:free|bonus|extra|optional|add[\s-]?ons?)\b", re.I)
# Строки цен («SB - Sketch», «$50 - colored sketch») — ступени и правила, а не тип лота.
_PRICE_LINE = re.compile(r"^\s*(?:[-•♢*➤]\s*)?(?:sb|mi|mb|ab\d?|min|start)\b", re.I)


def _type_blob(raw: str) -> str:
    lines = [ln for ln in classify.normalize_text(raw).splitlines()
             if not _PRICE_LINE.match(ln) and not TIER_LINE.match(ln)]
    blob = " ".join(lines)
    cut = _BONUS.search(blob)
    return blob[:cut.start()] if cut else blob


def ych_type_of(title: str, text: str = "") -> str | None:
    """Тип YCH по словам названия, потом начала описания: слово, которое стоит раньше всех (главное), без бонусов
    и строк цен. У сайта своей классификации нет."""
    for blob in (_type_blob(title or ""), _type_blob((text or "")[:YCH_LEAD])):
        best: tuple[int, str] | None = None
        for name, pattern in YCH_TYPES:
            m = re.search(pattern, blob, re.I)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), name)
        if best:
            return best[1]
    return None


def kind_of(title: str, text: str = "", category: str | None = None) -> str:
    """adopt | ych | commission | other. Вкладка «Adoptables» — адопт, если в названии нет YCH."""
    t = classify.normalize_text(title or "")
    if YCH_WORD.search(t):
        return "ych"
    if category == "adopt":
        return "adopt"
    lt = classify.lot_type_of(t, (text or "")[:300])
    if lt == "ych":
        return "ych"
    if lt == "commission":
        return "commission"
    if lt == "adopt" or re.search(r"\badopt", t, re.I):
        return "adopt"
    if YCH_WORD.search((text or "")[:YCH_LEAD]):
        return "ych"
    return "other"


def _plain(fragment: str) -> str:
    """HTML описания → текст со строками. В RSS сущности бывают экранированы дважды («&amp;rsquo;»)."""
    text = BeautifulSoup(fragment or "", "html.parser").get_text()
    text = html_lib.unescape(text).replace("\r\n", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


# --- RSS ---

@dataclass
class Entry:
    id: int | None
    code: str | None
    url: str | None
    title: str
    text: str
    category: str | None
    rss_end_ts: float | None        # дата из ленты: НЕ конец (на 1–3 дня раньше), только для отладки
    sb: float | None = None
    mi: float | None = None
    ab: float | None = None
    tiers: list[tuple[float, str]] = field(default_factory=list)
    sb_label: str | None = None
    kind: str = "other"
    ych_type: str | None = None
    ends_ts: float | None = None    # настоящего конца в RSS нет


ENDS_PREFIX = re.compile(r"^\s*\[Ends:[^\]]*\]\s*")


def _date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


_XML_ENTITIES = ("amp", "lt", "gt", "quot", "apos")
_NAMED_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]{1,31});")


def _xml_entities(text: str) -> str:
    """HTML-сущности вне CDATA («&bdquo;», «&bull;» в названии) в XML не определены — из-за одной такой вся
    лента не разбиралась. Заменяем числовыми; неизвестную — текстом (&amp;…)."""
    def fix(m: re.Match) -> str:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        cp = name2codepoint.get(name)
        return f"&#{cp};" if cp else f"&amp;{name};"

    return _NAMED_ENTITY.sub(fix, text)


def parse_rss(xml_text: str, category: str | None = None) -> tuple[list[Entry], int]:
    """(лоты, ttl). Ссылки с номером 0 (общая лента) дают id None."""
    try:
        root = safe_fromstring(_xml_entities((xml_text or "").strip()).encode("utf-8"))
    except (XmlParseError, DefusedXmlException, ValueError) as e:
        raise CommishesError(f"лента ych.commishes не разобралась: {type(e).__name__}") from e
    channel = root.find("channel")
    if channel is None:
        raise CommishesError("лента ych.commishes без <channel>")
    try:
        ttl = int((channel.findtext("ttl") or "").strip() or RSS_TTL_S)
    except ValueError:
        ttl = RSS_TTL_S
    out = []
    for it in channel.findall("item"):
        title = " ".join((it.findtext("title") or "").split())
        text = _plain(ENDS_PREFIX.sub("", it.findtext("description") or ""))
        got = parse_url(it.findtext("link") or "")
        prices = text_prices(text)
        kind = kind_of(title, text, category)
        out.append(Entry(id=got[0] if got else None, code=got[1] if got else None,
                         url=auction_url(got[1]) if got else None, title=title, text=text, category=category,
                         rss_end_ts=_date(it.findtext("pubDate")), sb=prices["sb"], mi=prices["mi"], ab=prices["ab"],
                         tiers=prices["tiers"], sb_label=prices["sb_label"], kind=kind,
                         ych_type=ych_type_of(title, text) if kind == "ych" else None))
    return out, max(ttl, 60)


# --- JSON аукциона ---

@dataclass
class SlotBid:
    name: str
    amount: float
    ts: float
    guest: bool = False
    user_id: str | None = None


@dataclass
class Slot:
    id: int
    name: str
    startbid: float | None
    minincrease: float | None
    autobuy: float | None
    highest: float | None
    bids: list[SlotBid] = field(default_factory=list)   # по времени, от ранней


@dataclass
class Auction:
    id: int
    code: str
    title: str
    subtitle: str
    description: str
    seller: str
    seller_id: str | None
    ends_ts: float | None
    fetched_ts: float | None        # "time" ответа сервера
    rating: int | None
    adult: bool
    promoted: bool
    sale: bool
    slots: list[Slot]
    settings: dict

    @property
    def url(self) -> str:
        return auction_url(self.code)


def _f(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def parse_auction_json(text: str) -> Auction:
    try:
        data = json.loads(text or "")
    except json.JSONDecodeError as e:
        raise CommishesError("ych.commishes вернул не JSON") from e
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict) or not str(data.get("result", "")).startswith("200") or not payload.get("id"):
        raise CommishesError(f"ych.commishes: {str(data.get('result') if isinstance(data, dict) else '?')[:40]}")
    p: dict[str, Any] = payload
    aid = int(p["id"])
    slots = []
    raw_slots: dict = p["slots"] if isinstance(p.get("slots"), dict) else {}
    for sid, s in raw_slots.items():
        if not isinstance(s, dict):
            continue
        sb_list = []
        for b in s.get("bids") or []:
            if not isinstance(b, dict) or _f(b.get("bid")) is None:
                continue
            user: dict = b["user"] if isinstance(b.get("user"), dict) else {}
            sb_list.append(SlotBid(name=str(b.get("name") or user.get("name") or "?"), amount=float(b["bid"]),
                                   ts=_f(b.get("created")) or 0.0, guest=bool(b.get("guest")),
                                   user_id=str(user["id"]) if user.get("id") else None))
        sb_list.sort(key=lambda b: (b.ts, b.amount))
        try:
            slot_id = int(sid)
        except ValueError:
            slot_id = aid
        slots.append(Slot(id=slot_id, name=str(s.get("name") or p.get("title") or ""), startbid=_f(s.get("startbid")),
                          minincrease=_f(s.get("minincrease")), autobuy=_f(s.get("autobuy")),
                          highest=_f(s.get("highestbid")), bids=sb_list))
    sale: dict = p["sale"] if isinstance(p.get("sale"), dict) else {}
    settings: dict = p["settings"] if isinstance(p.get("settings"), dict) else {}
    return Auction(id=aid, code=id_to_code(aid), title=str(p.get("title") or ""), subtitle=str(p.get("subtitle") or ""),
                   description=str(p.get("description") or "").replace("\r\n", "\n"),
                   seller=str(p.get("username") or ""), seller_id=str(p["user"]) if p.get("user") else None,
                   ends_ts=_f(p.get("endsunix")) or _date(p.get("ends")), fetched_ts=_f(data.get("time")),
                   rating=int(p["rating"]) if isinstance(p.get("rating"), int) else None, adult=bool(p.get("adult")),
                   promoted=bool(p.get("promoted")), sale=bool(sale.get("discount")), slots=slots, settings=settings)


def is_final(a: Auction) -> bool:
    """Итог только если сервер отвечал уже после конца: до этого защита от снайпинга могла продлить."""
    return bool(a.ends_ts and a.fetched_ts and a.fetched_ts >= a.ends_ts)


# --- страница аукциона ---

@dataclass
class Page:
    id: int | None
    code: str | None
    seller: str | None
    ends_ts: float | None
    category: str | None
    anti_snipe: bool
    anti_snipe_min: int | None
    rating: str | None
    promoted: bool
    title: str = ""


SNIPE_WINDOW = re.compile(r"last\s+(\d{1,3})\s+minutes?", re.I)
CONFIG_ID = re.compile(r"configuration\.auction\s*=\s*\{[^}]*?\bID\s*:\s*(\d+)", re.S)
CONFIG_URL = re.compile(r"configuration\.auction\s*=\s*\{[^}]*?\bURL\s*:\s*'([^']+)'", re.S)


def parse_auction_page(page_html: str) -> Page:
    soup = BeautifulSoup(page_html or "", "html.parser")
    m_id, m_url = CONFIG_ID.search(page_html or ""), CONFIG_URL.search(page_html or "")
    code = None
    if m_url:
        got = parse_url(BASE + m_url.group(1))
        code = got[1] if got else None
    aid = int(m_id.group(1)) if m_id else (code_to_id(code) if code else None)
    box = soup.find(id="auction")
    ends = _f(box.get("data-endtime")) if box else None
    seller = None
    for a in soup.select('a[href^="/user/"]'):
        name = a.get_text(strip=True)
        if name:
            seller = name
            break
    tab = soup.select_one(".tab.active")
    category = None
    if tab is not None:
        m = re.match(r"^/category/([a-z]+)/?$", str(tab.get("href") or ""))
        category = m.group(1) if m else None
    badge, window = False, None
    for b in soup.select(".setting-badge"):
        title = b.select_one(".title")
        if title and "sniping protection enabled" in title.get_text(" ", strip=True).lower():
            badge = True
            m = SNIPE_WINDOW.search(b.get_text(" ", strip=True))
            window = int(m.group(1)) if m else None
    chips = [s.get_text(" ", strip=True) for s in soup.select("span.rounded-full")]
    rating = next((c for c in chips if c in ("Safe", "Mature", "Adult", "Questionable")), None)
    title_el = soup.select_one("h1 .tracking-tight")
    return Page(id=aid, code=code, seller=seller, ends_ts=ends, category=category, anti_snipe=badge,
                anti_snipe_min=window, rating=rating, promoted="Promoted" in chips,
                title=title_el.get_text(" ", strip=True) if title_el else "")


# --- профиль и история ---

@dataclass
class Card:
    id: int
    code: str
    title: str
    ends_ts: float | None
    bid: float | None
    rating: int | None = None
    sale: bool = False
    promoted: bool = False


@dataclass
class Profile:
    username: str | None
    userid: int | None
    registered: float | None
    stats: dict
    cards: list[Card]


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _bool_attr(v) -> bool:
    return str(v or "").strip().lower() == "true"


def parse_profile(page_html: str) -> Profile:
    soup = BeautifulSoup(page_html or "", "html.parser")
    hero = soup.find("profile-hero")
    tree = soup.find("profile-linktree")
    stats = {}
    if tree is not None:
        for key in ("approval", "reputation", "testimonials", "started", "followers", "uploads"):
            value = _int(tree.get(f":{key}"))
            if value is not None:
                stats[key] = value
    cards: list[Card] = []
    seen: set[int] = set()
    for el in soup.find_all("auction-card"):
        got = parse_url(BASE + str(el.get("url") or ""))
        aid = _int(el.get("id")) or (got[0] if got else None)
        if not aid or aid in seen:
            continue
        seen.add(aid)
        cards.append(Card(id=aid, code=got[1] if got else id_to_code(aid), title=str(el.get("title") or ""),
                          ends_ts=_f(el.get("endsunix")), bid=_f(el.get("bid")), rating=_int(el.get("rating")),
                          sale=_bool_attr(el.get(":sale")), promoted=_bool_attr(el.get(":promoted"))))
    username = (hero.get("username") if hero is not None else None) or (tree.get("username") if tree else None)
    return Profile(username=str(username) if username else None, userid=_int(hero.get("userid")) if hero else None,
                   registered=_f(hero.get("registered")) if hero else None, stats=stats, cards=cards)


def history_path(username: str, until: int | None = None) -> str:
    if not USERNAME.match(username or ""):
        raise ValueError("ник ych.commishes — буквы, цифры, _ . -")
    return f"/user/history/{username}.json" + (f"?until={int(until)}" if until else "")


def parse_history(text: str) -> list[Card]:
    """Прошлые аукционы. Форма ответа не проверена: берём только записи, где есть номер или ссылка."""
    try:
        data = json.loads(text or "")
    except json.JSONDecodeError:
        return []
    rows = data
    if isinstance(data, dict):
        rows = next((data[k] for k in ("payload", "auctions", "history", "items") if isinstance(data.get(k), list)), [])
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        got = parse_url(BASE + str(r.get("url") or "")) if r.get("url") else None
        aid = _int(r.get("id")) or (got[0] if got else None)
        if not aid:
            continue
        out.append(Card(id=aid, code=got[1] if got else id_to_code(aid), title=str(r.get("title") or ""),
                        ends_ts=_f(r.get("endsunix")) or _date(r.get("ends")),
                        bid=_f(r.get("bid")) if r.get("bid") is not None else _f(r.get("highestbid"))))
    return out


# --- ставки ---

def slot_state(a: Auction, slot: Slot, now: float | None = None) -> bids.AuctionState:
    """Состояние слота в форме bids.AuctionState (как у разбора комментариев DA). Ставки — из данных сайта,
    не из текста: здесь их не угадываем. ended — только если сервер отвечал после конца (is_final)."""
    state = bids.AuctionState(currency="usd")
    active = []
    for i, b in enumerate(slot.bids):
        kind = "bid"
        if slot.autobuy is not None and b.amount >= slot.autobuy:
            kind = "ab"
        elif i == 0 and slot.startbid is not None and b.amount == slot.startbid:
            kind = "sb"
        active.append(bids.Bid(comment_id=f"{slot.id}:{int(b.ts)}:{i}", user=b.name, amount=b.amount, currency="usd",
                               ts=b.ts, kind=kind, raw=""))
        if b.guest:
            state.notes.append(f"{b.name}: гость (ставка с почтой, ник не привязан к аккаунту сайта)")
    state.bids = active
    state.top = max(active, key=lambda b: (b.amount, -b.ts)) if active else None
    state.n_bids = len(active)
    state.bidders = len({b.user.lower() for b in active})
    state.last_bid_ts = max((b.ts for b in active), default=None)
    state.ab_hit = bool(state.top and slot.autobuy is not None and state.top.amount >= slot.autobuy)
    state.ended = is_final(a)
    state.winner = state.top.user if state.ended and state.top else None
    return state


def site_step(amount: float) -> float:
    """Шаг из таблицы подсказок сайта для такой суммы: как в скрипте формы ставки — шаг самой большой границы
    строго меньше суммы (для $10 — 2.5, для $10.01 — 5), для нуля — 0.5 (site_rules.txt)."""
    for floor, step in SITE_STEPS:
        if amount > floor:
            return step
    return 0.5


def min_next_bid(slot: Slot) -> float | None:
    """Минимальная следующая ставка: верхняя + шаг, без ставок — SB (так считает сайт). Шага в данных нет —
    None: «верхняя + 0» сайт не примет (равная ставка отбрасывается), а свой шаг не выдумываем."""
    top = max((b.amount for b in slot.bids), default=None)
    if top is None:
        return slot.startbid
    return top + slot.minincrease if slot.minincrease else None


def _next_text(slot: Slot) -> str | None:
    nxt = min_next_bid(slot)
    if nxt is not None:
        return f"от {_money(nxt)}"
    top = max((b.amount for b in slot.bids), default=None)
    return f"выше {_money(top)} (шага в данных сайта нет)" if top is not None else None


def next_read(ends_ts: float | None, now: float, final: bool = False) -> float | None:
    """Когда в следующий раз читать её аукцион: с T-30 мин раз в 10 мин, потом один раз после конца
    (защита от снайпинга может продлить — тогда конец сдвинется, и расписание продолжится)."""
    if final:
        return None
    if not ends_ts:
        return now + FAR_REREAD_S
    if now < ends_ts - NEAR_END_S:
        return ends_ts - NEAR_END_S
    if now < ends_ts:
        nxt = now + NEAR_STEP_S
        return nxt if nxt < ends_ts else ends_ts + AFTER_END_S
    return max(now, ends_ts + AFTER_END_S)


def _money(v: float | None) -> str:
    return bids.money(v, "usd")


def past_when(ts: float, now: float) -> str:
    """Прошедшее время по часам её мака: «в 11:05» (сегодня), «вчера в 11:05», «30.10 в 11:05» — иначе
    закончившийся три дня назад аукцион читался бы как сегодняшний."""
    at, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    hm = at.strftime("%H:%M")
    days = (today.date() - at.date()).days
    if days <= 0:
        return f"в {hm}"
    if days == 1:
        return f"вчера в {hm}"
    return f"{at.day}.{at.month:02d} в {hm}"


def summary(a: Auction, page: Page | None = None, now: float | None = None, first_ends: float | None = None,
            watched: bool = False) -> str:
    """watched — её аукцион, за которым следит фоновая проверка: тогда «итог перечитаю сама» — правда."""
    now = time.time() if now is None else now
    lines = [f"«{a.title[:70]}» — {a.url}"]
    for slot in a.slots:
        st = slot_state(a, slot, now)
        head = f"{slot.name[:40]}: " if len(a.slots) > 1 else ""
        sb = f"SB {_money(slot.startbid)}" if slot.startbid is not None else "SB ?"
        if st.top:
            n, b = st.n_bids, st.bidders
            text = (f"{head}ставок {n} от {b} чел., сейчас {_money(st.top.amount)} у {st.top.user} ({sb}"
                    + (f", шаг {_money(slot.minincrease)}" if slot.minincrease else "") + ")")
        else:
            text = f"{head}ставок пока нет ({sb})"
        if slot.autobuy is not None:
            text += f"; автобай {_money(slot.autobuy)} {'взят' if st.ab_hit else 'не взят'}"
        if st.ended:
            text += f". Закончился: {'победитель ' + st.winner if st.winner else 'без ставок'}"
        else:
            nxt = _next_text(slot)
            if nxt is not None:
                text += f"; следующая ставка {nxt}"
        lines.append(text + ".")
    if a.ends_ts:
        if is_final(a):
            lines.append(f"Конец был {past_when(a.ends_ts, now)} (по часам твоего мака).")
        elif now >= a.ends_ts:
            lines.append(f"Конец был {past_when(a.ends_ts, now)}, итога сайт ещё не показал — "
                         + ("перечитаю сама при фоновой проверке." if watched else "спроси ещё раз чуть позже."))
        else:
            lines.append(f"Конец {da_access.until_text(a.ends_ts, now)} (по часам твоего мака).")
        if first_ends and a.ends_ts > first_ends + 30:
            lines.append(f"Защита от снайпинга продлила аукцион на {round((a.ends_ts - first_ends) / 60)} мин.")
    if page is not None and page.anti_snipe:
        window = f"{page.anti_snipe_min} мин" if page.anti_snipe_min else "последние минуты"
        lines.append(f"Защита от снайпинга включена: ставки за {window} до конца продлевают аукцион.")
    elif page is not None:
        lines.append("Защиты от снайпинга на этом аукционе нет.")
    return "\n".join(lines)


# --- советы про конец аукциона и пост ---

def ending_advice(a: Auction, page: Page | None, now: float | None = None, brief: bool = False,
                  watched: bool = False, missing: str = "") -> list[str]:
    """Только то, что видно на сайте: значок защиты, конец, минимальная ставка. Своих правил не придумываем.
    brief — после summary (там конец и значок уже сказаны): только то, чего в summary нет.
    watched — фоновая проверка следит за этим аукционом (ник указан, фон включён); missing — чего для этого
    не хватает («ника commishes_username», «фоновой проверки»…). Аукцион закончился — о слежке не говорим."""
    now = time.time() if now is None else now
    out = []
    watch = ("с T-30 мин смотрю ставки при каждой фоновой проверке (раз в 15 мин и реже), итог — после конца"
             if watched else f"сама в фоне не слежу (не хватает {missing or 'фоновой проверки'}) — "
             "спроси status ближе к концу")
    if page is None:
        out.append("Страницу аукциона ещё не смотрела — включена ли защита от снайпинга, не знаю.")
    elif page.anti_snipe:
        if not is_final(a):  # итог уже есть — продлевать нечего, о слежке не говорим
            window = page.anti_snipe_min or 10
            head = "" if brief else (f"Защита от снайпинга включена: ставка за {window} мин до конца продлевает "
                                     "аукцион (так написано на значке). ")
            out.append(f"{head}Насколько продлевает — сайт не пишет; {watch}.")
    else:
        head = "" if brief else "Значка «Sniping protection enabled» на странице нет: последние ставки конец не продлевают. "
        out.append(f"{head}Если в форме создания будет «Sniping protection» — в следующем аукционе её стоит "
                   "включить (форму создания я ещё не видела).")
    if a.ends_ts and now < a.ends_ts and not brief:
        out.append(f"Конец {da_access.until_text(a.ends_ts, now)} по часам твоего мака.")
    if brief:  # следующая ставка уже в summary — только правило сайта
        if not is_final(a):
            out.append(f"{EQUAL_BID_RULE[0].upper()}{EQUAL_BID_RULE[1:]}.")
        return out
    for slot in a.slots:
        nxt = _next_text(slot)
        if nxt is not None and not is_final(a):
            head = f"{slot.name[:40]}: с" if len(a.slots) > 1 else "С"
            out.append(f"{head}ледующая ставка — {nxt}; {EQUAL_BID_RULE}.")
    return out


PAYPAL = re.compile(r"\bpay\s?pal\b", re.I)
USD_STRICT = re.compile(r"\busd\b|\bus\s?dollars?\b|\bdollars\b", re.I)
AI_WORDS = re.compile(r"\b(?:no\s+ai|not\s+ai|ai[\s-]?free|hand[\s-]?(?:drawn|made)|без\s+ии)\b", re.I)
PAY_ANY = re.compile(r"\b(?:boosty|hipolink|lava\.?top|payment|pay|оплат\w*)\b", re.I)


def listing_check(text: str, lot_kind: str = "adopt") -> list[checklist.Item]:
    """Проверка описания лота для ych.commishes (в ленте покупатель видит только текст)."""
    text = text or ""
    prices = text_prices(text)
    items = [
        checklist.Item(prices["sb"] is not None, "sb", "стартовая ставка (SB) в тексте",
                       "" if prices["sb"] is not None else "SB: $…"),
        checklist.Item(prices["mi"] is not None, "mi", "шаг ставки (MI) в тексте",
                       "" if prices["mi"] is not None else "MI: $… (one bid can't equal another)"),
        checklist.Item(bool(USD_STRICT.search(text)), "usd", "валюта названа словом (USD)",
                       "" if USD_STRICT.search(text) else "All prices in USD."),
    ]
    if PAYPAL.search(text):
        items.append(checklist.Item(False, "paypal", f"в тексте PayPal: {checklist.NOT_FOR_HER['paypal']}",
                                    "Payment: Boosty (cards issued outside Russia)."))
    else:
        items.append(checklist.Item(True, "paypal", "PayPal не обещан"))
    items.append(checklist.Item(bool(PAY_ANY.search(text)), "pay", "как платить",
                                "" if PAY_ANY.search(text) else "Payment: Boosty (cards issued outside Russia)."))
    items.append(checklist.Item(bool(AI_WORDS.search(text)), "ai", f"{AI_RULE} — можно написать, что арт свой",
                                "" if AI_WORDS.search(text) else "Hand-drawn, no AI."))
    if lot_kind == "ych" and prices["tiers"]:
        items.append(checklist.Item(True, "tiers", "ступени цен в тексте — ок, но ставки сайт считает одной суммой"))
    return items


PAY_UNVERIFIED = re.compile(r"\b(?:hipolink|lava\.?top)\b", re.I)


def listing_notes(text: str) -> list[str]:
    """Оговорки к проверке описания — отдельными строками, не пунктами чек-листа (их нельзя «иметь»):
    в тексте Hipolink или Lava.top — сказать, что эти способы не проверены."""
    if not PAY_UNVERIFIED.search(text or ""):
        return []
    return [f"{checklist.UNVERIFIED['hipolink']} {LAVA_UNVERIFIED}"]


# --- кросспост DA ↔ ych.commishes ---

DA_ART = re.compile(r"^https://(?:www\.)?deviantart\.com/[A-Za-z0-9_-]+/art/[^\s?#]+$", re.I)
BIDS_WHERE = re.compile(r"\bbids?\s+(?:only|here|there)\b|\bbid\s+on\b|\bonly\s+bids?\b", re.I)


def crosspost_lines(cm_url: str, da_url: str = "", anti_snipe: bool | None = None) -> dict:
    """Готовые английские строки: в пост DA — ссылка на аукцион, в описание на сайте — ссылка на DA.
    anti_snipe — по значку на странице аукциона; не знаем (None) — о защите не пишем."""
    got = parse_url(cm_url)
    if not got:
        raise ValueError("нужна ссылка на аукцион https://ych.commishes.com/auction/show/КОД/")
    url = auction_url(got[1])
    if da_url and not DA_ART.match(da_url.strip()):
        raise ValueError("ссылка DA — вида https://www.deviantart.com/ник/art/название-12345")
    snipe = "anti-snipe on, " if anti_snipe else ""
    out = {"da": f"Bids only on ych.commishes ({snipe}USD): {url}",
           "commishes": (f"Also posted on DeviantArt: {da_url.strip()} - bids are taken here only." if da_url else ""),
           "warnings": ["Ставки только в одном месте: две ленты ставок на один лот — спор, чья выше.",
                        f"На ych.commishes {FINAL_RULE}; в посте DA это стоит повторить."]}
    return out


def crosspost_check(text: str) -> list[checklist.Item]:
    """Для чек-листа поста DA: если есть ссылка на ych.commishes, сказано ли, где принимают ставки."""
    urls = [u for u in re.findall(r"https?://\S+", text or "") if parse_url(u.rstrip(").,"))]
    if not urls:
        return []
    ok = bool(BIDS_WHERE.search(text))
    url = auction_url(parse_url(urls[0].rstrip(").,"))[1])  # type: ignore[index]
    return [checklist.Item(ok, "cm_bids_where", "сказано, где принимаются ставки (DA или ych.commishes)",
                           "" if ok else f"Bids only on ych.commishes: {url}")]


# --- черновик лота (только текст; форму сайта ещё не видели) ---

def listing_draft(*, title: str, sb: float, mi: float | None = None, ab: float | None = None, kind: str = "adopt",
                  ych_type: str | None = None, hours: float = 48, payment: str = "", rating: str = "safe",
                  category: str = "", subtitle: str = "", rules: str = "") -> dict:
    """Поля и описание аукциона. Ничего не отправляет и в её Chrome не заполняет: форму создания на сайте
    ещё не смотрели (нужен её вход), поэтому заполнение — TODO. Она переносит текст сама."""
    if not (title or "").strip():
        raise ValueError("нужно название")
    if sb is None or sb <= 0:
        raise ValueError("SB — сумма больше нуля")
    warnings = []
    step = site_step(sb)
    mi = mi if mi is not None else step
    if mi != step:
        warnings.append(f"шаг сайта для {_money(sb)} — {_money(step)} (подсказка в форме ставки); "
                        f"{EQUAL_BID_RULE}, круглый шаг помогает")
    if ab is not None and ab <= sb:
        warnings.append("автобай не выше SB — его возьмут первой ставкой")
    if kind == "ych" and not ych_type:
        warnings.append("тип YCH (icon/headshot/halfbody/fullbody/ref) лучше назвать в заголовке — по нему ищут")
    warnings.append(AI_RULE)
    if PAYPAL.search(payment or ""):
        warnings.append(checklist.NOT_FOR_HER["paypal"])
    pay = (payment or "").strip() or "Boosty (cards issued outside Russia)"
    lines = [f"SB: {_money(sb)}", f"MI: {_money(mi)}", f"AB: {_money(ab) if ab is not None else '-'}", "",
             "All prices in USD.", f"Payment: {pay}. Payment within 24h after the auction ends.",
             "Bids are final: the winner is required to buy.", "Hand-drawn, no AI."]
    if rules.strip():
        lines += ["", rules.strip()]
    cat = category or ("adopt" if kind == "adopt" else "")
    fields = {"title": title.strip(), "subtitle": subtitle.strip(), "category": cat or None,
              "rating": rating, "duration_h": hours, "anti_snipe": True, "allow_questions": True,
              "slots": [{"name": title.strip(), "startbid": sb, "minincrease": mi, "autobuy": ab}]}
    return {"fields": fields, "description": "\n".join(lines), "warnings": warnings, "submit": False,
            "fill": "TODO: заполнение формы в её Chrome — после того как посмотрим форму создания (нужен её вход); "
                    "отправляет всегда она сама."}


# --- её аукционы: чтение, события, продажи ---

@dataclass
class TrackResult:
    events: list = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    read: int = 0
    throttled: str | None = None
    profile_error: str | None = None   # профиль не открылся (опечатка в нике, нужен вход) — сказать ей
    gone: list[str] = field(default_factory=list)   # её аукционы, которые пропали с сайта (сказано один раз)

    @property
    def text(self) -> str:
        out = list(self.lines)
        out += [auctions.describe_event(e) for e in self.events]
        out += self.notes
        if self.throttled:
            out.append(self.throttled[0].upper() + self.throttled[1:] + ".")
        return "\n".join(out) if out else "Её аукционов на ych.commishes пока не вижу."


def _extra(row) -> dict:
    try:
        value = json.loads(row["extra"] or "{}") if row else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _slot_iid(a: Auction, slot: Slot) -> str:
    """Один слот — cm:<номер>; несколько — у каждого свой cm:<номер>:<слот> (даже если номер слота = номеру
    аукциона): тогда cm:<номер> у аукциона со слотами — никогда не лот, и ручная запись без слота видна."""
    return _item_id(a.id, slot.id) if len(a.slots) > 1 else _item_id(a.id)


def lot_rows(conn, auction_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM items WHERE source = ? AND (id = ? OR id LIKE ?) ORDER BY id",
                        (SOURCE, _item_id(auction_id), f"{_item_id(auction_id)}:%")).fetchall()


def _save_lots(conn, a: Auction, page: Page | None, own: bool, now: float) -> list[sqlite3.Row]:
    """Каждый слот — свой лот (items). Сохраняет то, что знаем о странице, из прошлых чтений.
    Текст описания — только у её лотов (для проверки описания); у чужих — нет (там он не нужен)."""
    rows = []
    final = is_final(a)
    for slot in a.slots:
        iid = _slot_iid(a, slot)
        old = _extra(db.get_item(conn, iid))
        category = page.category if page is not None else old.get("category")
        kind = kind_of(a.title, a.description, category)
        st = slot_state(a, slot, now)
        extra = {"code": a.code, "kind": kind, "ych_type": ych_type_of(a.title, a.description) if kind == "ych" else None,
                 "category": category, "ends_ts": a.ends_ts, "first_ends_ts": old.get("first_ends_ts") or a.ends_ts,
                 "sb": slot.startbid, "mi": slot.minincrease, "ab": slot.autobuy, "slot_id": slot.id,
                 "final": final, "top": st.top.amount if st.top else None, "n_bids": st.n_bids,
                 "rating": a.rating, "adult": a.adult, "promoted": a.promoted, "sale": a.sale,
                 "anti_snipe": page.anti_snipe if page is not None else old.get("anti_snipe"),
                 "anti_snipe_min": page.anti_snipe_min if page is not None else old.get("anti_snipe_min"),
                 "read_ts": now, "fetched_ts": a.fetched_ts, "price_basis": "json",
                 "tiers": text_prices(a.description)["tiers"]}
        extra["extended"] = bool(a.ends_ts and extra["first_ends_ts"] and a.ends_ts > extra["first_ends_ts"] + 30)
        title = a.title if len(a.slots) == 1 else f"{a.title} — {slot.name}"
        lot_type = "ych" if kind == "ych" else "commission" if kind == "commission" else "auction"
        db.upsert_item(conn, db.Item(
            id=iid, source=SOURCE, title=title[:200], url=a.url, author=a.seller or None,
            place=f"{PLACE}/{category}" if category else PLACE, lot_type=lot_type, status="closed" if final else "open",
            price_text=f"SB {_money(slot.startbid)}" if slot.startbid is not None else None, price_usd=slot.startbid,
            activity_ts=st.last_bid_ts, snippet=a.description[:4000] if own else None, own=own, extra=extra), now=now)
        row = db.get_item(conn, iid)
        if row is not None:
            rows.append(row)
    conn.commit()
    return rows


def _read(conn, client: Client, code: str, *, purpose: str, low: bool = False, need_page: bool = True,
          now: float, fresh: bool = False, err_ttl: float = ERROR_TTL_S) -> tuple[Auction, Page | None]:
    """fresh — мимо кэша JSON: чтение после конца должно быть настоящим, иначе ответ из кэша (до конца) не
    итог, а время чтения сдвинется — и аукцион больше никогда не станет «пора читать»."""
    a = parse_auction_json(client.get(json_path(code), ttl=0 if fresh else JSON_TTL_S, purpose=purpose, low=low,
                                      err_ttl=err_ttl))
    page = None
    if need_page:
        page = parse_auction_page(client.get(page_path(code), ttl=PAGE_TTL_S, purpose=purpose, low=low))
    return a, page


def _ends(extra: dict, card: Card | None) -> float | None:
    """Позднейший из концов: карточка профиля в кэше до 30 мин и не знает о свежем продлении из JSON."""
    got = [float(e) for e in ((card.ends_ts if card is not None else None), extra.get("ends_ts")) if e]
    return max(got) if got else None


def _due(extra: dict, card: Card | None, now: float, forced: bool, profile_read: bool) -> bool:
    if forced or not extra.get("read_ts"):
        return True
    if extra.get("final") or extra.get("gone"):
        return False
    if card is not None and card.bid is not None and card.bid != extra.get("card_bid"):
        return True  # на карточке профиля сумма другая — значит, была ставка
    ends = _ends(extra, card)
    last = float(extra.get("read_ts") or 0)
    if ends and now >= ends - NEAR_END_S:
        if now >= ends:
            if now < ends + AFTER_END_S:
                return False
            if last < ends + AFTER_END_S:
                return True  # первое чтение после конца
            # Итога всё ещё нет (часы мака впереди сервера, сбой): перечитываем всё реже, до раза в 6 ч.
            return now - last >= min(POST_END_MAX_S, max(NEAR_STEP_S, last - ends))
        return now - last >= NEAR_STEP_S - 1
    if profile_read and card is None:
        # С профиля пропал — наверное, закончился (или скрыт): перечитать, но не на каждом чтении профиля.
        return now - last >= GONE_REREAD_S
    return not profile_read and now - last >= FAR_REREAD_S


def _mark_gone(conn, auction_id: int, why: str, now: float) -> None:
    """Её аукцион не читается (удалён, скрыт, нужен вход): закрываем, больше не спрашиваем — как чужие лоты."""
    for r in lot_rows(conn, auction_id):
        db.merge_extra(conn, r["id"], {"gone": True, "gone_why": why, "gone_ts": now})
        conn.execute("UPDATE items SET status = 'closed' WHERE id = ?", (r["id"],))
    conn.commit()


def track_mine(conn, client: Client, username: str = "", urls: list[str] | tuple = (),
               now: float | None = None, watched: bool | None = None) -> TrackResult:
    """Её аукционы: профиль (1 запрос — все текущие) + JSON только тех, где что-то могло измениться.
    Новые ставки — события (как у DA), итог — после конца. urls — аукционы, которые она назвала сама.
    watched — следит ли за ними фоновая проверка (по умолчанию — если ник указан)."""
    now = time.time() if now is None else now
    watched = bool(username) if watched is None else watched
    if username and not USERNAME.match(username):
        raise ValueError("ник ych.commishes — буквы, цифры, _ . -")
    res = TrackResult()
    forced: dict[str, bool] = {}
    for u in urls or ():
        got = parse_url(u)
        if not got:
            res.notes.append(f"Не похоже на ссылку на аукцион ych.commishes: {u[:80]}")
            continue
        forced[got[1]] = True
    cards: dict[str, Card] = {}
    profile_read = False
    try:
        if username:
            prof = parse_profile(client.get(f"/user/{username}/", ttl=PROFILE_TTL_S, purpose="её аукционы"))
            cards = {c.code: c for c in prof.cards}
            profile_read = True
    except Throttled as e:
        res.throttled = str(e)
    except CommishesError as e:
        why = (str(e) if not isinstance(e, Unavailable) else "такой страницы нет" if e.status == 404
               else "сайт перенаправляет — нужен вход")
        # Текст без времени проверки: одна и та же ошибка — одно сообщение ей, а не новое на каждом проходе.
        res.profile_error = f"профиль {username} на ych.commishes не открылся ({why}) — проверь ник (commishes_username)"
        res.notes.append(f"Профиль {username} не прочитала: {e}")
    known = conn.execute("SELECT id, extra FROM items WHERE source = ? AND own = 1 AND status != 'closed'",
                         (SOURCE,)).fetchall()
    tracked: dict[str, dict] = {}
    for r in known:
        ex = _extra(r)
        if ex.get("code"):
            tracked.setdefault(str(ex["code"]), ex)
    codes = list(dict.fromkeys([*forced, *cards, *tracked]))
    for code in codes:
        if res.throttled:
            break
        aid = code_to_id(code)
        rows = lot_rows(conn, aid)
        extra = tracked.get(code) or (_extra(rows[0]) if rows else {})
        card = cards.get(code)
        if not _due(extra, card, now, forced.get(code, False), profile_read):
            continue
        ends = _ends(extra, card)
        try:
            a, _ = _read(conn, client, code, purpose="её аукционы", need_page=False, now=now,
                         fresh=bool(ends and now >= ends), err_ttl=0 if forced.get(code) else ERROR_TTL_S)
        except Throttled as e:
            res.throttled = str(e)
            break
        except Unavailable as e:
            if rows and any(r["own"] for r in rows):
                _mark_gone(conn, aid, str(e), now)
                res.gone.append(auction_url(code))
                res.notes.append(f"{auction_url(code)}: не читается ({e}) — наверное, удалён или скрыт; "
                                 "больше его не проверяю.")
            else:
                res.notes.append(f"{auction_url(code)}: {e}")
            continue
        except CommishesError as e:
            # 200 с «result: 404», не JSON…: попытку записываем — иначе после конца аукцион «пора читать» на
            # каждом проходе (read_ts старый) и мимо кэша. Подряд ERR_GONE_AFTER раз после конца — закрываем.
            fails = int(extra.get("err_n") or 0) + 1
            for r in rows:
                db.merge_extra(conn, r["id"], {"read_ts": now, "err": str(e)[:200], "err_n": fails})
            conn.commit()
            if rows and any(r["own"] for r in rows) and ends and now >= ends and fails >= ERR_GONE_AFTER:
                _mark_gone(conn, aid, str(e), now)
                res.gone.append(auction_url(code))
                res.notes.append(f"{auction_url(code)}: после конца не читается ({e}) — больше его не проверяю.")
            else:
                res.notes.append(f"{auction_url(code)}: {e}")
            continue
        res.read += 1
        page = None
        if extra.get("anti_snipe") is None:
            try:
                page = parse_auction_page(client.get(page_path(code), ttl=PAGE_TTL_S, purpose="её аукционы"))
            except Throttled as e:
                res.throttled = str(e)
            except CommishesError as e:
                res.notes.append(f"{a.url}: страницу не прочитала ({e})")
        own = not username or a.seller.lower() == username.lower()
        if not own:
            _save_lots(conn, a, page, False, now)
            res.notes.append(f"{a.url} — не твой аукцион (продавец {a.seller}), не слежу за ним как за твоим.")
            continue
        for row in _save_lots(conn, a, page, True, now):
            slot = next((s for s in a.slots if _slot_iid(a, s) == row["id"]), a.slots[0] if a.slots else None)
            if slot is None:
                continue
            if card is not None:
                db.merge_extra(conn, row["id"], {"card_bid": card.bid})
                row = db.get_item(conn, row["id"]) or row
            event = auctions.save(conn, row, slot_state(a, slot, now), comments_seen=len(slot.bids), now=now)
            if event:
                res.events.append(event)
        ex = _extra(db.get_item(conn, _slot_iid(a, a.slots[0]))) if a.slots else {}
        res.lines.append(summary(a, page, now, first_ends=ex.get("first_ends_ts"), watched=watched))
        if ex.get("extended") and not is_final(a):
            res.lines.append("Сайт продлил аукцион (защита от снайпинга) — перечитаю после нового конца.")
    db.record_run(conn, SOURCE, res.throttled, now=now)
    return res


def sync_sales(conn, now: float | None = None) -> list[str]:
    """Закончившиеся её аукционы со ставками → продажа «ждёт оплаты» (platform commishes, один раз на лот).
    Победитель — верхняя ставка по данным сайта (не по словам), поэтому без пометки «проверь»."""
    rows = conn.execute(
        "SELECT i.id, i.title, i.lot_type, i.extra, a.winner, a.top_amount FROM items i "
        "JOIN auctions a ON a.deviation_id = i.id WHERE i.source = ? AND i.own = 1 AND a.ended = 1 "
        "AND a.top_amount IS NOT NULL AND a.winner IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM sales s WHERE s.item_id = i.id)", (SOURCE,)).fetchall()
    created = []
    claimed = _claim_base_sales(conn, rows)
    for r in rows:
        if r["id"] in claimed or _claim_crosspost_sale(conn, r):
            continue
        kind = _extra(r).get("kind")
        fmt = "ych" if kind == "ych" else "adopt" if kind == "adopt" else "auction"
        note = "ставки на ych.commishes окончательные — победитель обязан купить"
        sale_id = sales.add_sale(conn, title=r["title"], amount=r["top_amount"], currency="usd", platform=SOURCE,
                                 format=fmt, buyer=r["winner"], item_id=r["id"], source="auto", note=note, now=now,
                                 if_new=True)
        if sale_id:
            created.append(sale_id)
    return created


def _same_amount(s, r) -> bool:
    return s["amount"] is not None and abs(float(s["amount"]) - float(r["top_amount"])) < 0.005


def _same_buyer(s, r) -> bool:
    return bool(s["buyer"]) and sales._same_user(str(s["buyer"]), str(r["winner"]))


CHECK_SLOT = "проверь слот: покупатель тот же, что у победителя слота, сумма другая — записала твою"
CHECK_SLOTS = "проверь: у этого покупателя несколько слотов — к какому эта запись, не знаю (укажи lot)"


def _claim_base_sales(conn, rows) -> set[str]:
    """Слоты аукциона со слотами, а она записала продажу по ссылке без слота (cm:<номер>), пока помощник
    аукциона ещё не видел: это та же продажа — привязываем её к слоту, второй не заводим. По порядку:
    сумма и покупатель (ник без «@» и регистра) совпали или покупатель не назван; суммы нет, покупатель тот же
    (сумму берём с сайта); покупатель тот же, сумма другая — её запись главнее, с пометкой «проверь».
    Покупатель выиграл несколько оставшихся слотов — не гадаем: авто-записей для них не заводим, просим lot.
    Возвращает слоты, за которыми теперь её запись (или которые ждут её ответа)."""
    out: set[str] = set()
    by_base: dict[str, list] = {}
    for r in rows:
        if ":" in r["id"][3:]:
            by_base.setdefault(r["id"].rsplit(":", 1)[0], []).append(r)
    for base, slots in by_base.items():
        manual = conn.execute("SELECT id, amount, buyer, note FROM sales WHERE item_id = ? AND source != 'auto' "
                              "AND status != 'cancelled' ORDER BY created_ts", (base,)).fetchall()
        tests = (lambda s, r: _same_amount(s, r) and (not s["buyer"] or _same_buyer(s, r)),
                 lambda s, r: s["amount"] is None and _same_buyer(s, r),
                 _same_buyer)
        for step, test in enumerate(tests):
            for s in list(manual):
                hits = [r for r in slots if r["id"] not in out and test(s, r)]
                if not hits:
                    continue
                if step == 2 and len(hits) > 1:
                    out.update(r["id"] for r in hits)
                    if CHECK_SLOTS not in str(s["note"] or ""):
                        conn.execute("UPDATE sales SET note = TRIM(COALESCE(note, '') || ' ' || ?) WHERE id = ?",
                                     (CHECK_SLOTS, s["id"]))
                    manual.remove(s)
                    continue
                r = hits[0]
                conn.execute("UPDATE sales SET item_id = ?, amount = COALESCE(amount, ?), "
                             "currency = COALESCE(currency, 'usd') WHERE id = ?", (r["id"], r["top_amount"], s["id"]))
                if step == 2:
                    conn.execute("UPDATE sales SET note = TRIM(COALESCE(note, '') || ' ' || ?) WHERE id = ?",
                                 (CHECK_SLOT, s["id"]))
                out.add(r["id"])
                manual.remove(s)
    conn.commit()
    return out


CROSSPOST_MATCH_S = 7 * 86400   # её запись по ссылке DA — в пределах недели от конца аукциона


def _claim_crosspost_sale(conn, r) -> bool:
    """Она записала продажу по ссылке на кросспост DA (da:<номер>) или без ссылки, а ставки были на
    ych.commishes: тот же покупатель (ник без «@» и регистра), та же сумма, запись около конца — это та же
    продажа. Привязываем её к лоту ych.commishes, вторую не заводим."""
    ends = _extra(r).get("ends_ts")
    if not ends:
        return False
    rows = conn.execute("SELECT id, amount, buyer FROM sales WHERE source != 'auto' AND status != 'cancelled' "
                        "AND platform != 'toyhouse' AND (item_id IS NULL OR item_id LIKE 'da:%') "
                        "AND created_ts BETWEEN ? AND ?",
                        (float(ends) - CROSSPOST_MATCH_S, float(ends) + CROSSPOST_MATCH_S)).fetchall()
    for s in rows:
        if _same_amount(s, r) and _same_buyer(s, r):
            conn.execute("UPDATE sales SET item_id = ? WHERE id = ?", (r["id"], s["id"]))
            conn.commit()
            return True
    return False


def sale_item_id(conn, url: str, lot: str = "") -> str:
    """item_id для её ручной записи продажи по ссылке на аукцион ych.commishes (sales.record): тот же, что у
    авто-записи победителя (sync_sales), — тогда ручная запись сольётся с ней. У аукциона со слотами — лот
    одного слота: lot — номер слота или часть его названия; без lot — ValueError со списком слотов."""
    got = parse_url(url)
    if not got:
        raise ValueError("нужна ссылка на аукцион https://ych.commishes.com/auction/show/КОД/")
    rows = lot_rows(conn, got[0])
    slots = [r for r in rows if r["id"] != _item_id(got[0])]
    if not slots:
        return _item_id(got[0])
    want = str(lot or "").lstrip("#").strip().lower()
    if want:
        hit = [r for r in slots if r["id"].rsplit(":", 1)[1] == want]
        hit = hit or [r for r in slots if want in str(r["title"] or "").lower().rsplit(" — ", 1)[-1]]
        if len(hit) == 1:
            return str(hit[0]["id"])
    names = "; ".join(f"{str(r['title'] or '').rsplit(' — ', 1)[-1][:30]} (#{r['id'].rsplit(':', 1)[1]})"
                      for r in slots[:8])
    raise ValueError(f"В этом аукционе ych.commishes несколько слотов — {names}. Укажи lot: номер или название слота.")


def import_history(conn, client: Client, username: str, until: int | None = None, max_reads: int = 3,
                   now: float | None = None) -> dict:
    """Прошлые её аукционы со страницы истории (форма не проверена): итоги со ставками дочитываем JSON-ом
    (не больше max_reads за раз), потом sync_sales."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"cards": 0, "read": 0, "sales": [], "throttled": None}
    try:
        cards = parse_history(client.get(history_path(username, until), ttl=PROFILE_TTL_S, purpose="её история"))
    except Throttled as e:
        out["throttled"] = str(e)
        return out
    out["cards"] = len(cards)
    for card in cards:
        if out["read"] >= max_reads:
            break
        if not card.bid or lot_rows(conn, card.id):
            continue
        try:
            a, page = _read(conn, client, card.code, purpose="её история", need_page=False, now=now)
        except Throttled as e:
            out["throttled"] = str(e)
            break
        except CommishesError:
            continue
        out["read"] += 1
        if a.seller.lower() != username.lower():
            continue
        for row in _save_lots(conn, a, page, True, now):
            slot = next((s for s in a.slots if _slot_iid(a, s) == row["id"]), None)
            if slot is not None:
                auctions.save(conn, row, slot_state(a, slot, now), comments_seen=len(slot.bids), now=now)
    out["sales"] = sync_sales(conn, now)
    return out


# --- рынок: небольшая выборка ---

def sample_market(conn, client: Client, category: str, now: float | None = None) -> dict:
    """Лента категории (1 запрос, ~20 лотов с настоящими номерами) → лоты рынка. Общую ленту не берём:
    в ней номер 0. Цена — SB из текста (заявленная), конца из ленты не берём (он там неверный)."""
    now = time.time() if now is None else now
    if category not in CATEGORIES:
        raise ValueError(f"категория: {', '.join(CATEGORIES)} (общую ленту не читаю — в ней нет номеров)")
    text = client.get(f"/category/{category}/index.rss", ttl=RSS_TTL_S, purpose="рынок", low=True)
    entries, _ = parse_rss(text, category)
    new = 0
    for e in entries:
        if e.id is None:
            continue
        iid = _item_id(e.id)
        # Её лот, уже прочитан JSON-ом (точнее ленты) или хранится по слотам (cm:<номер>:<слот>) — не дублируем.
        if any(r["own"] or r["id"] != iid or _extra(r).get("price_basis") == "json" for r in lot_rows(conn, e.id)):
            continue
        old = _extra(db.get_item(conn, iid))
        if old.get("gone"):
            continue  # не читается — лента его ещё держит, но итога не будет
        extra = {"code": e.code, "kind": e.kind, "ych_type": e.ych_type, "category": category, "sb": e.sb,
                 "mi": e.mi, "ab": e.ab, "tiers": e.tiers, "price_basis": "sb_text", "final": False,
                 "check_after_ts": e.rss_end_ts + RSS_END_LAG_S if e.rss_end_ts else None}
        if old.get("ends_ts"):
            extra["ends_ts"] = old["ends_ts"]  # настоящий конец из JSON (finalize_market) — лента его не знает
        is_new = db.upsert_item(conn, db.Item(
            id=iid, source=SOURCE, title=e.title[:200], url=e.url or "", place=f"{PLACE}/{category}",
            lot_type="ych" if e.kind == "ych" else "commission" if e.kind == "commission" else "auction",
            status="open", price_text=f"SB {_money(e.sb)}" if e.sb is not None else None, price_usd=e.sb,
            snippet=None, extra=extra), now=now)
        new += int(is_new)
    conn.commit()
    db.record_run(conn, f"{SOURCE}_market", None, now=now)
    return {"entries": len(entries), "new": new}


def finalize_market(conn, client: Client, now: float | None = None, limit: int = MARKET_JSON_PER_RUN) -> int:
    """Итоги нескольких закончившихся чужих лотов (JSON после конца, низкий приоритет; limit — запросов за раз).
    Ники покупателей не храним — только сумму и число ставок. Возвращает, сколько итогов узнали."""
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT id, extra FROM items WHERE source = ? AND own = 0 AND COALESCE(json_extract(extra, '$.final'), 0) = 0 "
        "AND COALESCE(json_extract(extra, '$.gone'), 0) = 0 AND first_seen_ts >= ? ORDER BY first_seen_ts",
        (SOURCE, now - MARKET_SAMPLE_DAYS * 86400)).fetchall()
    done = spent = 0
    got: dict[str, Auction | None] = {}   # один JSON на аукцион, сколько бы слотов ни было
    for r in rows:
        ex = _extra(r)
        ends = ex.get("ends_ts") or ex.get("check_after_ts")
        if not ends or now < float(ends) + AFTER_END_S or not ex.get("code"):
            continue
        code = str(ex["code"])
        if code not in got:
            if spent >= limit:
                break
            spent += 1
            try:
                got[code] = parse_auction_json(client.get(json_path(code), ttl=JSON_TTL_S, purpose="рынок", low=True,
                                                          err_ttl=MARKET_ERROR_TTL_S))
                drop_old_json(conn, now, json_path(code))  # ники ставящих чужого лота не держим
            except Throttled:
                break  # пауза, бюджет или сеть: не «удалён», доберу в другой раз
            except CommishesError:
                got[code] = None
        a = got[code]
        if a is None:  # 404, нужен вход, не JSON — больше не спрашиваем
            db.merge_extra(conn, r["id"], {"gone": True})
            conn.execute("UPDATE items SET status = 'closed' WHERE id = ?", (r["id"],))
            continue
        if not is_final(a):
            db.merge_extra(conn, r["id"], {"ends_ts": a.ends_ts})
            continue
        # Лот одного слота (cm:<номер>:<слот>) — только его цифры; cm:<номер> — весь аукцион.
        parts = str(r["id"]).split(":")
        mine = [s for s in a.slots if str(s.id) == parts[2]] if len(parts) > 2 else a.slots
        if len(parts) > 2 and not mine:
            db.merge_extra(conn, r["id"], {"gone": True})  # слота больше нет
            conn.execute("UPDATE items SET status = 'closed' WHERE id = ?", (r["id"],))
            continue
        slot = mine[0] if mine else None
        tops = [max((b.amount for b in s.bids), default=0) for s in mine]
        db.merge_extra(conn, r["id"], {
            "final": True, "price_basis": "json", "ends_ts": a.ends_ts, "sb": slot.startbid if slot else ex.get("sb"),
            "mi": slot.minincrease if slot else ex.get("mi"), "ab": slot.autobuy if slot else ex.get("ab"),
            "top": max(tops) if tops and max(tops) > 0 else None, "n_bids": sum(len(s.bids) for s in mine),
            "slots": len(a.slots)})
        conn.execute("UPDATE items SET status = 'closed', author = ? WHERE id = ?", (a.seller or None, r["id"]))
        done += 1
    conn.commit()
    return done


# --- статистика цен: по типам YCH и адоптам ---

POP = "аукционы ych.commishes, $, все продавцы"
YCH_CATEGORIES = ("furry", "human", "brony")   # ленты по виду персонажа: YCH там, а не в Adoptables


def _pop(cats) -> str:
    """Подпись выборки: какие ленты на самом деле читали (по умолчанию, например, только Adoptables)."""
    names = ", ".join(CATEGORY_NAMES.get(c, c) for c in sorted(cats))
    return f"{POP}, ленты: {names}" if names else POP


def _bucket(ex: dict) -> str | None:
    kind = ex.get("kind")
    if kind == "ych":
        return f"ych:{ex.get('ych_type') or 'none'}"
    if kind in ("adopt", "commission"):
        return str(kind)
    return None


def price_lists(conn, days: int = MARKET_SAMPLE_DAYS, now: float | None = None,
                include_own: bool = False) -> tuple[dict[str, dict[str, list[tuple[float, float]]]], set[str]]:
    """({корзина: {"sb"|"final"|"ended": [(значение, момент)]}}, ленты чужих лотов). Момент — конец, но не
    позже «сейчас»: идущие аукционы не тянут период в будущее."""
    now = time.time() if now is None else now
    rows = conn.execute("SELECT own, first_seen_ts, extra FROM items WHERE source = ? AND first_seen_ts >= ?"
                        + ("" if include_own else " AND own = 0"),
                        (SOURCE, now - days * 86400)).fetchall()
    groups: dict[str, dict[str, list]] = {}
    cats: set[str] = set()
    for r in rows:
        ex = _extra(r)
        b = _bucket(ex)
        if b is None:
            continue
        if ex.get("category") and not r["own"]:
            cats.add(str(ex["category"]))
        g = groups.setdefault(b, {"sb": [], "final": [], "ended": []})
        ts = min(float(ex.get("ends_ts") or r["first_seen_ts"]), now)
        if ex.get("sb") is not None:
            g["sb"].append((float(ex["sb"]), ts))
        if ex.get("final"):
            g["ended"].append((1.0 if ex.get("top") else 0.0, ts))
            if ex.get("top"):
                g["final"].append((float(ex["top"]), ts))
    return groups, cats


def price_stats(conn, days: int = MARKET_SAMPLE_DAYS, now: float | None = None,
                include_own: bool = False) -> dict[str, dict[str, evidence.Evidence]]:
    """{корзина: {"sb": Evidence, "final": Evidence, "sold": Evidence}} — SB по всем лотам; итог — только
    по закончившимся со ставками (прочитанным после конца); sold — доля закончившихся со ставкой.
    interval у цен — 80% для самой медианы (насколько она точна), а не разброс цен: разброс — spread()."""
    now = time.time() if now is None else now
    groups, cats = price_lists(conn, days, now, include_own)
    pop = _pop(cats)
    out: dict[str, dict[str, evidence.Evidence]] = {}
    for b, g in groups.items():
        out[b] = {}
        for key in ("sb", "final"):
            vals = g[key]
            if not vals:
                continue
            prices = [v for v, _ in vals]
            out[b][key] = evidence.span([t for _, t in vals], "market_commishes",
                                        f"{pop}, {'стартовые ставки' if key == 'sb' else 'итоговые ставки'}", "usd",
                                        "observed", interval=evidence.median_ci(prices), measure="price",
                                        value=statistics.median(prices), data_as_of=now)
        if g["ended"]:
            k = sum(v for v, _ in g["ended"])
            n = len(g["ended"])
            lo, hi = evidence.wilson(k, n)
            out[b]["sold"] = evidence.span([t for _, t in g["ended"]], "market_commishes",
                                           f"{pop}, закончившиеся", None, "observed", interval=(lo, hi),
                                           value=k / n, data_as_of=now)
    return out


def spread(values: list[float]):
    """Квартили цен (market.PriceStats): «половина лотов — от q1 до q3» и доля лотов дешевле её цены."""
    from . import market

    vals: list[float | None] = list(values)
    return market.price_stats(vals)


def _bucket_name(b: str) -> str:
    if b.startswith("ych:"):
        sub = b.split(":", 1)[1]
        return f"YCH — {YCH_NAMES.get(None if sub == 'none' else sub, sub)}"
    return KIND_NAMES.get(b, b)


def _price_line(ev: evidence.Evidence, values: list[float], what: str) -> str:
    lo, hi = ev.interval or (ev.value, ev.value)
    q = spread(values)
    half = f"; половина лотов — {_money(q.low)}–{_money(q.high)}" if q.n >= 4 and q.low is not None else ""
    return (f"{what} — медиана {_money(ev.value)} (80% для медианы: {_money(lo)}–{_money(hi)}){half} "
            f"{evidence.tag(ev)}")


def _refuse(what: str, ev: evidence.Evidence) -> str:
    """«Вывода нет»: лотов меньше 8 — сколько из нужных; лотов хватает, но интервал медианы широкий
    (уровень снижен за разброс) — так и сказать, а не «10 из нужных 8»."""
    n = ev.n_eff if ev.n_eff is not None else ev.n
    if n < 8:
        return evidence.refuse(what, ev.n, 8)
    lo, hi = ev.interval or (ev.value, ev.value)
    return (f"Вывода нет: {what} — цены слишком разные (n={ev.n}, медиана {_money(ev.value)}, "
            f"80% для медианы: {_money(lo)}–{_money(hi)}).")


def render_stats(stats: dict, now: float | None = None, *, lists: dict | None = None,
                 configured: list[str] | tuple | None = None, focus: str | None = None,
                 background: bool = True) -> str:
    """focus — «ych» или «ych:<тип>» (она спросила про YCH/иконки): только YCH, и сказать, если нужного нет.
    configured — её commishes_categories (подсказать ленты YCH). background — фон берёт выборку сам."""
    lists = lists or {}
    want = {b: g for b, g in stats.items() if not focus or b.startswith("ych")}
    lines: list[str] = []
    has_ych = any(b.startswith("ych") for b in stats)
    ych_feeds = [c for c in (configured or ()) if c in YCH_CATEGORIES]
    if not has_ych and (focus or stats):
        if configured is not None and not ych_feeds:
            lines.append("YCH в выборке нет: YCH выкладывают в ленты по виду персонажа (Furry, Human…), а читаю только "
                         f"{', '.join(CATEGORY_NAMES.get(c, c) for c in configured) or 'ничего'} — добавь furry "
                         "или human в commishes_categories (всего не больше 3).")
        else:
            lines.append("YCH в выборке пока нет.")
    elif focus and focus.startswith("ych:") and focus not in stats:
        lines.append(f"{_bucket_name(focus)}: таких лотов в выборке пока нет — ниже остальные YCH.")
    if not want:
        if not lines:
            lines.append("Данных с ych.commishes пока нет.")
        lines.append("Возьму небольшую выборку лент при следующей фоновой проверке." if background else
                     "Фоновой выборки нет (нужен ник commishes_username и commishes_market) — новую возьму, "
                     "когда спросишь снова.")
        return "\n".join(lines)
    out = ["Цены на ych.commishes (только $; отдельно от DA и Toyhouse):", *lines]
    for b in sorted(want, key=lambda k: (k != focus, not k.startswith("ych"), k)):
        g = want[b]
        name = _bucket_name(b)
        vals = lists.get(b, {})
        sb = g.get("sb")
        if sb is not None and evidence.level(sb) != "нет вывода":
            out.append(f"• {name}: " + _price_line(sb, [v for v, _ in vals.get("sb", [])], "SB"))
        elif sb is not None:
            out.append(f"• {name}: " + _refuse("стартовые ставки", sb))
        fin = g.get("final")
        if fin is not None and evidence.level(fin) != "нет вывода":
            out.append("  " + _price_line(fin, [v for v, _ in vals.get("final", [])], "итог со ставками"))
        elif fin is not None:
            out.append("  итог: " + _refuse("закончившиеся лоты со ставками", fin))
        sold = g.get("sold")
        if sold is not None and evidence.level(sold) != "нет вывода" and sold.interval:
            out.append(f"  со ставкой закончились {evidence.pct(sold.value or 0)} "
                       f"({evidence.rate_interval(*sold.interval)}) {evidence.tag(sold)}")
    out.append("SB — заявленные стартовые ставки из описаний; итог — верхняя ставка, прочитанная после конца.")
    return "\n".join(out)


@contextlib.contextmanager
def client_for(conn) -> Iterator[Client]:
    """Клиент с настоящим httpx — для MCP-сервера и фоновой проверки (в тестах не используется)."""
    http = make_http()
    try:
        yield Client(conn, http=http)
    finally:
        http.close()


# --- для инструментов сервера: один аукцион по ссылке, её лоты, сроки, подсказки цен ---

def _final_text(conn, rows, now: float) -> str:
    """Итог уже прочитан после конца (is_final) и больше не изменится: ответ из базы, без запроса к сайту."""
    ex0 = _extra(rows[0])
    title = str(rows[0]["title"] or "")
    if len(rows) > 1:
        title = title.rsplit(" — ", 1)[0]
    lines = [f"«{title[:70]}» — {rows[0]['url'] or auction_url(str(ex0.get('code') or ''))}"]
    for r in rows:
        ex = _extra(r)
        head = f"{str(r['title'] or '').rsplit(' — ', 1)[-1][:40]}: " if len(rows) > 1 else ""
        a = conn.execute("SELECT winner, top_amount, n_bids FROM auctions WHERE deviation_id = ?", (r["id"],)).fetchone()
        top = a["top_amount"] if a is not None and a["top_amount"] else ex.get("top")
        n = a["n_bids"] if a is not None else ex.get("n_bids")
        winner = a["winner"] if a is not None else None
        if top:
            lines.append(f"{head}ставок {n or '?'}, итог {_money(top)}"
                         + (f". Закончился: победитель {winner}." if winner else ". Закончился."))
        else:
            lines.append(f"{head}закончился без ставок.")
    if ex0.get("ends_ts"):
        lines.append(f"Конец был {past_when(float(ex0['ends_ts']), now)} (по часам твоего мака).")
    lines.append("Итог прочитан после конца — он не изменится, сайт заново не спрашивала.")
    return "\n".join(lines)


def status(conn, client: Client, url: str, username: str = "", now: float | None = None,
           watched: bool = False, missing: str = "") -> tuple[str, list]:
    """Ставки на одном аукционе по ссылке (JSON + страница ради значка защиты, страница в кэше 12 ч).
    Её аукцион (продавец = её ник) — сохраняем ставки и события, как в фоне; чужой — только показываем
    (в базу — как лот рынка, без ников покупателей; JSON с никами из кэша сразу убираем). watched — фоновая
    проверка следит за её аукционами, missing — чего для этого не хватает. Итог уже известен — из базы.
    (текст, события)."""
    now = time.time() if now is None else now
    got = parse_url(url)
    if not got:
        raise ValueError("нужна ссылка на аукцион вида https://ych.commishes.com/auction/show/КОД/")
    if username and not USERNAME.match(username):
        raise ValueError("ник ych.commishes — буквы, цифры, _ . -")
    known = lot_rows(conn, got[0])
    if known and all(_extra(r).get("final") for r in known):
        return _final_text(conn, known, now), []
    known_ends = [_extra(r).get("ends_ts") for r in known]
    passed = any(e and now >= float(e) for e in known_ends)
    # Она спросила сама: 404 час назад не повод не проверить снова; после конца — мимо кэша JSON.
    a, page = _read(conn, client, got[1], purpose="её аукционы", need_page=True, now=now, fresh=passed, err_ttl=0)
    prev = conn.execute("SELECT MAX(own) FROM items WHERE source = ? AND (id = ? OR id LIKE ?)",
                        (SOURCE, _item_id(a.id), f"{_item_id(a.id)}:%")).fetchone()[0]
    # Ник указан — решает продавец; не указан — её, только если раньше уже числился её лотом.
    mine = a.seller.lower() == username.lower() if username else bool(prev)
    if not mine:
        drop_old_json(conn, now, json_path(a.code))
    events = []
    rows = _save_lots(conn, a, page, mine, now)
    if mine:
        for row in rows:
            slot = next((s for s in a.slots if _slot_iid(a, s) == row["id"]), None)
            if slot is not None:
                event = auctions.save(conn, row, slot_state(a, slot, now), comments_seen=len(slot.bids), now=now)
                if event:
                    events.append(event)
    first = _extra(rows[0]).get("first_ends_ts") if rows else None
    lines = [summary(a, page, now, first_ends=first, watched=mine and watched)]
    if mine:
        lines += ending_advice(a, page, now, brief=True, watched=watched,
                               missing=missing or ("" if username else "ника commishes_username"))
        if not is_final(a):
            line = crosspost_lines(a.url, anti_snipe=page.anti_snipe if page is not None else None)["da"]
            lines.append(f"Если выкладываешь и на DA — строка для поста (вставляешь сама, опубликованный пост "
                         f"DA помощник не правит): {line}")
    elif username:
        lines.append(f"Продавец {a.seller or '?'} — не ты: ставки показала, следить за ним как за твоим не буду.")
    elif not prev:
        lines.append("Твой ник на ych.commishes не указан (commishes_username) — не знаю, твой ли это аукцион: "
                     "ставки показала, а следить и записать продажу смогу, когда скажешь ник.")
    return "\n".join(lines), events


def _open_rows(conn):
    return conn.execute("SELECT i.*, a.n_bids AS a_n, a.top_amount AS a_top, a.winner AS a_winner, "
                        "a.ab_hit AS a_ab, a.ended AS a_ended FROM items i LEFT JOIN auctions a "
                        "ON a.deviation_id = i.id WHERE i.source = ? AND i.own = 1 AND i.status != 'closed' "
                        "ORDER BY COALESCE(json_extract(i.extra, '$.ends_ts'), 9e18)", (SOURCE,)).fetchall()


def _bids_line(r) -> str:
    if r["a_n"] is None:
        return "ставки ещё не читались"
    if not r["a_n"]:
        return "ставок пока нет"
    return f"ставок {r['a_n']}, сейчас {_money(r['a_top'])}" + ("; автобай взят" if r["a_ab"] else "")


def render_mine(conn, now: float | None = None, limit: int = 10) -> str:
    """Её открытые аукционы ych.commishes из базы (без запросов): ставки, конец, защита от снайпинга."""
    now = time.time() if now is None else now
    rows = _open_rows(conn)
    if not rows:
        return ""
    lines = ["ych.commishes ($):"]
    for r in rows[:limit]:
        ex = _extra(r)
        end = ex.get("ends_ts")
        when = f"конец {da_access.until_text(end, now)}" if end and end > now else ("конец прошёл, итог дочитаю"
                                                                                   if end else "конец ?")
        snipe = " · защита от снайпинга" if ex.get("anti_snipe") else ""
        ext = " · продлён" if ex.get("extended") else ""
        lines.append(f"- {r['title'][:60]} — {_bids_line(r)}; {when}{snipe}{ext}\n  {r['url']}")
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}.")
    return "\n".join(lines)


def deadline_lines(conn, now: float | None = None) -> list[str]:
    """Сроки её открытых лотов ych.commishes — по endsunix сайта (точное время, с продлением)."""
    now = time.time() if now is None else now
    out = []
    for r in _open_rows(conn):
        end = _extra(r).get("ends_ts")
        if not end:
            continue
        when = da_access.until_text(end, now) + (" (прошёл, итог дочитаю)" if end <= now else "")
        out.append(f"- «{r['title'][:50]}» — {when}; {_bids_line(r)} · ych.commishes")
    return out


def _bucket_for(title: str, kind: str) -> str:
    if kind == "ych":
        return f"ych:{ych_type_of(title) or 'none'}"
    return kind


def price_hint(conn, title: str, kind: str = "ych", price: float | None = None, currency: str = "usd",
               now: float | None = None) -> str:
    """Одна строка: что на ych.commishes по такому же лоту (тип YCH из названия). Нет данных — пусто.
    Её цену сравниваем с разбросом цен (квартили, доля лотов дешевле), а не с интервалом медианы.
    Итогов ещё нет (только SB) — её цену со стартовыми ставками не ставим в ряд: SB — пол аукциона, а не цена
    продажи, «дороже 80% лотов» по SB было бы неправдой. Валюты не смешиваем: ₽/pts с $-рынком не сравниваем."""
    now = time.time() if now is None else now
    stats = price_stats(conn, now=now)
    lists, _ = price_lists(conn, now=now)
    b = _bucket_for(title, kind)
    if not stats.get(b) and kind == "ych":
        b = "ych:none"
    group = stats.get(b) or {}
    use_final = group.get("final") is not None and evidence.level(group["final"]) != "нет вывода"
    key = "final" if use_final else "sb"
    ev = group.get(key)
    if ev is None:
        return ""
    name = _bucket_name(b)
    what = "итог со ставками" if use_final else "SB"
    if evidence.level(ev) == "нет вывода":
        return f"ych.commishes, {name}: " + _refuse(what, ev)
    values = [v for v, _ in lists.get(b, {}).get(key, [])]
    line = f"ych.commishes, {name}: " + _price_line(ev, values, what)
    q = spread(values)
    if price is not None and not use_final and (currency or "usd").lower() == "usd":
        line += ("; твою цену с SB не сравниваю: это стартовые ставки, итогов со ставками для вывода "
                 "пока мало — итог обычно выше SB")
    elif price is not None and (currency or "usd").lower() == "usd" and q.n >= 4 and q.low is not None:
        cheaper = q.position(price) or 0.0
        if q.high is not None and price > q.high:
            line += f"; твоя цена {_money(price)} выше, чем у {evidence.pct(cheaper)} лотов там"
        elif price < q.low:
            line += f"; твоя цена {_money(price)} ниже, чем у {evidence.pct(1 - cheaper)} лотов там"
        else:
            line += f"; твоя цена {_money(price)} — в средней половине цен там"
    elif price is not None and (currency or "usd").lower() != "usd":
        line += f"; твоя цена в {currency} — с $ не сравниваю"
    return line


def plan_text(conn, title: str = "", kind: str = "adopt", now: float | None = None) -> str:
    """План лота на ych.commishes: цена по выборке рынка, шаг сайта, что включить. Текст описания — её."""
    now = time.time() if now is None else now
    kind = kind if kind in ("adopt", "ych", "commission") else "adopt"
    hint = price_hint(conn, title, kind, now=now)
    lines = [f"План аукциона на ych.commishes ({KIND_NAMES.get(kind, kind)}):",
             hint or ("Цен с ych.commishes пока нет — market:commishes возьмёт небольшую выборку"
                      + (" (YCH — из лент Furry/Human: они должны быть в commishes_categories)." if kind == "ych"
                         else "."))]
    stats = price_stats(conn, now=now).get(_bucket_for(title, kind), {})
    sb = stats.get("sb")
    if sb is not None and evidence.level(sb) != "нет вывода" and sb.value:
        lines.append(f"Подсказка шага на сайте для ставок около {_money(sb.value)} — {_money(site_step(sb.value))}; "
                     f"{EQUAL_BID_RULE}.")
    lines += ["Защита от снайпинга (ставки в последние 10 минут продлевают конец) бывает у аукционов сайта — если "
              "в форме создания она есть, включи (форму создания я ещё не видела).",
              f"В описании: SB, MI и AB, «USD» словом, как платить (не PayPal), что арт свой — {AI_RULE}.",
              f"{FINAL_RULE[0].upper()}{FINAL_RULE[1:]} — это стоит написать.",
              "Если выкладываешь и на DA — в посте DA напиши, что ставки только на ych.commishes "
              "(готовая строка — da_auctions:status url=… её аукциона; проверка — promo:checklist).",
              "Лучшего времени конца по данным ych.commishes нет; срок выбираешь ты.",
              "Форму на сайте заполняешь и отправляешь ты; описание — твоими словами, проверю его "
              "promo:checklist variant=commishes."]
    return "\n".join(lines)
