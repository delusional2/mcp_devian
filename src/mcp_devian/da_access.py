"""Бережный доступ к DeviantArt: один бюджет запросов и одна пауза на всех.

Почему: в 0.3 плановая проверка каждые 15 минут читала все ленты подряд, и DeviantArt начинал
отвечать 403 («временно не пускает помощника»). Теперь каждый запрос к DeviantArt — ленты и их
листание, поиск рынка, статистика, ставки, поиск UUID, страницы групп — сначала спрашивает здесь.
Запросы идут через deviantart._get (RSS и страницы сайта) и da_api.DAClient._request (API), поэтому
здесь видно всё. Обходить блокировки мы не пытаемся: User-Agent честный, просто запросов меньше.

Два класса адресов (kind):

* "rss" — backend.deviantart.com/rss.xml и страницы www.deviantart.com, без входа. Именно они
  отвечали 403. RSS_PER_HOUR = 20 запросов за скользящий час: плановая проверка раз в 15 минут
  читает 3 ленты по странице (12 в час), остальное — листание, поиск рынка, страницы групп.
* "api" — официальный API, куда она сама дала доступ. API_PER_HOUR = 120 (в среднем 2 в минуту):
  ленты через API — 3 × (страница + описания новых работ) за проход, глубже — раз в 6 часов на ленту
  (около 30 в час), её аукционы — около 20 в час, остальное — фоновая работа (низкий приоритет).

Необязательная фоновая работа (поиск рынка, карточки групп, статистика, чужие аукционы) идёт
с низким приоритетом (task(low=True)): последние LOW_RESERVE запросов часа ей не достаются,
чтобы ленты и её собственные аукционы не остались без чтения.

Пауза: 403/429 от класса адресов → этот класс не трогаем 30 мин; если после паузы DeviantArt снова
просит — 1 ч, 2 ч, 4 ч, не больше 8 ч. Retry-After соблюдаем (берём большее из двух). Чистый проход
(были удачные запросы этого класса, отказов не было) сбрасывает ступень. Внутри прохода не повторяем:
вызывающий получает RateLimited и прекращает свою работу («остальное в следующий раз»).

Записи (публикация, комментарий, папки галереи) делаются только по её команде: бюджетом и паузой
их не режем, но считаем, и их 429 тоже включает паузу для чтения.

Если база занята или сломана, доступ не блокируем (fail open), а пишем в лог.
"""

import contextlib
import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

RSS, API = "rss", "api"
KINDS = (RSS, API)
PER_HOUR = {RSS: 20, API: 120}
LOW_RESERVE = {RSS: 6, API: 30}
WINDOW_S = 3600
COOLDOWN_STEPS_S = (30 * 60, 3600, 2 * 3600, 4 * 3600, 8 * 3600)
RETRY_AFTER_MAX_S = 24 * 3600
# Если DeviantArt давно (сутки) не просил притормозить, следующая пауза снова с 30 минут.
FORGET_LEVEL_S = 24 * 3600
NOT_MODIFIED_COST = 0.5
KEEP_ROWS_S = 2 * 86400

KIND_NAMES = {RSS: "чтение лент", API: "API"}


@dataclass
class Block:
    """Почему запрос сейчас не отправляем: пауза от DeviantArt (cooldown) или свой бюджет (budget)."""
    kind: str
    why: str
    until_ts: float
    # Какой предел сработал: для фоновой работы он меньше PER_HOUR на LOW_RESERVE.
    limit: int | None = None

    @property
    def message(self) -> str:
        when = until_text(self.until_ts)
        if self.why == "cooldown":
            return f"DeviantArt притормозил {KIND_NAMES[self.kind]} до {when}"
        what = "лент" if self.kind == RSS else "API"
        limit = PER_HOUR[self.kind] if self.limit is None else self.limit
        tail = "" if limit == PER_HOUR[self.kind] else ", для фоновой работы"
        return (f"лимит запросов к DeviantArt ({what}: {limit} в час{tail}) на сейчас исчерпан — "
                f"остальное в следующий раз (после {when})")


def until_text(ts: float, now: float | None = None) -> str:
    """«18:40», «завтра в 02:10» или «3.11 в 02:10» — по часам её мака."""
    now = _now() if now is None else now
    at, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    hm = at.strftime("%H:%M")
    days = (at.date() - today.date()).days
    if days <= 0:
        return hm
    if days == 1:
        return f"завтра в {hm}"
    return f"{at.day}.{at.month:02d} в {hm}"


def retry_after_s(headers, now: float | None = None) -> float | None:
    """Retry-After: секунды или HTTP-дата. Нет или не разобрать — None."""
    try:
        value = (headers or {}).get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    value = str(value).strip()
    if value.isdigit():
        return float(value)
    try:
        at = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, at - (time.time() if now is None else now))


class Guard:
    """Бюджет и пауза поверх базы. conn — соединение вызывающего (проход проверки), иначе path —
    своё короткое соединение на каждый запрос (MCP-сервер, отправка черновиков)."""

    def __init__(self, conn: sqlite3.Connection | None = None, path: Path | None = None,
                 clock: Callable[[], float] | None = None):
        self.conn, self.path = conn, path
        self.clock = clock or time.time
        self.ok: Counter = Counter()
        self.limited: set[str] = set()
        self.spent: dict[str, float] = {}

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        if self.conn is not None:
            yield self.conn
            return
        conn = sqlite3.connect(self.path or db.DATA_DIR / "monitor.sqlite3", timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(db.DA_ACCESS_SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    # что можно

    def block(self, kind: str, *, low: bool = False, write: bool = False) -> Block | None:
        now = self.clock()
        with self._db() as conn:
            row = conn.execute("SELECT until_ts FROM da_cooldown WHERE kind = ?", (kind,)).fetchone()
            if row and now < row["until_ts"] and not write:
                return Block(kind, "cooldown", row["until_ts"])
            if write:
                return None
            rows = conn.execute("SELECT ts, cost FROM da_budget WHERE kind = ? AND ts > ? ORDER BY ts",
                                (kind, now - WINDOW_S)).fetchall()
        limit = PER_HOUR[kind] - (LOW_RESERVE[kind] if low else 0)
        used = sum(r["cost"] for r in rows)
        if used + 1 <= limit:
            return None
        need, freed = used + 1 - limit, 0.0
        for r in rows:
            freed += r["cost"]
            if freed >= need:
                return Block(kind, "budget", r["ts"] + WINDOW_S, limit)
        return Block(kind, "budget", now + WINDOW_S, limit)

    def room(self, kind: str, *, low: bool = False) -> float:
        """Сколько запросов этого класса ещё можно в этом часу (0 — на паузе или бюджет исчерпан)."""
        now = self.clock()
        with self._db() as conn:
            row = conn.execute("SELECT until_ts FROM da_cooldown WHERE kind = ?", (kind,)).fetchone()
            if row and now < row["until_ts"]:
                return 0.0
            used = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM da_budget WHERE kind = ? AND ts > ?",
                                (kind, now - WINDOW_S)).fetchone()[0]
        return max(0.0, PER_HOUR[kind] - (LOW_RESERVE[kind] if low else 0) - float(used))

    # что случилось

    def record(self, kind: str, cost: float = 1.0, purpose: str = "") -> None:
        now = self.clock()
        self.spent[kind] = self.spent.get(kind, 0.0) + cost
        with self._db() as conn:
            conn.execute("INSERT INTO da_budget (ts, kind, cost, purpose) VALUES (?, ?, ?, ?)",
                         (now, kind, cost, purpose or None))
            conn.execute("DELETE FROM da_budget WHERE ts < ?", (now - KEEP_ROWS_S,))
            conn.commit()

    def note_limited(self, kind: str, status: int | None, retry_after: float | None = None) -> float:
        """DeviantArt попросил притормозить: пауза для всего класса адресов. Возвращает, до какого времени."""
        now = self.clock()
        self.limited.add(kind)
        wait = min(float(retry_after or 0), RETRY_AFTER_MAX_S)
        with self._db() as conn:
            row = conn.execute("SELECT * FROM da_cooldown WHERE kind = ?", (kind,)).fetchone()
            if row and now < row["until_ts"]:
                # Уже пауза (запрос другого процесса или запись по её команде): ступень не наращиваем.
                until = max(row["until_ts"], now + wait)
                conn.execute("UPDATE da_cooldown SET until_ts = ? WHERE kind = ?", (until, kind))
                conn.commit()
                return until
            level = row["level"] if row and now - row["since_ts"] < FORGET_LEVEL_S else 0
            until = now + max(COOLDOWN_STEPS_S[min(level, len(COOLDOWN_STEPS_S) - 1)], wait)
            conn.execute(
                "INSERT INTO da_cooldown (kind, until_ts, level, since_ts, status) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET until_ts = excluded.until_ts, level = excluded.level, "
                "since_ts = excluded.since_ts, status = excluded.status",
                (kind, until, level + 1, now, status))
            conn.commit()
        log.warning("DeviantArt %s: %s, пауза до %s", kind, status, until_text(until, now))
        return until

    def finish(self) -> None:
        """Конец прохода: класс, где были удачные запросы и не было отказов, снова начинает с 30 минут."""
        now = self.clock()
        clean = [k for k in KINDS if self.ok[k] and k not in self.limited]
        if not clean:
            return
        with self._db() as conn:
            conn.executemany("UPDATE da_cooldown SET level = 0 WHERE kind = ? AND until_ts <= ? AND level > 0",
                             [(k, now) for k in clean])
            conn.commit()


_current: ContextVar[Guard | None] = ContextVar("da_guard", default=None)
# Пауза, которую не удалось записать в базу (занята): держим хотя бы в памяти этого процесса,
# иначе после 403 следующие запросы ушли бы сразу. {kind: until_ts}
_unsaved_cooldown: dict[str, float] = {}
_task: ContextVar[tuple[str, bool]] = ContextVar("da_task", default=("", False))
_default_path: Path | None = None


def enable_default(path: Path | None = None) -> None:
    """Для процессов помощника (проверка, MCP-сервер): запросы вне session() тоже считаются —
    своим коротким соединением с базой. В тестах не включается."""
    global _default_path
    _default_path = path or db.DATA_DIR / "monitor.sqlite3"


def disable_default() -> None:
    global _default_path
    _default_path = None


def _now() -> float:
    """Часы открытого прохода (тесты подставляют свои), иначе настоящее время."""
    guard = _current.get()
    return guard.clock() if guard is not None else time.time()


def current() -> Guard | None:
    guard = _current.get()
    if guard is None and _default_path is not None:
        return Guard(path=_default_path)
    return guard


@contextlib.contextmanager
def session(conn: sqlite3.Connection, clock: Callable[[], float] | None = None) -> Iterator[Guard]:
    """Проход (плановая проверка, инструмент в чате): все запросы к DeviantArt внутри считаются в этой базе.
    Вложенный session() берёт уже открытый. В конце — сброс ступени паузы, если проход был чистым."""
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    guard = Guard(conn, clock=clock)
    token = _current.set(guard)
    try:
        yield guard
    finally:
        _current.reset(token)
        with contextlib.suppress(sqlite3.Error):
            guard.finish()


@contextlib.contextmanager
def task(purpose: str = "", low: bool = False) -> Iterator[None]:
    """Кто тратит запросы (для отчёта) и низкий ли приоритет. Вложенное наследует низкий приоритет."""
    outer, outer_low = _task.get()
    token = _task.set((purpose or outer, low or outer_low))
    try:
        yield
    finally:
        _task.reset(token)


# --- для deviantart._get и DAClient._request ---

def before(kind: str, write: bool = False) -> Block | None:
    """None — можно отправлять; Block — нельзя (запрос не отправляем)."""
    guard = current()
    if guard is None:
        return None
    until = _unsaved_cooldown.get(kind)
    if until is not None and not write:
        if guard.clock() < until:
            return Block(kind, "cooldown", until)
        _unsaved_cooldown.pop(kind, None)
    try:
        return guard.block(kind, low=_task.get()[1], write=write)
    except sqlite3.Error as e:
        log.warning("da_access: база недоступна (%s), запрос не ограничиваю", e)
        return None


def after(kind: str, status: int, headers=None, limited: bool | None = None) -> None:
    """Запрос ушёл и вернулся с ответом: посчитать, а на 403/429 — включить паузу."""
    guard = current()
    if guard is None:
        return
    if limited is None:
        limited = status in (403, 429)
    if limited:
        # Пауза важнее счёта: пишем её первой и отдельно — если база занята, счёт может пропасть,
        # а пауза нет (в крайнем случае остаётся в памяти процесса).
        wait = retry_after_s(headers, guard.clock())
        try:
            guard.note_limited(kind, status, wait)
        except sqlite3.Error as e:
            guard.limited.add(kind)
            step = max(COOLDOWN_STEPS_S[0], min(float(wait or 0), RETRY_AFTER_MAX_S))
            _unsaved_cooldown[kind] = max(_unsaved_cooldown.get(kind, 0.0), guard.clock() + step)
            log.warning("da_access: не записала паузу (%s), держу её в памяти", e)
    elif status < 400:
        guard.ok[kind] += 1
    try:
        guard.record(kind, NOT_MODIFIED_COST if status == 304 else 1.0, _task.get()[0])
    except sqlite3.Error as e:
        log.warning("da_access: не записала запрос (%s)", e)


# --- состояние для отчётов ---

def cooldowns(conn: sqlite3.Connection, now: float | None = None) -> dict[str, sqlite3.Row]:
    """Действующие паузы по классам адресов."""
    now = _now() if now is None else now
    try:
        rows = conn.execute("SELECT * FROM da_cooldown WHERE until_ts > ?", (now,)).fetchall()
    except sqlite3.Error:
        return {}
    return {r["kind"]: r for r in rows}


def blocked(conn: sqlite3.Connection, kind: str, low: bool = False, now: float | None = None) -> Block | None:
    """Можно ли сейчас к этому классу адресов (без запроса): для решений «через API или RSS»."""
    now = _now() if now is None else now
    try:
        return Guard(conn, clock=lambda: now).block(kind, low=low)
    except sqlite3.Error:
        return None


def room(conn: sqlite3.Connection, kind: str, low: bool | None = None, now: float | None = None) -> float:
    """Сколько запросов класса ещё можно в этом часу (для решения «листать ли глубже»)."""
    now = _now() if now is None else now
    low = _task.get()[1] if low is None else low
    try:
        return Guard(conn, clock=lambda: now).room(kind, low=low)
    except sqlite3.Error:
        return float(PER_HOUR[kind])


def usage(conn: sqlite3.Connection, now: float | None = None) -> dict[str, float]:
    now = _now() if now is None else now
    try:
        rows = conn.execute("SELECT kind, SUM(cost) AS s FROM da_budget WHERE ts > ? GROUP BY kind",
                            (now - WINDOW_S,)).fetchall()
    except sqlite3.Error:
        return {}
    return {r["kind"]: float(r["s"] or 0) for r in rows}


def status_line(conn: sqlite3.Connection, api_connected: bool = False, now: float | None = None) -> str | None:
    """Одна строка для отчёта проверки и data_health, если DeviantArt сейчас просит притормозить."""
    now = _now() if now is None else now
    cds = cooldowns(conn, now)
    rss, api = cds.get(RSS), cds.get(API)
    if rss and api:
        return (f"DeviantArt притормозил и ленты, и API до {until_text(max(rss['until_ts'], api['until_ts']), now)}"
                " — подожду, ничего делать не нужно")
    if rss:
        tail = "читаю через API" if api_connected else "подожду, ничего делать не нужно"
        return f"DeviantArt притормозил чтение лент до {until_text(rss['until_ts'], now)} — {tail}"
    if api:
        return (f"DeviantArt притормозил API до {until_text(api['until_ts'], now)} — ставки и статистику обновлю "
                "после, ничего делать не нужно")
    return None


def claim_notice(conn: sqlite3.Connection, now: float | None = None) -> bool:
    """Сказать ли уведомлением про паузу: один раз на серию (первая ступень), а не каждые 15 минут
    и не на каждой ступени, пока DeviantArt продолжает просить подождать."""
    now = _now() if now is None else now
    claimed = False
    for kind, row in cooldowns(conn, now).items():
        if row["notified_since"] is not None and row["level"] > 1:
            continue  # серия уже началась и о ней сказали
        if row["notified_since"] == row["since_ts"]:
            continue
        conn.execute("UPDATE da_cooldown SET notified_since = ? WHERE kind = ?", (row["since_ts"], kind))
        claimed = True
    if claimed:
        conn.commit()
    return claimed


def health_lines(conn: sqlite3.Connection, now: float | None = None) -> list[str]:
    now = _now() if now is None else now
    used = usage(conn, now)
    lines = [f"Запросы к DeviantArt за час: ленты {used.get(RSS, 0):g} из {PER_HOUR[RSS]}, "
             f"API {used.get(API, 0):g} из {PER_HOUR[API]} (бережём, чтобы DeviantArt не закрывал доступ)."]
    line = status_line(conn, False, now)
    if line:
        lines.append(line.split(" — ")[0] + ".")
    return lines
