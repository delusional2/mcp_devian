"""Ставки на аукционах: комментарии → разбор → таблица auctions → уведомления о новых ставках.

Комментарии берутся через официальный API DeviantArt (если она подключила DA) или,
по её просьбе в чате, из страницы в её Chrome. В фоне работает только API.
"""

import html
import json
import logging
import re
import time
from dataclasses import dataclass

from . import bids, da_access, da_api, da_browse, da_feeds, db
from . import deviantart as da
from .config import Config

log = logging.getLogger(__name__)

AUCTION_TYPES = ("auction", "bta")
# Сколько чужих аукционов за проход дочитывать ради итогов (была ли ставка). Большинство UUID
# теперь приходит пачкой со страницы папки, поэтому 20; проход всё равно ограничен TIME_BUDGET_S.
MARKET_SAMPLE_PER_RUN = 20
# Отдельный предел на чужие лоты, которым UUID ещё надо искать по галерее автора (до 2 страниц API).
MARKET_LOOKUPS_PER_RUN = 8
# Итог чужого лота считаем известным, только если его дочитали в возрасте 7+ дней: при первом
# чтении на 2-й день «выживают» в основном проданные (у них есть «sold to»), а лоты без ставок
# так и оставались бы непрочитанными — доля лотов со ставкой выходила 100% вместо 50%.
MATURE_READ_DAYS = 7
# Если и на 7+ день в названии всё ещё [OPEN], дочитываем ещё раз в 14+ дней: итог таких лотов
# (чаще всего без ставок) иначе остался бы неизвестным (forecast.FINAL_DAYS).
FINAL_READ_DAYS = 14
# Дочитываем на 7-й день лоты не старше этого (дальше комментарии уже ничего не изменят).
REREAD_WINDOW_DAYS = 45
# Страниц папок групп за проход (по одной на папку): UUID и число комментариев пачкой.
MAX_FOLDER_PAGES = 6
# «AB1/AB2» — номер автобая вплотную к AB; через пробел идёт уже сумма.
AB_RE = re.compile(r"\b(?:ab\d?|auto[\s-]?buy|автобай)\b\s*[:=\-]?\s*", re.I)
# Следующая метка в описании: дальше идёт уже её цена, а не автобая.
NEXT_LABEL = re.compile(r"\b(?:sb|mi|min(?:imum)?|ab\d?|auto[\s-]?buy|автобай|start(?:ing)?\s*bid|мин\w*)\b", re.I)
BARE_NUMBER = re.compile(r"\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d{1,2})?)\s?(k\b)?", re.I)

# Своя строка в runs: когда ставки обновлялись и не просил ли DeviantArt притормозить.
RUN_SOURCE = "da_auctions"
# Повторное «проверь» в чате раньше этого не дёргает API: ставки и так свежие.
MIN_INTERVAL_S = 5 * 60
# После 429 ставки не трогаем столько же, сколько ленту DeviantArt.
RATE_LIMIT_COOLDOWN_S = 30 * 60
# Проход в чате должен уложиться в ожидание Claude Desktop.
TIME_BUDGET_S = 30
# Подряд столько ошибок по разным лотам — значит, лежит сеть или API, дальше не долбим.
MAX_FAILS_IN_ROW = 3
# Ставка при первом чтении лота считается новой, только если она свежая: иначе после
# подключения DA пришли бы «новые ставки» недельной давности.
RECENT_BID_S = 2 * 3600
DISCONNECTED = "DeviantArt отключился — ставки не обновляются. Скажи Claude «подключи DeviantArt заново»."


@dataclass
class BidEvent:
    deviation_id: str
    title: str
    url: str
    amount: float
    currency: str | None
    user: str | None


def _ab_amounts(text: str | None) -> list[tuple[float, str | None]]:
    """Суммы сразу после метки автобая: [(сумма, валюта)], валюта None — число без валюты.
    «No AB», «AB: ???», «AB: ask» — автобая нет, и цена SB/MI дальше по тексту ему не достаётся."""
    text = text or ""
    for m in AB_RE.finditer(text):
        before = text[max(0, m.start() - 12):m.start()]
        if re.search(r"\b(?:no|нет|without|без)\s*[:\-]?\s*$", before, re.I):
            return []
        tail = text[m.end():m.end() + 40]
        nxt = NEXT_LABEL.search(tail)
        tail = tail[:nxt.start()] if nxt else tail
        if not re.match(r"\s*(?:[$€£]\s*)?\d", tail):
            return []  # «AB: ???», «AB: ask», «AB: TBA», «AB SB $10»
        found: list[tuple[float, str | None]] = [(v, c) for _, v, c in bids.find_amounts(tail)]
        if found:
            return found
        bare = BARE_NUMBER.match(tail)
        if bare:
            return [(float(bare.group(1).replace(",", "")) * (1000 if bare.group(2) else 1), None)]
    return []


def ab_price(text: str | None, currency: str | None = None) -> float | None:
    """Цена автобая из описания: «AB: $80», «AB 80$», «autobuy - 2000 pts». «AB: N/A» — нет.
    С валютой лота — только сумма в этой валюте: «AB: $25 or 3000 pts» на лоте за поинты = 3000,
    а «AB: $20» на лоте за поинты — None (сравнивать доллары с поинтами нельзя)."""
    found = _ab_amounts(text)
    if currency is None:
        return found[0][0] if found else None
    for value, cur in found:
        if cur == currency or cur is None:
            return value
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
    ab = ab_price(item["snippet"], currency)
    state = bids.parse_bids(to_comments(comments), owner=item["author"] or "", sb=sb, ab=ab, currency=currency)
    if ab is None and currency:
        other = [(v, c) for v, c in _ab_amounts(item["snippet"]) if c and c != currency]
        if other:
            state.notes.append(f"автобай в другой валюте ({bids.money(*other[0])}), не сравниваю со ставками")
    return state


def _extra(row) -> dict:
    try:
        return json.loads(row["extra"] or "{}") if row else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _field(item, key):
    try:
        return item[key]
    except (IndexError, KeyError):
        return None


def lot_start(item) -> float | None:
    return _field(item, "published_ts") or _field(item, "first_seen_ts")


def save(conn, item, state: bids.AuctionState, comments_seen: int, now: float | None = None,
         zero_by_stats: bool = False) -> BidEvent | None:
    """Сохранить состояние. Вернуть событие, если появилась ставка, о которой она ещё не знает.

    Про ставку говорим один раз: запоминаем, о какой сумме уже сказали (notified_top). Если одно
    чтение пропустило верхний комментарий, а следующее снова его увидело — это не новая ставка.
    При первом чтении лота новыми считаются только свежие ставки (см. RECENT_BID_S).

    В extra ещё: read_age_days — сколько дней было лоту при этом чтении (итог надёжен с 7 дней),
    bidders — кто и сколько ставил (для аналитики рынка), zero_by_stats — 0 комментариев по API
    в возрасте 7+ дней, комментарии не читали."""
    now = now or time.time()
    old = conn.execute("SELECT top_amount, extra FROM auctions WHERE deviation_id = ?", (item["id"],)).fetchone()
    old_extra = _extra(old)
    top = state.top
    notified = old_extra.get("notified_top", old["top_amount"] if old else None)
    notified_ts = old_extra.get("notified_ts")
    if top is None:
        fresh = False
    elif not old:
        fresh = bool(top.ts) and top.ts >= max(item["first_seen_ts"] or 0, now - RECENT_BID_S)
    else:
        # Сумма выше, или после отзыва ставки появилась новая (позже той, о которой говорили).
        fresh = (notified is None or top.amount > notified
                 or (bool(top.ts) and notified_ts is not None and top.amount != notified and top.ts > notified_ts))
    if top is not None and (fresh or not old):
        notified, notified_ts = top.amount, top.ts  # при первом чтении старые ставки — точка отсчёта
    extra = {"notes": state.notes[:10], "top_user": top.user if top else None, "notified_top": notified,
             "notified_ts": notified_ts, "seen_status": item["status"], "bidders": bids.bidder_records(state)}
    start = lot_start(item)
    if start:
        extra["read_age_days"] = round(max(0.0, now - start) / 86400, 3)
    if zero_by_stats:
        extra["zero_by_stats"] = True
    conn.execute(
        "INSERT INTO auctions (deviation_id, owner, top_amount, currency, n_bids, bidders, last_bid_ts, ab_hit, ended, "
        "winner, comments_seen, updated_ts, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(deviation_id) DO UPDATE SET owner=excluded.owner, top_amount=excluded.top_amount, "
        "currency=excluded.currency, n_bids=excluded.n_bids, bidders=excluded.bidders, last_bid_ts=excluded.last_bid_ts, "
        "ab_hit=excluded.ab_hit, ended=excluded.ended, winner=excluded.winner, comments_seen=excluded.comments_seen, "
        "updated_ts=excluded.updated_ts, extra=excluded.extra",
        (item["id"], item["author"], top.amount if top else None, state.currency, state.n_bids, state.bidders,
         state.last_bid_ts, int(state.ab_hit), int(state.ended), state.winner, comments_seen, now,
         json.dumps(extra, ensure_ascii=False)))
    conn.commit()
    if top is not None and fresh:
        return BidEvent(item["id"], item["title"], item["url"], top.amount, state.currency, top.user)
    return None


def _ensure_lookup(conn) -> None:
    """Промахи поиска UUID: лот глубже UUID_LOOKUP_PAGES страниц галереи не ищем каждый проход."""
    conn.execute("CREATE TABLE IF NOT EXISTS auction_lookup (deviation_id TEXT PRIMARY KEY, miss_ts REAL, "
                 "misses INTEGER NOT NULL DEFAULT 0)")


def note_uuid_miss(conn, item_id: str, now: float) -> None:
    _ensure_lookup(conn)
    conn.execute("INSERT INTO auction_lookup (deviation_id, miss_ts, misses) VALUES (?, ?, 1) ON CONFLICT(deviation_id) "
                 "DO UPDATE SET miss_ts = excluded.miss_ts, misses = auction_lookup.misses + 1", (item_id, now))
    conn.commit()


# После промаха ждём base·2^(промахи−1), но не больше 8·base: свои лоты — часы, чужие — дни.
OWN_MISS_BASE_S = 3600
MARKET_MISS_BASE_S = 86400
_MISS_OK = "(l.deviation_id IS NULL OR l.miss_ts + ? * MIN(1 << (l.misses - 1), 8) <= ?)"


def to_refresh(conn, now: float | None = None, market_sample: int = MARKET_SAMPLE_PER_RUN, own_limit: int = 20) -> list:
    """Её открытые аукционы (последние 3 недели) + немного чужих, у которых ещё нет финальной цены.

    Свои — сначала те, что дольше всего не перечитывались, чтобы при ограничении по времени
    очередь шла по кругу. Чужие — сначала ни разу не читанные; закрытый лот перечитываем
    один раз после закрытия (seen_status), а не каждый проход. И каждый чужой лот, который
    читали моложе 7 дней, дочитываем ещё раз в 7+ дней — что бы ни было в названии: иначе
    итоги знали бы только у лотов с «sold to», и доля лотов со ставкой была бы завышена."""
    now = now or time.time()
    _ensure_lookup(conn)
    types = ",".join(f"'{t}'" for t in AUCTION_TYPES)
    mine = conn.execute(
        f"SELECT i.* FROM items i LEFT JOIN auctions a ON a.deviation_id = i.id "
        f"LEFT JOIN auction_lookup l ON l.deviation_id = i.id WHERE i.source='deviantart' AND i.own=1 "
        f"AND i.lot_type IN ({types}) AND i.status != 'closed' AND COALESCE(i.published_ts, i.first_seen_ts) >= ? "
        f"AND {_MISS_OK} ORDER BY COALESCE(a.updated_ts, 0), COALESCE(i.published_ts, i.first_seen_ts) DESC LIMIT ?",
        (now - 21 * 86400, OWN_MISS_BASE_S, now, own_limit)).fetchall()
    start = "COALESCE(i.published_ts, i.first_seen_ts)"
    market = conn.execute(
        f"SELECT i.* FROM items i LEFT JOIN auctions a ON a.deviation_id = i.id "
        f"LEFT JOIN auction_lookup l ON l.deviation_id = i.id WHERE i.source='deviantart' "
        f"AND i.own=0 AND i.lot_type IN ({types}) AND {_MISS_OK} AND ("
        f"((a.deviation_id IS NULL OR (a.ended = 0 AND i.status = 'closed' "
        "AND COALESCE(json_extract(a.extra, '$.seen_status'), '') != 'closed' "
        f"AND (i.closed_ts IS NULL OR a.updated_ts < i.closed_ts))) AND {start} BETWEEN ? AND ?) "
        "OR (a.deviation_id IS NOT NULL AND a.ended = 0 "
        f"AND COALESCE(json_extract(a.extra, '$.read_age_days'), 0) < ? AND {start} BETWEEN ? AND ?) "
        "OR (a.deviation_id IS NOT NULL AND a.ended = 0 AND i.status = 'open' "
        "AND COALESCE(json_extract(a.extra, '$.zero_by_stats'), 0) = 0 "
        f"AND COALESCE(json_extract(a.extra, '$.read_age_days'), 0) < ? AND {start} BETWEEN ? AND ?)) "
        f"ORDER BY a.deviation_id IS NOT NULL, {start} DESC LIMIT ?",
        (MARKET_MISS_BASE_S, now, now - 30 * 86400, now - 2 * 86400,
         MATURE_READ_DAYS, now - REREAD_WINDOW_DAYS * 86400, now - MATURE_READ_DAYS * 86400,
         FINAL_READ_DAYS, now - REREAD_WINDOW_DAYS * 86400, now - FINAL_READ_DAYS * 86400,
         market_sample)).fetchall()
    return list(mine) + list(market)


def ingest_pinglist(conn, cfg: Config, item, comments, now: float | None = None) -> dict | None:
    """Комментарии под ЕЁ работой → пинглист («ping me» / «unping»). Тихо: сбой не мешает ставкам."""
    from . import pinglist

    owner = cfg.deviantart.my_username
    if not owner or not comments:
        return None
    try:
        return pinglist.ingest(conn, item["id"], list(comments), owner=owner, now=now)
    except Exception as e:  # noqa: BLE001
        log.warning("pinglist ingest failed: %s", e)
        return None


def refresh_via_api(conn, cfg: Config, client, now: float | None = None, sleep=None,
                    market_sample: int = MARKET_SAMPLE_PER_RUN, own_limit: int = 20, deadline: float | None = None,
                    clock=time.monotonic, stats: dict | None = None) -> tuple[int, list[BidEvent]]:
    """Фоновое обновление через API. Возвращает (сколько обновлено, новые ставки на ЕЁ лотах).

    Сначала её открытые аукционы. Потом, с низким приоритетом в бюджете da_access, по одной странице
    отслеживаемых папок групп (/gallery/{folderid}): UUID и число комментариев сразу для 24 лотов —
    вместо поиска UUID по галерее автора для каждого. Потом чужие лоты (тоже низкий приоритет).
    Чужой лот 7+ дней с 0 комментариев по API записываем как «0 ставок», комментарии не читаем.
    Поиск UUID по галерее автора для чужих лотов — не больше MARKET_LOOKUPS_PER_RUN за проход.

    Проход останавливается, а не идёт дальше по лотам, если DeviantArt просит притормозить (429),
    подключение пропало, подряд падают несколько лотов или вышло время (deadline по clock()).
    Причина остановки — в stats["stopped"]: rate_limited | not_connected | errors | time."""
    stats = {} if stats is None else stats
    stats["stopped"] = None
    sleep = sleep or time.sleep
    events: list[BidEvent] = []
    done = fails = lookups = 0
    ts = now or time.time()
    briefs: dict[str, dict] = {}
    # Сначала её лоты (обычный приоритет), потом страницы папок и чужие лоты (низкий): если бюджет часа
    # на исходе, на её ставки его хватит, а фоновое подождёт следующего прохода.
    for phase in ("mine", "folders", "market"):
        if stats["stopped"]:
            break
        if phase == "folders":
            try:
                with da_access.task(low=True):
                    briefs = folder_pass(conn, cfg, client, ts, sleep=sleep, deadline=deadline, clock=clock,
                                         stats=stats)
            except da_api.RateLimited as e:
                _stopped(stats, e)
            except da_api.NotConnected:
                stats["stopped"] = "not_connected"
            continue
        # Чужие лоты берём после страниц папок: те уже записали UUID, и искать их по галереям не надо.
        mine = phase == "mine"
        batch = [it for it in to_refresh(conn, now, 0 if mine else market_sample, own_limit if mine else 0)
                 if bool(it["own"]) == mine]
        for item in batch:
            if deadline is not None and clock() >= deadline:
                stats["stopped"] = "time"
                break
            if not item["own"] and zero_by_stats(conn, item, briefs, ts):
                save(conn, item, analyse(item, []), 0, now, zero_by_stats=True)
                stats["zero_by_stats"] = stats.get("zero_by_stats", 0) + 1
                done += 1
                continue
            try:
                # Чужие лоты — для статистики рынка: низкий приоритет в общем бюджете, её лоты важнее.
                with da_access.task(low=not item["own"]):
                    uuid = known_uuid(item)
                    if not uuid:
                        if not item["own"]:
                            if lookups >= MARKET_LOOKUPS_PER_RUN:
                                continue
                            lookups += 1
                        uuid = client.deviation_uuid_from_url(item["url"], max_pages=UUID_LOOKUP_PAGES)
                        if not uuid:
                            note_uuid_miss(conn, item["id"], now or time.time())
                            continue
                        remember_uuid(conn, item, uuid)
                    comments = client.deviation_comments(uuid)
            except da_api.Throttled as e:
                if e.why == "budget" and not item["own"]:
                    stats["limit_note"] = str(e)
                    break  # на чужие лоты бюджета нет — остальные чужие тоже подождут
                _stopped(stats, e)
                break
            except da_api.RateLimited as e:
                _stopped(stats, e)
                break
            except da_api.NotConnected:
                stats["stopped"] = "not_connected"
                break
            except Exception:  # noqa: BLE001 — один лот не должен валить весь проход
                fails += 1
                if fails >= MAX_FAILS_IN_ROW:
                    stats["stopped"] = "errors"
                    break
                continue
            fails = 0
            if item["own"]:
                ingest_pinglist(conn, cfg, item, comments, ts)
            state = analyse(item, comments)
            event = save(conn, item, state, len(comments), now)
            done += 1
            if event and item["own"]:
                events.append(event)
            sleep(1.5)
    return done, events


def _stopped(stats: dict, e: da_api.RateLimited) -> None:
    """rate_limited — DeviantArt ответил 429/403; throttled — запрос не отправляли (пауза или бюджет)."""
    if isinstance(e, da_api.Throttled):
        stats["stopped"] = "rate_limited" if e.why == "cooldown" else "throttled"
        stats["limit_note"] = str(e)
    else:
        stats["stopped"] = "rate_limited"


def folder_targets(conn, cfg: Config) -> list[tuple[str, str]]:
    """Отслеживаемые папки групп, для которых известен UUID папки в API: [(группа, folderid)].

    В ссылке на папку номер (…/gallery/26780440), а API нужен UUID; его даёт страница группы
    (gallectionUuid, см. deviantart.folder_meta), он хранится в da_folders. Без UUID папку пропускаем."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for g in cfg.deviantart.groups:
        try:
            folder = da.parse_folder_url(g.url)
        except da.DAError:
            continue
        row = conn.execute("SELECT uuid FROM da_folders WHERE grp = ? AND folder_id = ?",
                           (folder.group.lower(), folder.folder_id)).fetchone()
        if row and row["uuid"] and row["uuid"].upper() not in seen:
            seen.add(row["uuid"].upper())
            out.append((folder.group, row["uuid"]))
    return out


def folder_pass(conn, cfg: Config, client, now: float, sleep=time.sleep, deadline: float | None = None,
                clock=time.monotonic, stats: dict | None = None) -> dict[str, dict]:
    """По одной странице каждой папки: {номер работы: {uuid, comments, favourites, …}}.

    Вызывается с низким приоритетом (refresh_via_api): кончился бюджет фоновой работы — возвращаем, что успели.
    Для лотов, которые уже есть в базе, запоминаем UUID и пишем снимок комментариев/фавов.
    Порядок выдачи папки не документирован — на «сначала новые» не полагаемся.
    RateLimited / NotConnected летят наверх: проход прекращается."""
    briefs: dict[str, dict] = {}
    # Папку, которую лента только что прочитала через API, не перечитываем: UUID и снимок уже есть.
    # Остальные — сначала те, чью ленту дольше всех не читали (иначе последние папки в настройках
    # не доходили бы до очереди никогда).
    fresh = da_feeds.fresh_api_folders(conn, now)
    last = {r["uuid"].upper(): r["last_read_ts"] or 0 for r in conn.execute(
        "SELECT uuid, last_read_ts FROM da_feeds WHERE uuid IS NOT NULL")}
    targets = sorted([t for t in folder_targets(conn, cfg) if t[1].upper() not in fresh],
                     key=lambda t: last.get(t[1].upper(), 0))
    for group, folderid in targets[:MAX_FOLDER_PAGES]:
        if deadline is not None and clock() >= deadline:
            break
        try:
            page = da_browse.folder_page(client, group, folderid, sleep=sleep)
        except da_api.Throttled as e:
            if e.why != "budget":
                raise
            break  # бюджет фоновой работы на час кончился: что успели — сохраняем
        except (da_api.RateLimited, da_api.NotConnected):
            raise
        except Exception as e:  # noqa: BLE001 — одна папка не должна валить проход
            log.warning("folder page %s/%s failed: %s", group, folderid, e)
            continue
        for b in page:
            briefs[b["number"]] = b
    mapped = 0
    for number, b in briefs.items():
        row = db.get_item(conn, f"da:{number}")
        if not row:
            continue
        remember_uuid(conn, row, b["uuid"])
        conn.execute("INSERT OR IGNORE INTO deviation_stats (deviation_id, ts, views, favourites, comments) "
                     "VALUES (?, ?, NULL, ?, ?)", (row["id"], now, b.get("favourites"), b.get("comments")))
        mapped += 1
    conn.commit()
    if stats is not None and briefs:
        stats["folder_uuids"] = mapped
    return briefs


def zero_by_stats(conn, item, briefs: dict[str, dict], now: float) -> bool:
    """Лоту 7+ дней и API говорит «0 комментариев» — ставок нет, комментарии читать незачем.
    Снимок из базы годится, только если снят в возрасте лота 7+ дней."""
    start = lot_start(item)
    if not start or now - start < MATURE_READ_DAYS * 86400:
        return False
    number = str(item["id"]).removeprefix("da:")
    b = briefs.get(number)
    if b is not None and b.get("comments") is not None:
        return b["comments"] == 0
    row = conn.execute("SELECT comments, ts FROM deviation_stats WHERE deviation_id = ? AND comments IS NOT NULL "
                       "ORDER BY ts DESC LIMIT 1", (item["id"],)).fetchone()
    return bool(row) and row["comments"] == 0 and row["ts"] >= start + MATURE_READ_DAYS * 86400


def gate(conn, now: float) -> str | None:
    """Почему сейчас не обновлять ставки (строка для отчёта) или None — можно."""
    row = db.last_run(conn, RUN_SOURCE)
    if not row or not row["last_run_ts"]:
        return None
    ago = now - row["last_run_ts"]
    err = row["last_error"] or ""
    if err.startswith("429") and ago < RATE_LIMIT_COOLDOWN_S:
        return (f"ставки: DeviantArt просил притормозить, обновлю через "
                f"{int((RATE_LIMIT_COOLDOWN_S - ago) // 60) + 1} мин")
    if not err and ago < MIN_INTERVAL_S:
        return f"ставки: обновлено {int(ago // 60)} мин назад, данные свежие"
    return None


def note_disconnected(conn, cfg: Config, now: float, lost_now: bool = False) -> list[str]:
    """DeviantArt был подключён, а теперь нет: сказать ей один раз (уведомлением) и в каждом отчёте.
    Если DA никогда не подключали — молчим: ставки без него просто не читаются."""
    row = db.last_run(conn, RUN_SOURCE)
    if not lost_now and (not row or not row["last_ok_ts"]):
        return []
    if not row or row["last_error"] != DISCONNECTED:
        db.record_run(conn, RUN_SOURCE, DISCONNECTED, now)
        from . import digest

        if cfg.notify.macos:
            digest.notify_macos("mcp_devian", DISCONNECTED)
        if cfg.notify.telegram:
            digest.send_telegram([html.escape(DISCONNECTED)])
    return [f"ставки: {DISCONNECTED}"]


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
