"""Рынок её ниши на DeviantArt за пределами папок, которые она назвала. Только чтение, без уведомлений.

Зачем: в отслеживаемой папке видно лишь малую часть предложения ниши (проверка 01.11.2026:
из 27 свежих аукционов ниши, найденных поиском, в папке не было ни одного). Прогнозу нужны
итоги аукционов именно её ниши, поэтому:

* слой A — поиск RSS по обычным словам («dragon adoptable auction»): работает без входа,
  листается, но ранжирует по релевантности/популярности, не по дате. Операторы tag:, sort:,
  max_age:, boost: DeviantArt больше не понимает и отдаёт общую ленту «Popular» — такие
  запросы ловим (deviantart.IgnoredQuery) и ничего из них не сохраняем;
* слой B — теги через API (/browse/tags), если DeviantArt подключён: сразу с UUID, датой и
  числом комментариев, без поиска UUID по галерее автора.

Честность: найденное поиском перекошено в сторону популярного. Годится для диапазона цен и
итогов, но не для доли продаж и не для «перегретости» (предложение считается только по
постоянным папкам). Лоты, найденные только поиском, помечены местом «поиск DA: …».

Ещё здесь: кто ставит в её нише (buyers) и карточка групп DA по рынку (group_scorecard).
"""

import contextlib
import json
import logging
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import da_access, da_api, da_browse, db
from . import deviantart as da
from .classify import classify_lot
from .config import Config
from .db import SEARCH_PLACE_PREFIX
from .taxonomy import tag_text

log = logging.getLogger(__name__)

__all__ = ["SEARCH_PLACE_PREFIX", "SampleResult", "niche_profile", "niche_queries", "sample_market", "maybe_sample",
           "render", "buyers", "render_buyers", "group_scorecard", "group_scorecard_many", "render_scorecard"]

RUN_SOURCE = "da_sampler"
TAGS_SUGGEST_RUN = "da_tags_suggest"
TAGS_BROWSE_RUN = "da_tags_browse"
SAMPLE_EVERY_S = 6 * 3600          # слой A: ≤6 запросов раз в 6 ч = ≤24 RSS-запроса в сутки
TAGS_SUGGEST_EVERY_S = 7 * 86400   # какие теги ниши существуют — раз в неделю
TAGS_BROWSE_EVERY_S = 3600         # слой B не чаще раза в час
HISTORY_DAYS = 180                 # как forecast.HISTORY_DAYS: старше — в прогноз всё равно не идёт
MAX_QUERIES = 6
MAX_RSS_PAGES = 2
MAX_API_CALLS = 4                  # вызовов слоя B за проход (подсказки тегов + /browse/tags), без метаданных
MAX_SUGGEST_CALLS = 2
DEFAULT_QUERIES = ["adoptable auction", "adopt"]
DEFAULT_TAGS = ["adoptauction", "adoptable"]
RATE_LIMIT_COOLDOWN_S = 30 * 60
HONESTY = ("поиск DA ранжирует по популярности — эти лоты годятся для диапазона цен и итогов, "
           "но не для доли продаж и не для «перегретости»")

TAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS da_market_tags (
    tag TEXT PRIMARY KEY,
    ts REAL NOT NULL,              -- когда нашли подсказкой
    browsed_ts REAL                -- когда последний раз читали работы с этим тегом
);
"""


@dataclass
class SampleResult:
    queries: list[str] = field(default_factory=list)
    found: int = 0          # всего работ в выдаче (RSS + API)
    new: int = 0            # новых лотов в базе
    priced: int = 0         # из сохранённых — с разобранной ценой
    too_old: int = 0        # старше 180 дней, отброшены
    ignored: list[str] = field(default_factory=list)   # запросы, которые DeviantArt не понял
    via_api: int = 0        # из найденного — через теги API
    rate_limited: bool = False
    api_rate_limited: bool = False  # 429 пришёл от API (а не от RSS)
    not_lots: int = 0       # арт, журналы и т.п. — не лоты, не сохраняем
    stored: int = 0         # сколько лотов записали/обновили
    tags: list[str] = field(default_factory=list)      # какие теги API читали
    errors: list[str] = field(default_factory=list)
    limit_note: str | None = None  # свой бюджет запросов кончился (не DeviantArt просил) — что сказать


# --- ниша ---

def _extra(row) -> dict:
    try:
        value = json.loads(row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError, IndexError, KeyError):
        return {}
    return value if isinstance(value, dict) else {}


def _str_list(value) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def niche_profile(conn, cfg: Config) -> tuple[Counter, Counter]:
    """(виды, стили) её ниши со счётчиками: её персонажи, её лоты DA и её ключевые слова."""
    species: Counter = Counter()
    styles: Counter = Counter()

    def add(title: str, tags=(), text: str = "") -> None:
        t = tag_text(title, tags, text)
        species.update(t.species)
        styles.update(t.styles)

    for ch in cfg.posting.characters or []:
        if isinstance(ch, dict):
            add(str(ch.get("name") or ""), _str_list(ch.get("tags")))
    for row in conn.execute("SELECT title, extra FROM items WHERE own = 1 AND source = 'deviantart'"):
        extra = _extra(row)
        add(row["title"] or "", _str_list(extra.get("tags")) + _str_list(extra.get("api_tags")))
    for kw in cfg.filters.keywords or []:
        add(str(kw))
    return species, styles


def _top(species: Counter, styles: Counter) -> tuple[list[str], list[str]]:
    return [s for s, _ in species.most_common(3)], [s for s, _ in styles.most_common(2)]


def niche_terms(conn, cfg: Config) -> set[str]:
    sp, st = _top(*niche_profile(conn, cfg))
    return set(sp) | set(st)


def niche_queries(conn, cfg: Config) -> list[str]:
    """До 6 поисковых запросов обычными словами: «<вид> adoptable auction», «<стиль> adoptable auction»,
    «<вид> adopt». Без данных о нише — общие «adoptable auction» и «adopt»."""
    sp, st = _top(*niche_profile(conn, cfg))
    if not sp and not st:
        return list(DEFAULT_QUERIES)
    queries = [f"{s} adoptable auction" for s in sp] + [f"{s} adoptable auction" for s in st] + [f"{s} adopt" for s in sp]
    return list(dict.fromkeys(queries))[:MAX_QUERIES]


# --- слой A: поиск RSS ---

def _too_old(ts: float | None, now: float) -> bool:
    return ts is not None and now - ts > HISTORY_DAYS * 86400


def _keep_old_match(conn, item: db.Item) -> None:
    """Поиск не должен сбрасывать то, что о лоте решила лента (находка, причина)."""
    old = db.get_item(conn, item.id)
    if old:
        item.matched = bool(old["matched"])
        item.match_kind = old["match_kind"]
        item.match_reason = old["match_reason"]


def _store(conn, cfg: Config, item: db.Item, lot, now: float, res: SampleResult) -> None:
    item.lot_type, item.status, item.price_text = lot.lot_type, lot.status, lot.price_text
    item.price_usd, item.price_points = lot.usd, lot.points
    item.extra["multi"] = bool(getattr(lot, "multi", False))
    me = (cfg.deviantart.my_username or "").lower()
    item.own = bool(me and (item.author or "").lower() == me)
    item.matched, item.match_kind, item.match_reason = False, None, None
    _keep_old_match(conn, item)
    if item.own:
        item.matched, item.match_kind = False, None
    if lot.usd is not None or lot.points is not None:
        res.priced += 1
    res.stored += 1
    if db.upsert_item(conn, item, now):
        res.new += 1


def _store_entry(conn, cfg: Config, entry: dict, query: str, now: float, res: SampleResult) -> None:
    res.found += 1
    if _too_old(entry["published_ts"], now):
        res.too_old += 1
        return
    lot = classify_lot(entry["title"], entry["text"], entry["tags"])
    if lot.lot_type == "other":
        res.not_lots += 1
        return
    item = da.entry_to_item(entry)
    item.place = f"{SEARCH_PLACE_PREFIX}: {query}"
    # places — только настоящие папки/галереи; откуда нашли поиском — в via.
    item.extra["places"] = []
    item.extra["via"] = [f"search:{query}"]
    _store(conn, cfg, item, lot, now, res)


def _layer_a(conn, cfg: Config, http, queries: list[str], now: float, pages: int, pause, res: SampleResult) -> None:
    for i, q in enumerate(queries):
        if i:
            pause()
        try:
            entries = da.fetch_feed(http, q, f"{SEARCH_PLACE_PREFIX}: {q}", pages=min(max(1, pages), MAX_RSS_PAGES),
                                    pause=pause)
        except da.IgnoredQuery:
            res.ignored.append(q)
            continue
        except da.RateLimited as e:
            res.rate_limited = True
            if isinstance(e, da.Throttled):
                res.limit_note = str(e)
            return
        except (da.DAError, OSError) as e:
            res.errors.append(f"«{q}»: {e}")
            continue
        for entry in entries:
            _store_entry(conn, cfg, entry, q, now, res)
        conn.commit()


# --- слой B: теги через API ---

def _compact_tag(query: str) -> str:
    return re.sub(r"[^a-z0-9]", "", query.lower())


def _explicit_tag(query: str) -> str | None:
    """«dragon» / «dragon adopt» → слитный тег «dragonadopt». Длинные фразы тегами не бывают — None."""
    if len(query.split()) > 2:
        return None
    tag = _compact_tag(query)
    if not tag:
        return None
    return tag if "adopt" in tag else tag + "adopt"


def _tags_to_browse(conn, species: list[str], explicit: list[str] | None) -> list[str]:
    """Ручной запрос — только его теги. Иначе «<вид>adopt» для всех её главных видов плюс найденные
    подсказкой теги этих видов; давно не читанные — первыми, так за несколько проходов обходим все."""
    if explicit:
        return [t for t in dict.fromkeys(_explicit_tag(q) for q in explicit) if t][:MAX_API_CALLS]
    rows = conn.execute("SELECT tag, browsed_ts FROM da_market_tags ORDER BY ts").fetchall()
    browsed = {r["tag"]: r["browsed_ts"] for r in rows}
    stored = [r["tag"] for r in rows if not species or any(s in r["tag"] for s in species)]
    tags = list(dict.fromkeys([f"{s}adopt" for s in species] + stored)) or list(DEFAULT_TAGS)
    return sorted(tags, key=lambda t: (browsed.get(t) is not None, browsed.get(t) or 0))


def _suggest_tags(conn, client, species: list[str], now: float, sleep, budget: int) -> int:
    """Раз в неделю: какие слитные теги «…adopt» существуют для её видов. Возвращает число вызовов."""
    last = db.last_run(conn, TAGS_SUGGEST_RUN)
    if not species or (last and last["last_run_ts"] and now - last["last_run_ts"] < TAGS_SUGGEST_EVERY_S):
        return 0
    calls = 0
    sp = species[int(now // TAGS_SUGGEST_EVERY_S) % len(species)]  # каждую неделю — следующий её вид
    for partial in [f"{sp}adopt", f"adopt{sp}"][:min(budget, MAX_SUGGEST_CALLS)]:
        names = da_browse.tag_suggest(client, partial, sleep=sleep)
        calls += 1
        conn.executemany("INSERT OR IGNORE INTO da_market_tags (tag, ts) VALUES (?, ?)",
                         [(n.lower(), now) for n in names if "adopt" in n.lower()])
    db.record_run(conn, TAGS_SUGGEST_RUN, None, now)
    return calls


def _store_api(conn, cfg: Config, client, results: list[dict], tag: str, now: float, sleep,
               res: SampleResult) -> None:
    fresh = []
    for b in results:
        if b["published_ts"] is None:
            continue  # без даты не понять, свежий ли — порядок выдачи не документирован
        res.found += 1
        res.via_api += 1
        if _too_old(b["published_ts"], now):
            res.too_old += 1
            continue
        fresh.append(b)
    if not fresh:
        return
    known = db.known_ids(conn, [f"da:{b['number']}" for b in fresh])
    new = []
    for b in fresh:
        item_id = f"da:{b['number']}"
        if item_id in known:
            db.merge_extra(conn, item_id, {"uuid": b["uuid"], "via": [f"api_tag:{tag}"]})
        else:
            new.append(b)
    meta = da_browse.metadata(client, [b["uuid"] for b in new], sleep=sleep) if new else {}
    for b in new:
        m = meta.get(b["uuid"]) or {}
        raw = m.get("description") or ""
        text = da._plain(raw)
        api_tags = _str_list(m.get("tags"))
        title = b["title"] or m.get("title") or ""
        author = b["author"] or m.get("author")
        lot = classify_lot(title, text, api_tags)
        if lot.lot_type == "other":
            res.not_lots += 1
            continue
        item = db.Item(
            id=f"da:{b['number']}", source="deviantart", title=title, url=b["url"], author=author,
            place=f"{SEARCH_PLACE_PREFIX}: тег {tag}", published_ts=b["published_ts"], activity_ts=b["published_ts"],
            snippet=text[:da.SNIPPET_LIMIT] or None,
            extra={"tags": [], "api_tags": api_tags, "uuid": b["uuid"], "via": [f"api_tag:{tag}"], "desc": text,
                   "folder_links": da.folder_links(raw, author), "places": []})
        _store(conn, cfg, item, lot, now, res)
    # Снимок комментариев/фавов из выдачи — только для лотов, которые есть в базе.
    stored = db.known_ids(conn, [f"da:{b['number']}" for b in fresh])
    conn.executemany("INSERT OR IGNORE INTO deviation_stats (deviation_id, ts, views, favourites, comments) "
                     "VALUES (?, ?, NULL, ?, ?)",
                     [(f"da:{b['number']}", now, b["favourites"], b["comments"]) for b in fresh
                      if f"da:{b['number']}" in stored])
    conn.commit()


def _layer_b(conn, cfg: Config, client, now: float, explicit: list[str] | None, sleep, res: SampleResult) -> None:
    last = db.last_run(conn, TAGS_BROWSE_RUN)
    if last and last["last_run_ts"] and now - last["last_run_ts"] < TAGS_BROWSE_EVERY_S:
        return
    species, _ = _top(*niche_profile(conn, cfg))
    calls = 0 if explicit else _suggest_tags(conn, client, species, now, sleep, MAX_API_CALLS)
    for tag in _tags_to_browse(conn, species, explicit):
        if calls >= MAX_API_CALLS:
            break
        results, _next = da_browse.browse_tags(client, tag, sleep=sleep)
        calls += 1
        res.tags.append(tag)
        if not explicit:  # теги ручного запроса в обход по её нише не попадают
            conn.execute("INSERT INTO da_market_tags (tag, ts, browsed_ts) VALUES (?, ?, ?) "
                         "ON CONFLICT(tag) DO UPDATE SET browsed_ts = excluded.browsed_ts", (tag, now, now))
        _store_api(conn, cfg, client, results, tag, now, sleep, res)
    db.record_run(conn, TAGS_BROWSE_RUN, None, now)


def _connected(client) -> bool:
    try:
        return client is not None and bool(client.is_connected())
    except Exception:  # noqa: BLE001 — нет токена, битый файл и т.п.
        return False


def sample_market(conn, cfg: Config, http=None, client=None, now: float | None = None, queries=None,
                  pages: int = 1, sleep=time.sleep, pause=None) -> SampleResult:
    """Один проход по рынку ниши: RSS-поиск (без входа) и, если DA подключён, теги через API.

    Сохраняет чужие лоты как данные рынка (matched=0, без уведомлений). На первом 429 проход
    прекращается (rate_limited)."""
    now = now or time.time()
    pause = pause or da.polite_pause
    conn.executescript(TAGS_SCHEMA)
    explicit = [q.strip() for q in queries or [] if q and q.strip()] or None
    res = SampleResult(queries=explicit or niche_queries(conn, cfg))
    if _cooling_down(conn, now):
        res.rate_limited = True
        res.errors.append("DeviantArt недавно просил притормозить — жду 30 минут, ничего не спрашивала")
        return res
    # Поиск рынка — необязательная работа: низкий приоритет в общем бюджете (da_access), ленты важнее.
    with da_access.session(conn), da_access.task("рынок", low=True):
        own_http = http is None
        http = http or da.client()
        try:
            _layer_a(conn, cfg, http, res.queries, now, pages, pause, res)
        finally:
            if own_http:
                http.close()
        # RSS притормозил — пауза общая (da_access), ленты её тоже видят. API — отдельный класс адресов:
        # его слой можно читать и сейчас.
        if not _connected(client):
            return res
        try:
            _layer_b(conn, cfg, client, now, explicit, sleep, res)
        except da_api.RateLimited as e:
            res.rate_limited = res.api_rate_limited = True
            if isinstance(e, da_api.Throttled):
                res.limit_note = str(e)
            else:
                # API просил притормозить — ставки не трогаем столько же (auctions.gate).
                db.record_run(conn, "da_auctions", "429: DeviantArt просит притормозить", now)
        except da_api.NotConnected:
            res.errors.append("DeviantArt отключился — теги через API не читала")
        except da_api.DAApiError as e:
            res.errors.append(f"теги API: {e}")
    return res


def _cooling_down(conn, now: float) -> bool:
    """После 429 от DeviantArt (лента или API) 30 минут ничего не спрашиваем."""
    for source in ("deviantart", RUN_SOURCE, "da_auctions", "enrich"):
        row = db.last_run(conn, source)
        err = (row["last_error"] or "") if row else ""
        if row and ("429" in err or "403" in err) and now - (row["last_run_ts"] or 0) < RATE_LIMIT_COOLDOWN_S:
            return True
    return False


FOLDER_TTL_S = 30 * 86400
# Страница группы была, а UUID папки на ней не нашёлся: смотрим её снова через неделю, а не через месяц.
FOLDER_NO_UUID_TTL_S = 7 * 86400


def resolve_folders(conn, cfg: Config, http, now: float, limit: int = 2, pause=None) -> int:
    """Для отслеживаемых папок узнать UUID папки в API (gallectionUuid со страницы группы): по нему
    ставки читаются пачкой (auctions.folder_pass). Одна страница на группу, кэш 30 дней."""
    pause = pause or da.polite_pause
    groups: list[str] = []
    for g in cfg.deviantart.groups:
        try:
            folder = da.parse_folder_url(g.url)
        except da.DAError:
            continue
        row = conn.execute("SELECT uuid, ts FROM da_folders WHERE grp = ? AND folder_id = ?",
                           (folder.group.lower(), folder.folder_id)).fetchone()
        ttl = FOLDER_TTL_S if row and row["uuid"] else FOLDER_NO_UUID_TTL_S
        if (not row or now - row["ts"] > ttl) and folder.group.lower() not in groups:
            groups.append(folder.group.lower())
    done = 0
    for i, group in enumerate(groups[:limit]):
        if i:
            pause()
        folders = da.list_group_folders(http, group)
        db.save_folders(conn, group, folders, now)
        done += 1
    return done


def maybe_sample(conn, cfg: Config, now: float | None = None, http=None) -> list[str]:
    """Раз в 6 часов после обычной проверки: поиск рынка её ниши. Строки отчёта; никогда не бросает."""
    ts = now or time.time()
    try:
        last = db.last_run(conn, RUN_SOURCE)
        if last and last["last_run_ts"] and ts - last["last_run_ts"] < SAMPLE_EVERY_S:
            return []
        if _cooling_down(conn, ts):
            return []
        da.polite_pause()  # обычная проверка только что читала ленты DeviantArt
        client = None
        try:
            from . import posting

            client = posting.da_client(cfg)
        except Exception:  # noqa: BLE001 — нет client_id: только RSS
            client = None
        own_http = http is None
        http = http or da.client()
        try:
            with da_access.session(conn), da_access.task("рынок", low=True):
                try:
                    if resolve_folders(conn, cfg, http, ts):
                        da.polite_pause()
                except da.Throttled as e:
                    return [f"рынок DA: {e} — поиск отложен"]
                except da.RateLimited:
                    db.record_run(conn, RUN_SOURCE, "429: DeviantArt просит притормозить", ts)
                    return ["рынок DA: DeviantArt просит притормозить — поиск отложен"]
                except (da.DAError, OSError) as e:
                    log.info("group page for folder ids failed: %s", e)
                res = sample_market(conn, cfg, http=http, client=client, now=ts)
        finally:
            if own_http:
                http.close()
        # Свой бюджет кончился — это не просьба DeviantArt: «429» не пишем, иначе ждали бы лишние 30 минут.
        limited = res.rate_limited and not res.limit_note
        db.record_run(conn, RUN_SOURCE, "429: DeviantArt просит притормозить" if limited else None, ts)
        return [f"рынок DA: {line}" for line in render(res).split("\n")[:3]]
    except Exception as e:  # noqa: BLE001 — поиск рынка не должен ронять проверку
        log.warning("market sample failed: %s", e)
        with contextlib.suppress(Exception):
            db.record_run(conn, RUN_SOURCE, f"{type(e).__name__}: {e}"[:200], ts)
        return [f"рынок DA: не получилось ({type(e).__name__})"]


def render(res: SampleResult) -> str:
    lines = [f"нашла {res.found}, новых {res.new}, с ценой {res.priced}, старше 180 дн отброшено {res.too_old}"
             + (f", не лоты (арт, журналы) {res.not_lots}" if res.not_lots else "")]
    if res.queries:
        lines.append("запросы RSS: " + ", ".join(f"«{q}»" for q in res.queries))
    if res.tags:
        lines.append(f"через теги API: {res.via_api} (теги: {', '.join(res.tags)})")
    if res.ignored:
        lines.append("DeviantArt проигнорировал запросы: " + ", ".join(f"«{q}»" for q in res.ignored)
                     + " — пиши обычные слова, без tag:/sort:/max_age:")
    if res.limit_note:
        lines.append(res.limit_note)
    elif res.rate_limited:
        lines.append("DeviantArt попросил притормозить — остальное в следующий раз")
    lines += res.errors[:3]
    lines.append(HONESTY)
    return "\n".join(lines)


# --- кто ставит ---

def buyers(conn, days: int = 60, now: float | None = None, my_username: str = "") -> list[dict]:
    """Кто ставит на аукционах (из комментариев, которые уже прочитаны): по нику и валюте отдельно —
    доллары и поинты не смешиваем. {user, unit, bids, lots, max_bid, species, places, last_ts, on_her_lots}."""
    now = now or time.time()
    me = (my_username or "").lower()
    rows = conn.execute(
        "SELECT a.extra AS a_extra, a.currency, i.id, i.title, i.place, i.own, i.extra FROM auctions a "
        "JOIN items i ON i.id = a.deviation_id WHERE i.source = 'deviantart' "
        "AND COALESCE(i.published_ts, i.first_seen_ts) >= ?", (now - days * 86400,)).fetchall()
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        try:
            records = json.loads(r["a_extra"] or "{}").get("bidders") or []
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        extra = _extra(r)
        species = tag_text(r["title"] or "", _str_list(extra.get("tags")) + _str_list(extra.get("api_tags"))).species
        places = [p for p in _str_list(extra.get("places")) if p] or ([r["place"]] if r["place"] else [])
        for rec in records:
            if not isinstance(rec, dict) or not rec.get("user") or rec.get("amount") is None:
                continue
            user = str(rec["user"])
            if me and user.lower() == me:
                continue
            unit = rec.get("currency") or r["currency"] or "usd"
            g = out.setdefault((user.lower(), unit), {
                "user": user, "unit": unit, "bids": 0, "lots": 0, "max_bid": 0.0, "species": Counter(),
                "places": Counter(), "last_ts": 0.0, "on_her_lots": 0})
            g["bids"] += int(rec.get("n") or 1)
            g["lots"] += 1
            g["max_bid"] = max(g["max_bid"], float(rec["amount"]))
            g["species"].update(species)
            g["places"].update(p for p in places if not db.is_search_place(p))
            g["last_ts"] = max(g["last_ts"], float(rec.get("ts") or 0))
            g["on_her_lots"] += int(bool(r["own"]))
    result = []
    for g in out.values():
        g["species"] = [s for s, _ in g["species"].most_common(3)]
        g["places"] = [p for p, _ in g["places"].most_common(3)]
        result.append(g)
    return sorted(result, key=lambda g: (-g["lots"], -g["max_bid"], g["user"].lower()))


def render_buyers(rows: list[dict], days: int = 60, limit: int = 20) -> str:
    if not rows:
        return (f"За {days} дн прочитанных ставок нет. Ставки читаются из комментариев, когда DeviantArt "
                "подключён; чем больше лотов её ниши в базе, тем полнее список.")
    lots = sum(r["lots"] for r in rows)
    lines = [f"Кто ставит (по прочитанным комментариям, за {days} дн): {len(rows)} человек, "
             f"{lots} лото-ставок. Только для выбора групп и цен — никому не пишем."]
    for r in rows[:limit]:
        bits = [f"ставок {r['bids']} на {r['lots']} лот(ах)", f"максимум {_money(r['max_bid'], r['unit'])}"]
        if r["species"]:
            bits.append("виды: " + ", ".join(r["species"]))
        if r["places"]:
            bits.append("где: " + ", ".join(r["places"]))
        if r["on_her_lots"]:
            bits.append(f"на её лотах: {r['on_her_lots']}")
        lines.append(f"• {r['user']} — " + "; ".join(bits))
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    return "\n".join(lines)


def _money(amount: float | None, unit: str | None) -> str:
    from .bids import money

    return money(amount, unit)


# --- карточка групп ---

LOT_FOLDER = re.compile(r"auction|adopt|ota|offer|sale|points?|money|bta|raffle|design|character", re.I)
NOT_LOT_FOLDER = re.compile(r"journal|base|ych|commission|f2u|ftu|ptu|info|featured|tutorial|stamp|resource", re.I)
SCORECARD_EVERY_S = 7 * 86400
MAX_GROUPS = 10
MIN_BID_SHARE_N = 10


def _lot_folders(folders: list[da.Folder], tracked: set[str], limit: int = 3) -> list[da.Folder]:
    cands = [f for f in folders if f.folder_id in tracked
             or (LOT_FOLDER.search(f.name or "") and not NOT_LOT_FOLDER.search(f.name or ""))]
    cands.sort(key=lambda f: (f.folder_id not in tracked, -(f.size or 0)))
    return cands[:limit]


def lots_per_day(stamps: list[float], now: float) -> float | None:
    """Лотов в день за окно «от старого лота страницы до сейчас»: мёртвая папка, где три лота
    выложены за час полгода назад, даёт почти ноль, а не 24 в день. Самые старые 10% страницы
    не считаем: старую работу, недавно добавленную в группу, лента ставит рядом с новыми."""
    stamps = sorted(s for s in stamps if s is not None)
    if len(stamps) < 2:
        return None
    k = len(stamps) // 10
    return (len(stamps) - k) / max((now - stamps[k]) / 86400, 1 / 24)


def _niche_hit(entry: dict, niche: set[str]) -> bool:
    t = tag_text(entry["title"], entry["tags"], entry["text"][:300])
    return bool(niche & (set(t.species) | set(t.styles)))


def _bid_share(conn, ids: set[str]) -> tuple[float | None, int]:
    """Доля аукционов с ≥1 ставкой среди тех, чей итог известен — по тому же правилу, что и прогноз
    (forecast.outcome_known): лот, который в 7–13 дней ещё виден открытым, итогом не считаем."""
    from .forecast import _row_known

    if not ids:
        return None, 0
    marks = ",".join("?" * len(ids))
    rows = [r for r in conn.execute(
        "SELECT a.n_bids, a.ended AS a_ended, a.extra AS a_extra, a.updated_ts AS a_updated, i.status, i.closed_ts, "
        "i.published_ts, i.first_seen_ts, i.updated_ts FROM auctions a LEFT JOIN items i ON i.id = a.deviation_id "
        f"WHERE a.deviation_id IN ({marks})", list(ids)).fetchall() if _row_known(r)]
    n = len(rows)
    if n < MIN_BID_SHARE_N:
        return None, n
    return sum(1 for r in rows if (r["n_bids"] or 0) > 0) / n, n


def _tracked_ids(conn, cfg: Config, folder: da.Folder) -> set[str]:
    """Лоты из этой папки, которые уже есть в базе (по метке её ленты)."""
    labels = []
    for g in cfg.deviantart.groups:
        try:
            f = da.parse_folder_url(g.url)
        except da.DAError:
            continue
        if f.group.lower() == folder.group.lower() and f.folder_id == folder.folder_id:
            labels.append(g.label or f"{f.group}: {f.name or f.folder_id}")
    out: set[str] = set()
    for label in labels:
        out |= {r["id"] for r in conn.execute(
            "SELECT id FROM items WHERE source = 'deviantart' AND (place = ? OR EXISTS "
            "(SELECT 1 FROM json_each(COALESCE(json_extract(items.extra, '$.places'), '[]')) WHERE value = ?))",
            (label, label))}
    return out


def group_scorecard(conn, http, cfg: Config, group: str, now: float | None = None, pause=None,
                    niche: set[str] | None = None) -> list[dict]:
    """Карточка одной группы по рынку: до 4 запросов (страница группы + по странице RSS для ≤3 папок лотов)."""
    now = now or time.time()
    pause = pause or da.polite_pause
    niche = niche_terms(conn, cfg) if niche is None else niche
    folders = da.list_group_folders(http, group)
    db.save_folders(conn, group, folders, now)
    tracked = set()
    for g in cfg.deviantart.groups:
        try:
            f = da.parse_folder_url(g.url)
        except da.DAError:
            continue
        if f.group.lower() == group.lower():
            tracked.add(f.folder_id)
    rows = []
    for folder in _lot_folders(folders, tracked):
        pause()
        try:
            entries = da.fetch_feed(http, folder.query, f"{folder.group}: {folder.name or folder.folder_id}", pages=1)
        except da.IgnoredQuery:
            continue
        lots = [(e, classify_lot(e["title"], e["text"], e["tags"])) for e in entries]
        lots = [(e, lot) for e, lot in lots if lot.lot_type != "other"]
        sb: dict[str, list[float]] = {"usd": [], "points": []}
        for _, lot in lots:
            if lot.lot_type in ("auction", "bta"):
                if lot.usd is not None:
                    sb["usd"].append(lot.usd)
                elif lot.points is not None:
                    sb["points"].append(lot.points)
        unit = max(sb, key=lambda u: len(sb[u])) if any(sb.values()) else None
        ids = {f"da:{e['deviation_id']}" for e, _ in lots} | _tracked_ids(conn, cfg, folder)
        share, known = _bid_share(conn, ids)
        row: dict[str, Any] = {
            "grp": folder.group, "folder": folder.folder_id, "name": folder.name, "size": folder.size,
            "lots_per_day": lots_per_day([e["published_ts"] for e, _ in lots], now),
            "niche_share": (sum(_niche_hit(e, niche) for e, _ in lots) / len(lots)) if niche and lots else None,
            "median_sb": statistics.median(sb[unit]) if unit else None, "unit": unit,
            "sb_n": len(sb[unit]) if unit else 0,
            "bid_share": share, "known": known, "n": len(lots), "ts": now,
        }
        conn.execute(
            "INSERT INTO group_scores (grp, folder, size, lots_per_day, niche_share, median_sb, unit, bid_share, n, ts, "
            "known, sb_n) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(grp, folder) DO UPDATE SET "
            "size = excluded.size, lots_per_day = excluded.lots_per_day, niche_share = excluded.niche_share, "
            "median_sb = excluded.median_sb, unit = excluded.unit, bid_share = excluded.bid_share, n = excluded.n, "
            "ts = excluded.ts, known = excluded.known, sb_n = excluded.sb_n",
            (row["grp"].lower(), row["folder"], row["size"], row["lots_per_day"], row["niche_share"], row["median_sb"],
             row["unit"], row["bid_share"], row["n"], now, row["known"], row["sb_n"]))
        rows.append(row)
    conn.commit()
    return rows


def scorecard_candidates(conn, cfg: Config, now: float, limit: int = MAX_GROUPS) -> list[str]:
    """Её отслеживаемые группы и наборы, куда она отправляла, плюс группы из ссылок в описаниях лотов её ниши."""
    out: list[str] = []

    def add(name: str | None) -> None:
        name = (name or "").strip().lower()
        if name and name not in out:
            out.append(name)

    for g in cfg.deviantart.groups:
        try:
            add(da.parse_folder_url(g.url).group)
        except da.DAError:
            continue
    for preset in (cfg.posting.group_presets or {}).values():
        for entry in preset or []:
            if isinstance(entry, dict):
                add(entry.get("group"))
    for r in conn.execute("SELECT DISTINCT target FROM sent_log WHERE kind = 'da_group'"):
        add(r["target"])
    niche = niche_terms(conn, cfg)
    if niche:
        linked: Counter = Counter()
        for r in conn.execute("SELECT title, extra FROM items WHERE source = 'deviantart' "
                              "AND COALESCE(published_ts, first_seen_ts) >= ? AND extra LIKE '%folder_links%'",
                              (now - 60 * 86400,)):
            extra = _extra(r)
            t = tag_text(r["title"] or "", _str_list(extra.get("tags")) + _str_list(extra.get("api_tags")),
                         str(extra.get("desc") or "")[:300])
            if niche & (set(t.species) | set(t.styles)):
                linked.update({link.split("/")[0] for link in _str_list(extra.get("folder_links"))})
        for g, _ in linked.most_common(5):
            add(g)
    return out[:limit]


def _cached(conn, group: str, now: float) -> list[dict] | None:
    rows = conn.execute(
        "SELECT s.*, f.name FROM group_scores s LEFT JOIN da_folders f ON f.grp = s.grp AND f.folder_id = s.folder "
        "WHERE s.grp = ? AND s.ts >= ?", (group.lower(), now - SCORECARD_EVERY_S)).fetchall()
    if not rows:
        return None
    out = []
    for r in rows:
        out.append({"grp": r["grp"], "folder": r["folder"], "name": r["name"], "size": r["size"],
                    "lots_per_day": r["lots_per_day"], "niche_share": r["niche_share"], "median_sb": r["median_sb"],
                    "unit": r["unit"], "bid_share": r["bid_share"], "known": r["known"], "sb_n": r["sb_n"],
                    "n": r["n"], "ts": r["ts"], "cached": True})
    return out


def group_scorecard_many(conn, http, cfg: Config, groups=None, now: float | None = None, pause=None) -> list[dict]:
    """Карточки до 10 групп; каждую обновляем не чаще раза в неделю (иначе — из базы).
    На 429 останавливаемся; ошибка группы — строка с error."""
    now = now or time.time()
    pause = pause or da.polite_pause
    names = [g.strip().rstrip("/").split("deviantart.com/")[-1].split("/")[0].lower() for g in groups or [] if g.strip()]
    names = list(dict.fromkeys(names))[:MAX_GROUPS] if names else scorecard_candidates(conn, cfg, now)
    niche = niche_terms(conn, cfg)
    # До 4 RSS-запросов на группу: низкий приоритет в общем бюджете, чтобы ленты не остались без чтения.
    with da_access.session(conn), da_access.task("карточки групп", low=True):
        return _scorecards(conn, http, cfg, names, niche, now, pause)


def _scorecards(conn, http, cfg: Config, names: list[str], niche: set[str], now: float, pause) -> list[dict]:
    rows: list[dict] = []
    fetched = 0
    for group in names:
        cached = _cached(conn, group, now)
        if cached is not None:
            rows += cached
            continue
        if fetched:
            pause()
        fetched += 1
        try:
            got = group_scorecard(conn, http, cfg, group, now, pause=pause, niche=niche)
        except da.Throttled as e:
            rows.append({"grp": group, "error": f"{e} — остальные группы позже"})
            break
        except da.RateLimited:
            rows.append({"grp": group, "error": "DeviantArt просит притормозить — остальные группы позже"})
            break
        except (da.DAError, OSError) as e:
            rows.append({"grp": group, "error": str(e)})
            continue
        rows += got or [{"grp": group, "error": "не нашла папок с лотами"}]
    return rows


def _score(r: dict) -> float:
    return (r.get("niche_share") or 0) * (r.get("lots_per_day") or 0)


def render_scorecard(rows: list[dict]) -> str:
    if not rows:
        return ("Групп для оценки нет: добавь папки групп (da_groups:track) или назови группы. "
                "По рынку, не по твоим продажам.")
    good = sorted((r for r in rows if "error" not in r), key=_score, reverse=True)
    lines = ["Группы DeviantArt по рынку, не по твоим продажам (одна страница ленты на папку; "
             "сортировка: доля твоей ниши × лотов в день):"]
    if good and all(r.get("niche_share") is None for r in good):
        lines.append("ниша не определилась (нет персонажей/её лотов/ключевых слов) — доля ниши не считается")
    for r in good:
        bits = [f"{r['size']} работ" if r.get("size") is not None else "размер неизвестен"]
        bits.append(f"~{r['lots_per_day']:.1f} лотов/день" if r.get("lots_per_day") else "темп не посчитать")
        bits.append(f"n={r.get('n') or 0} на странице")
        if r.get("niche_share") is not None:
            bits.append(f"твоя ниша {r['niche_share']:.0%}")
        if r.get("median_sb") is not None:
            bits.append(f"медианный старт {_money(r['median_sb'], r.get('unit'))}"
                        + (f" (n={r['sb_n']})" if r.get("sb_n") else ""))
        if r.get("bid_share") is not None:
            bits.append(f"со ставками {r['bid_share']:.0%}" + (f" из {r['known']} с известным итогом"
                                                                if r.get("known") else ""))
        elif r.get("known") is not None:
            bits.append(f"доля со ставками: мало данных ({r['known']} с известным итогом, нужно ≥{MIN_BID_SHARE_N})")
        cached = " (из базы, обновлю через неделю)" if r.get("cached") else ""
        lines.append(f"• {r['grp']} / {r.get('name') or r['folder']}: " + ", ".join(bits) + cached)
    for r in rows:
        if "error" in r:
            lines.append(f"• {r['grp']}: {r['error']}")
    return "\n".join(lines)
