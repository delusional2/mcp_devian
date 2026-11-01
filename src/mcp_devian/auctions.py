"""Ставки на аукционах: комментарии → разбор → таблица auctions → уведомления о новых ставках.

Комментарии берутся через официальный API DeviantArt (если она подключила DA) или,
по её просьбе в чате, из страницы в её Chrome. В фоне работает только API.
"""

import json
import re
import time
from dataclasses import dataclass

from . import bids, db
from .config import Config

AUCTION_TYPES = ("auction", "bta")
# Сколько чужих аукционов за проход дочитывать ради финальных цен — чтобы не долбить API.
MARKET_SAMPLE_PER_RUN = 5
# «AB1/AB2» — номер автобая вплотную к AB; через пробел идёт уже сумма.
AB_RE = re.compile(r"\b(?:ab\d?|auto[\s-]?buy|автобай)\b\s*[:=\-]?\s*", re.I)


@dataclass
class BidEvent:
    deviation_id: str
    title: str
    url: str
    amount: float
    currency: str | None
    user: str | None


def ab_price(text: str | None) -> float | None:
    """Цена автобая из описания: «AB: $80», «AB 80$», «autobuy - 2000 pts». «AB: N/A» — нет."""
    for m in AB_RE.finditer(text or ""):
        before = (text or "")[max(0, m.start() - 6):m.start()]
        if re.search(r"\b(?:no|без)\s*$", before, re.I):
            return None  # «No AB», «без AB»
        tail = (text or "")[m.end():m.end() + 20]
        if re.match(r"\s*(?:n/?a|none|нет|no|ask|tba|\?+|-\s*$|—)", tail, re.I):
            return None
        # Сумма должна стоять сразу после AB, а не где-то дальше (иначе подхватим SB или MI).
        found = bids.find_amount(tail[:12])
        if found and re.match(r"\s*[$€£]?\s*\d", tail):
            return found[0]
    return None


def to_comments(raw: list) -> list[bids.Comment]:
    out = []
    for c in raw:
        if isinstance(c, bids.Comment):
            out.append(c)
        elif isinstance(c, dict) and c.get("comment_id"):
            out.append(bids.Comment(comment_id=str(c["comment_id"]), parent_id=c.get("parent_id"),
                                    user=str(c.get("user") or ""), posted_ts=float(c.get("posted_ts") or 0),
                                    text=str(c.get("text") or ""), depth=int(c.get("depth") or 0)))
    return out


def analyse(item, comments: list) -> bids.AuctionState:
    currency = "usd" if item["price_usd"] is not None else "points" if item["price_points"] is not None else None
    sb = item["price_usd"] if currency == "usd" else item["price_points"]
    return bids.parse_bids(to_comments(comments), owner=item["author"] or "", sb=sb,
                           ab=ab_price(item["snippet"]), currency=currency)


def save(conn, item, state: bids.AuctionState, comments_seen: int, now: float | None = None) -> BidEvent | None:
    """Сохранить состояние. Вернуть событие, если появилась новая максимальная ставка."""
    now = now or time.time()
    old = conn.execute("SELECT top_amount, n_bids FROM auctions WHERE deviation_id = ?", (item["id"],)).fetchone()
    top = state.top
    conn.execute(
        "INSERT INTO auctions (deviation_id, owner, top_amount, currency, n_bids, bidders, last_bid_ts, ab_hit, ended, "
        "winner, comments_seen, updated_ts, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(deviation_id) DO UPDATE SET owner=excluded.owner, top_amount=excluded.top_amount, "
        "currency=excluded.currency, n_bids=excluded.n_bids, bidders=excluded.bidders, last_bid_ts=excluded.last_bid_ts, "
        "ab_hit=excluded.ab_hit, ended=excluded.ended, winner=excluded.winner, comments_seen=excluded.comments_seen, "
        "updated_ts=excluded.updated_ts, extra=excluded.extra",
        (item["id"], item["author"], top.amount if top else None, state.currency, state.n_bids, state.bidders,
         state.last_bid_ts, int(state.ab_hit), int(state.ended), state.winner, comments_seen, now,
         json.dumps({"notes": state.notes[:10], "top_user": top.user if top else None}, ensure_ascii=False)))
    conn.commit()
    if top is not None and (not old or old["top_amount"] is None or top.amount > old["top_amount"]):
        return BidEvent(item["id"], item["title"], item["url"], top.amount, state.currency, top.user)
    return None


def to_refresh(conn, now: float | None = None, market_sample: int = MARKET_SAMPLE_PER_RUN, own_limit: int = 20) -> list:
    """Её открытые аукционы (последние 3 недели) + немного чужих, у которых ещё нет финальной цены."""
    now = now or time.time()
    types = ",".join(f"'{t}'" for t in AUCTION_TYPES)
    mine = conn.execute(
        f"SELECT * FROM items WHERE source='deviantart' AND own=1 AND lot_type IN ({types}) AND status != 'closed' "
        "AND COALESCE(published_ts, first_seen_ts) >= ? ORDER BY COALESCE(published_ts, first_seen_ts) DESC LIMIT ?",
        (now - 21 * 86400, own_limit)).fetchall()
    market = conn.execute(
        f"SELECT i.* FROM items i LEFT JOIN auctions a ON a.deviation_id = i.id WHERE i.source='deviantart' "
        f"AND i.own=0 AND i.lot_type IN ({types}) AND (a.deviation_id IS NULL OR (a.ended = 0 AND i.status = 'closed')) "
        "AND COALESCE(i.published_ts, i.first_seen_ts) BETWEEN ? AND ? "
        "ORDER BY COALESCE(i.published_ts, i.first_seen_ts) DESC LIMIT ?",
        (now - 30 * 86400, now - 2 * 86400, market_sample)).fetchall()
    return list(mine) + list(market)


def refresh_via_api(conn, cfg: Config, client, now: float | None = None, sleep=time.sleep,
                    market_sample: int = MARKET_SAMPLE_PER_RUN, own_limit: int = 20) -> tuple[int, list[BidEvent]]:
    """Фоновое обновление через API. Возвращает (сколько обновлено, новые ставки на ЕЁ лотах)."""
    events, done = [], 0
    for item in to_refresh(conn, now, market_sample, own_limit):
        try:
            uuid = known_uuid(item) or client.deviation_uuid_from_url(item["url"], max_pages=UUID_LOOKUP_PAGES)
            if not uuid:
                continue
            remember_uuid(conn, item, uuid)
            comments = client.deviation_comments(uuid)
        except Exception:  # noqa: BLE001 — один лот не должен валить весь проход
            continue
        state = analyse(item, comments)
        event = save(conn, item, state, len(comments), now)
        done += 1
        if event and item["own"]:
            events.append(event)
        sleep(1.5)
    return done, events


# API не ищет работу по ссылке: листаем галерею автора. В фоне не глубже 2 страниц (48 работ).
UUID_LOOKUP_PAGES = 2


def known_uuid(item) -> str | None:
    try:
        return json.loads(item["extra"] or "{}").get("uuid")
    except (json.JSONDecodeError, TypeError, IndexError, KeyError):
        return None


def remember_uuid(conn, item, uuid: str) -> None:
    """Найденный UUID запоминаем, чтобы не искать его каждый раз заново."""
    if known_uuid(item) == uuid:
        return
    extra = json.loads(item["extra"] or "{}")
    extra["uuid"] = uuid
    conn.execute("UPDATE items SET extra = ? WHERE id = ?", (json.dumps(extra, ensure_ascii=False), item["id"]))
    conn.commit()


def describe_event(e: BidEvent) -> str:
    who = f" от {e.user}" if e.user else ""
    return f"Новая ставка {bids.money(e.amount, e.currency)}{who} — «{e.title[:60]}»"


def summary_for(conn, item) -> str:
    row = conn.execute("SELECT * FROM auctions WHERE deviation_id = ?", (item["id"],)).fetchone()
    if not row:
        return "ставки ещё не читались"
    extra = json.loads(row["extra"] or "{}")
    parts = [f"ставок {row['n_bids']}" + (f" от {row['bidders']} чел." if row["bidders"] else "")]
    if row["top_amount"] is not None:
        parts.append(f"максимум {bids.money(row['top_amount'], row['currency'])}"
                     + (f" у {extra['top_user']}" if extra.get("top_user") else ""))
    if row["ab_hit"]:
        parts.append("автобай взят")
    if row["ended"]:
        parts.append("закрыт" + (f", победитель {row['winner']}" if row["winner"] else ""))
    return ", ".join(parts)


def item_for_url(conn, url: str):
    m = re.search(r"-(\d+)/?(?:[?#].*)?$", url.strip())
    return db.get_item(conn, f"da:{m.group(1)}") if m else None
