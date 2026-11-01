"""Похожие продавцы: художники её уровня (±1 по вотчерам) в её нише — что и почём они выкладывают.

Только чтение: одна страница свежей галереи одного художника за проход (низкий приоритет в общем
бюджете da_access) — никаких вотчей, фавов и комментариев, никогда. Новые лоты ложатся в items
как рынок (extra.similar=1); дальше их итоги дочитывает обычный проход ставок, как у любых чужих лотов.

Отчёт: на художника — лотов в неделю, медиана старта/цены, доля аукционов со ставкой — и то же у неё.
Каждое число с меткой evidence.tag; меньше evidence.MIN_SIDE лотов — «вывода нет». Темп считаем только
за то время, что видно по чтениям его галереи (similar_state.cover_from), и не меньше MIN_SPAN_DAYS.
Сравнение с ней — с поправкой на число сравнений (evidence.family_threshold).
"""

import contextlib
import json
import logging
import sqlite3
import statistics
import time
from collections import Counter
from typing import Any

from . import da_access, da_api, da_feeds, db, evidence
from .classify import classify_lot
from .forecast import (
    MATURE_DAYS,
    MIN_COVERAGE,
    TRADING_TYPES,
    WATCHER_TIERS,
    SellerTiers,
    her_name,
    her_tier,
    outcome_known,
)
from .taxonomy import tag_text

log = logging.getLogger(__name__)

__all__ = ["ensure", "peers", "step", "report"]

DAY = 86400
PEER_DAYS = 90
MIN_PEER_LOTS = 3
MAX_PEERS = 8
EVERY_H = 6
PAUSE_S = 1.5
VIA = "similar"
MIN_SPAN_DAYS = 14            # темп по меньшему отрезку галереи не считаем
TIER_ORDER = [key for key, _, _ in WATCHER_TIERS]
TIER_LABEL = {key: label for key, label, _ in WATCHER_TIERS}

SCHEMA = """
CREATE TABLE IF NOT EXISTS similar_state (
    author TEXT PRIMARY KEY,       -- ник в нижнем регистре
    last_read_ts REAL,
    last_error TEXT,
    drops INTEGER NOT NULL DEFAULT 0,  -- новых лотов найдено всего
    cover_from REAL                -- с какой даты публикации его галерея видна целиком (0 — вся)
);
"""


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    if "cover_from" not in {r[1] for r in conn.execute("PRAGMA table_info(similar_state)")}:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE similar_state ADD COLUMN cover_from REAL")


def _extra(row) -> dict:
    try:
        v = json.loads(row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def _tags(row) -> set[str]:
    extra = _extra(row)
    raw = [*(extra.get("tags") or []), *(extra.get("api_tags") or [])]
    t = tag_text(row["title"] or "", [x for x in raw if isinstance(x, str)])
    return set(t.species) | set(t.styles)


def _her(conn, cfg=None, me: str | None = None) -> str:
    name = me or (getattr(getattr(cfg, "deviantart", None), "my_username", "") if cfg else "") or her_name(conn) or ""
    return name.strip().lstrip("@").lower()


def _niche(conn, cfg=None) -> set[str]:
    if cfg is not None:
        from .da_market import niche_terms

        return niche_terms(conn, cfg)
    sp: Counter = Counter()
    st: Counter = Counter()
    for r in conn.execute("SELECT title, extra FROM items WHERE source = 'deviantart' AND own = 1"):
        extra = _extra(r)
        raw = [*(extra.get("tags") or []), *(extra.get("api_tags") or [])]
        t = tag_text(r["title"] or "", [x for x in raw if isinstance(x, str)])
        sp.update(t.species)
        st.update(t.styles)
    return {s for s, _ in sp.most_common(3)} | {s for s, _ in st.most_common(2)}


def _adjacent(a: str | None, b: str) -> bool:
    if a not in TIER_ORDER or b not in TIER_ORDER:
        return False
    return abs(TIER_ORDER.index(a) - TIER_ORDER.index(b)) <= 1


def peers(conn, now: float | None = None, manual: list[str] | None = None, max_peers: int = MAX_PEERS,
          niche: set[str] | None = None, cfg=None, me: str | None = None) -> list[str]:
    """Её ручной список + авторы её уровня или соседнего (по вотчерам), у кого ≥3 лота её ниши за 90 дн.
    Её саму не берём никогда. Уровень её неизвестен или ниша пуста — только ручной список."""
    now = now or time.time()
    her = _her(conn, cfg, me)
    out: list[str] = []
    for name in manual or []:
        n = str(name or "").strip().lstrip("@").lower()
        if n and n != her and n not in out:
            out.append(n)
    tier = her_tier(conn, now)
    niche = _niche(conn, cfg) if niche is None else niche
    if not tier.known or not niche:
        return out[:max_peers]
    tiers = SellerTiers(conn)
    types = ",".join(f"'{t}'" for t in TRADING_TYPES)
    counts: Counter = Counter()
    for r in conn.execute(f"SELECT author, title, extra FROM items WHERE source = 'deviantart' AND own = 0 "
                          f"AND author IS NOT NULL AND lot_type IN ({types}) "
                          f"AND COALESCE(published_ts, first_seen_ts) >= ?", (now - PEER_DAYS * DAY,)):
        if niche & _tags(r):
            counts[(r["author"] or "").lower()] += 1
    auto = [(a, c) for a, c in counts.items() if a and a != her and c >= MIN_PEER_LOTS
            and _adjacent(tiers.tier(a), tier.key)]
    auto.sort(key=lambda ac: (tiers.tier(ac[0]) != tier.key, -ac[1], ac[0]))
    for a, _ in auto:
        if a not in out:
            out.append(a)
    return out[:max_peers]


# --- чтение ---

def _gallery_page(client, user: str, sleep) -> tuple[list[dict], float | None]:
    """Одна страница «Всех» работ художника, свежие сверху: с датой и статистикой (через /gallery/all).
    → (работы, с какой даты страница покрывает галерею: самая старая работа; 0 — галерея вся на странице)."""
    try:
        body = client._request("GET", "/gallery/all", params={"username": user, "limit": 24, "offset": 0,
                                                              "mature_content": "false"})
    finally:
        sleep(PAUSE_S)
    entries = [e for e in (da_feeds.api_entry(d, f"похожий художник {user}") for d in body.get("results") or []
                           if isinstance(d, dict)) if e]
    if not body.get("has_more"):
        return entries, 0.0
    dated = [e["published_ts"] for e in entries if e["published_ts"] is not None]
    return entries, (min(dated) if dated else None)


def _store(conn, e: dict, now: float, user: str) -> bool:
    lot = classify_lot(e["title"], e["text"], e["tags"])
    if lot.lot_type not in TRADING_TYPES:
        return False
    from . import deviantart as da

    item = da.entry_to_item(e)
    item.lot_type, item.status, item.price_text = lot.lot_type, lot.status, lot.price_text
    item.price_usd, item.price_points = lot.usd, lot.points
    item.extra.update({"multi": bool(lot.multi), "similar": 1, "via": [VIA]})
    old = db.get_item(conn, item.id)
    if old:
        item.matched, item.match_kind, item.match_reason = bool(old["matched"]), old["match_kind"], old["match_reason"]
    new = db.upsert_item(conn, item, now)
    if e.get("comments") is not None:
        conn.execute("INSERT OR IGNORE INTO deviation_stats (deviation_id, ts, views, favourites, comments) "
                     "VALUES (?, ?, NULL, ?, ?)", (item.id, now, e.get("favourites"), e.get("comments")))
    return new


def step(conn, client, now: float | None = None, manual: list[str] | None = None, max_artists: int = 1,
         every_h: float = EVERY_H, sleep=time.sleep, niche: set[str] | None = None, cfg=None,
         me: str | None = None) -> list[str]:
    """Прочитать свежую страницу галереи одного похожего художника (давно не читанного). Только чтение."""
    now = now or time.time()
    ensure(conn)
    names = peers(conn, now, manual, niche=niche, cfg=cfg, me=me)
    if not names:
        return []
    last = {r["author"]: r["last_read_ts"] or 0 for r in conn.execute("SELECT author, last_read_ts FROM similar_state")}
    due = sorted((n for n in names if now - last.get(n, 0) >= every_h * 3600), key=lambda n: (last.get(n, 0), n))
    lines: list[str] = []
    with da_access.session(conn), da_access.task("similar", low=True):
        if due and da_access.blocked(conn, da_access.API, low=True, now=now) is not None:
            return []
        for user in due[:max(0, max_artists)]:
            error = None
            new = 0
            cover: float | None = None
            try:
                entries, cover = _gallery_page(client, user, sleep)
                fresh = []
                for e in entries:
                    row = db.get_item(conn, f"da:{e['deviation_id']}")
                    extra = _extra(row) if row else {}
                    if row and extra.get("desc_ts"):  # полное описание уже есть — из базы, цена не пропадёт
                        e["text"] = str(extra.get("desc") or row["snippet"] or e["text"])
                        e["tags"] = e["tags"] or [str(t) for t in extra.get("tags") or []]
                    else:  # нового нет или в базе только отрывок (метаданные тогда не пришли) — дочитать
                        fresh.append(e)
                if fresh:  # описание нужно для цены — одним запросом метаданных на страницу
                    da_feeds.fill_text(conn, client, fresh, now, sleep=sleep)
                for e in entries:
                    new += int(_store(conn, e, now, user))
            except (da_api.RateLimited, da_api.NotConnected) as e:
                lines.append(f"похожие художники: {e} — продолжу позже")
                break
            except da_api.DAApiError as e:
                error = str(e)[:200]
            conn.execute("INSERT INTO similar_state (author, last_read_ts, last_error, drops, cover_from) "
                         "VALUES (?, ?, ?, ?, ?) ON CONFLICT(author) DO UPDATE SET last_read_ts = excluded.last_read_ts, "
                         "last_error = excluded.last_error, drops = similar_state.drops + excluded.drops, "
                         "cover_from = CASE WHEN excluded.cover_from IS NULL THEN similar_state.cover_from "
                         "WHEN similar_state.cover_from IS NULL THEN excluded.cover_from "
                         "ELSE MIN(similar_state.cover_from, excluded.cover_from) END",
                         (user, now, error, new, cover))
            conn.commit()
            lines.append(f"похожие художники: {user} — новых лотов {new}" if not error
                         else f"похожие художники: {user} — не прочиталось ({error[:60]})")
    return lines


# --- отчёт ---

def _lots(conn, author: str | None, own: bool, since: float, until: float) -> list:
    types = ",".join(f"'{t}'" for t in TRADING_TYPES)
    who = "own = 1" if own else "own = 0 AND lower(author) = ?"
    params: list[Any] = [] if own else [author]
    return conn.execute(
        f"SELECT i.*, a.n_bids AS a_n_bids, a.ended AS a_ended, a.extra AS a_extra, a.updated_ts AS a_updated "
        f"FROM items i LEFT JOIN auctions a ON a.deviation_id = i.id WHERE i.source = 'deviantart' AND {who} "
        f"AND i.lot_type IN ({types}) AND COALESCE(i.published_ts, i.first_seen_ts) BETWEEN ? AND ?",
        [*params, since, until]).fetchall()


def _known(r) -> bool:
    if r["a_updated"] is None:
        return False
    try:
        a_extra = json.loads(r["a_extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        a_extra = {}
    return outcome_known(ended=r["a_ended"], status=r["status"], closed_ts=r["closed_ts"],
                         a_extra=a_extra if isinstance(a_extra, dict) else {}, a_updated=r["a_updated"],
                         published=r["published_ts"] or r["first_seen_ts"], item_updated=r["updated_ts"])


def _money(v: float, unit: str) -> str:
    return f"{v:g} pts" if unit == "points" else f"${v:g}"


def _rate(rows: list, who: str, days: int, now: float, cover_from: float | None) -> str:
    """Лотов в неделю — только за видимый отрезок галереи: cover_from None — галерею не читали."""
    if cover_from is None:
        return "темп: вывода нет (его галерею ещё не читали — видны только лоты из групп)"
    start = max(now - days * DAY, cover_from)
    span_days = (now - start) / DAY
    if span_days < MIN_SPAN_DAYS:
        return f"темп: вывода нет (галерея видна за {span_days:.0f} дн из нужных {MIN_SPAN_DAYS})"
    ts = [t for r in rows if (t := r["published_ts"] or r["first_seen_ts"]) is not None and t >= start]
    if not ts:
        return f"лотов за {span_days:.0f} дн не видно"
    ev = evidence.span(ts, "market_da", f"лоты {who}", None, "observed", n=len(ts), measure="count", data_as_of=now)
    return f"{len(ts) / (span_days / 7):.1f} лота/нед (за {span_days:.0f} дн) {evidence.tag(ev)}"


def _metrics(rows: list, who: str, days: int, now: float, cover_from: float | None = 0.0) -> dict[str, str]:
    """Три числа с метками: темп, цена, доля аукционов со ставкой."""
    ts = [r["published_ts"] or r["first_seen_ts"] for r in rows]
    pop = f"лоты {who}"
    out: dict[str, str] = {"rate": _rate(rows, who, days, now, cover_from)}
    by_unit: dict[str, list[tuple[float, float]]] = {"usd": [], "points": []}
    for r, t in zip(rows, ts, strict=True):
        if r["price_usd"] is not None:
            by_unit["usd"].append((float(r["price_usd"]), t))
        elif r["price_points"] is not None:
            by_unit["points"].append((float(r["price_points"]), t))
    unit = max(by_unit, key=lambda u: len(by_unit[u]))
    prices = by_unit[unit]
    if len(prices) < evidence.MIN_SIDE:
        out["price"] = f"цена: вывода нет (с ценой {len(prices)} из нужных {evidence.MIN_SIDE})"
    else:
        vals = [p for p, _ in prices]
        med = statistics.median(vals)
        lo, hi = evidence.median_ci(vals)
        pev = evidence.span([t for _, t in prices], "market_da", pop, unit, "observed", measure="price",
                            value=med, interval=(lo, hi), data_as_of=now)
        out["price"] = f"медиана старта/цены {_money(med, unit)} ({_money(lo, unit)}–{_money(hi, unit)}) {evidence.tag(pev)}"
    auctions_ = [r for r in rows if r["lot_type"] in ("auction", "bta")]
    known = [r for r in auctions_ if _known(r)]
    k = sum(1 for r in known if (r["a_n_bids"] or 0) > 0)
    out["k"], out["n"] = str(k), str(len(known))
    # Покрытие, как в forecast.niches: её лоты читаются постоянно, чужие — раз в 6+ ч. Если у кого-то итог
    # известен у малой доли закончившихся аукционов, доля «со ставкой» у него завышена — сравнивать нельзя.
    mature = [r for r in auctions_ if (r["published_ts"] or r["first_seen_ts"] or now) <= now - MATURE_DAYS * DAY]
    mature_known = sum(1 for r in mature if _known(r))
    if mature and mature_known / len(mature) < MIN_COVERAGE:
        out["n"] = "0"
        out["sold"] = (f"доля со ставкой: вывода нет (итог известен у {mature_known} из {len(mature)} аукционов "
                       f"старше {MATURE_DAYS} дн, нужно ≥{evidence.pct(MIN_COVERAGE)})")
    elif len(known) < evidence.MIN_SIDE:
        out["sold"] = f"доля со ставкой: вывода нет (итог известен у {len(known)} из нужных {evidence.MIN_SIDE})"
    else:
        lo, hi = evidence.wilson(k, len(known))
        sev = evidence.span([r["published_ts"] or r["first_seen_ts"] for r in known], "market_da",
                            f"аукционы {who}", None, "observed", interval=(lo, hi), value=k / len(known),
                            data_as_of=now)
        out["sold"] = (f"со ставкой {evidence.pct(k / len(known))} ({evidence.rate_interval(lo, hi)}) "
                       f"{evidence.tag(sev)}")
    return out


def report(conn, now: float | None = None, days: int = 30, manual: list[str] | None = None, cfg=None,
           me: str | None = None) -> str:
    """Похожие продавцы против неё за days дней: темп, цена, доля со ставкой. Только наблюдения."""
    now = now or time.time()
    ensure(conn)
    names = peers(conn, now, manual, cfg=cfg, me=me)
    for r in conn.execute("SELECT author FROM similar_state ORDER BY author"):
        if r["author"] not in names and r["author"] != _her(conn, cfg, me):
            names.append(r["author"])
    tier = her_tier(conn, now)
    if not names:
        why = ("её уровень (по вотчерам) пока неизвестен" if not tier.known
               else "в её нише пока нет продавцов её уровня с ≥3 лотами за 90 дн")
        return (f"Похожих продавцов пока нет: {why}. Можно назвать художников вручную "
                "(update_settings: deviantart.similar_artists).")
    since = now - days * DAY
    tiers = SellerTiers(conn)
    mine = _metrics(_lots(conn, None, True, since, now), "её", days, now)
    lines = [f"Похожие продавцы за {days} дн (её уровень: {tier.label}; только чтение их галерей, никому не пишем):",
             f"• она: {mine['rate']}; {mine['price']}; {mine['sold']}"]
    cover = {r["author"]: r["cover_from"] for r in conn.execute("SELECT author, cover_from FROM similar_state")}
    shown = names[:MAX_PEERS]
    threshold = evidence.family_threshold(len(shown))   # много сравнений сразу — порог строже
    for name in shown:
        m = _metrics(_lots(conn, name, False, since, now), name, days, now, cover.get(name))
        label = TIER_LABEL.get(tiers.tier(name) or "", "уровень неизвестен")
        k1, n1, k0, n0 = int(mine["k"]), int(mine["n"]), int(m["k"]), int(m["n"])
        if min(n1, n0) < evidence.MIN_SIDE:
            verdict = (f"сравнение с ней: вывода нет (итогов {min(n1, n0)} из нужных {evidence.MIN_SIDE}"
                       " или итоги известны не у всех)")
        else:
            cmp = evidence.compare(k1, n1, k0, n0, threshold=threshold)
            verdict = {"better": "у неё ставка бывает чаще", "worse": "у неё ставка бывает реже"}.get(
                cmp or "", "разницы с ней по доле со ставкой не видно")
        lines.append(f"• {name} ({label}): {m['rate']}; {m['price']}; {m['sold']}; {verdict}")
    lines.append("Это наблюдения за чужими лотами, не совет копировать цены.")
    return "\n".join(lines)
