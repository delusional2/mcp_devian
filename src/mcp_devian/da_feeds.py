"""Ленты DeviantArt: какие читать в этот проход и как — через API (если она подключила DA) или RSS.

* По кругу: плановая проверка читает FEEDS_PER_RUN лент, которые дольше всех не читались. Её галерея
  и папки, где лежат её открытые аукционы, — вне очереди, но одно место всегда остаётся очереди,
  чтобы остальные ленты не простаивали. Ручная проверка читает больше (MANUAL_FEEDS_PER_RUN),
  но тоже в пределах общего бюджета (da_access).
* Через API: GET /gallery/{folderid}?username=группа (UUID папки — из da_folders или /gallery/folders),
  её галерея и художники — /gallery/all. Работы приводятся к тем же словарям, что даёт RSS
  (deviantart.parse_rss_page), поэтому разбор лотов и сохранение не меняются. UUID работы
  сохраняется сразу. Описание (там цена, SB/AB) API в выдаче не даёт: одним запросом
  /deviation/metadata дочитываем описания новых работ и тех, что читали больше 6 часов назад.
* Для RSS — условный GET: ETag/Last-Modified первой страницы храним в da_feeds; 304 — лента
  не менялась, ничего не разбираем.
* Листание: обычно только первая страница (следующая — лишь если на ней всё новое). Раз в DEEP_EVERY_S
  лента читается глубже, до FEED_PAGES страниц за 3 дня, — чтобы видеть лоты, закрывшиеся ниже первой
  страницы (closed_ts, прогноз). Глубокое чтение идёт без условного GET и только если в бюджете часа
  на него хватает запросов.
"""

import json
import logging
import re
import time

from . import da_api, da_browse, db
from . import deviantart as da

log = logging.getLogger(__name__)

# 3 ленты × 4 запуска в час = 12 первых страниц в час: укладывается в 20 RSS-запросов da_access
# с запасом на листание и поиск рынка. С 8 лентами каждая читается примерно раз в 40 минут.
FEEDS_PER_RUN = 3
MANUAL_FEEDS_PER_RUN = 12
API_PAGE = 24
API_PAUSE_S = 1.5
DESC_REFRESH_S = 6 * 3600
# Ленту, для которой API не годится (UUID папки не нашёлся в /gallery/folders или выдача API не в порядке
# «сначала новые»), неделю читаем через RSS и API для неё не спрашиваем. Промах хранится в da_feeds,
# а не в da_folders: строка da_folders без UUID мешала бы da_market.resolve_folders найти UUID на странице группы.
FOLDER_MISS_TTL_S = 7 * 86400
# Как часто ленту листать глубже первой страницы (лоты, закрывшиеся за 3 дня).
DEEP_EVERY_S = 6 * 3600
# Глубокое чтение — только если после него в часе останется на первые страницы следующей проверки
# (3 ленты для RSS; для API с запасом на ставки) — иначе глубокое съело бы час, и плановые проверки
# пропускались бы. Для RSS запас маленький нарочно: 12 первых страниц в час из 20 — и глубокое
# чтение всё равно должно иногда влезать.
DEEP_KEEP = {"rss": 3, "api": 30}
# Выдача API для папки не по порядку «сначала новые», если самая новая работа на полной первой странице
# старше самой новой уже известной из этой ленты больше чем на столько.
ORDER_SLACK_S = 2 * 86400
# Папка, только что прочитанная через API, уже дала UUID и снимок комментариев: ставкам её не перечитывать.
API_FRESH_S = 20 * 60
OPEN_AUCTION_DAYS = 21


class NoFolderUuid(Exception):
    """UUID папки для API неизвестен — эту ленту читаем через RSS."""


class ApiUnfit(NoFolderUuid):
    """Выдача API для ленты не похожа на «сначала новые» — эту ленту читаем через RSS."""


# --- состояние лент ---

def last_reads(conn) -> dict[str, float]:
    return {r["key"]: r["last_read_ts"] or 0 for r in conn.execute("SELECT key, last_read_ts FROM da_feeds")}


def validators(conn, key: str) -> dict:
    row = conn.execute("SELECT etag, last_modified FROM da_feeds WHERE key = ?", (key,)).fetchone()
    return {"etag": row["etag"], "last_modified": row["last_modified"]} if row else {}


def mark_read(conn, key: str, now: float, via: str, valid: dict | None = None, uuid: str | None = None) -> None:
    valid = valid or {}
    conn.execute(
        "INSERT INTO da_feeds (key, etag, last_modified, last_read_ts, via, uuid) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET last_read_ts = excluded.last_read_ts, via = excluded.via, "
        "etag = CASE WHEN excluded.via = 'rss' THEN excluded.etag ELSE da_feeds.etag END, "
        "last_modified = CASE WHEN excluded.via = 'rss' THEN excluded.last_modified ELSE da_feeds.last_modified END, "
        "uuid = COALESCE(excluded.uuid, da_feeds.uuid)",
        (key, valid.get("etag"), valid.get("last_modified"), now, via, uuid))
    conn.commit()


MISS_COLUMNS = {"uuid": "api_miss_ts", "order": "order_miss_ts"}


def deep_due(conn, key: str, now: float) -> bool:
    row = conn.execute("SELECT deep_ts FROM da_feeds WHERE key = ?", (key,)).fetchone()
    return now - ((row and row["deep_ts"]) or 0) >= DEEP_EVERY_S


def mark_deep(conn, key: str, now: float) -> None:
    conn.execute("UPDATE da_feeds SET deep_ts = ? WHERE key = ?", (now, key))
    conn.commit()


def note_api_miss(conn, key: str, now: float, why: str = "uuid") -> None:
    """API для этой ленты не годится (why: uuid — нет UUID папки, order — не тот порядок): неделю через RSS."""
    col = MISS_COLUMNS[why]
    conn.execute(f"INSERT INTO da_feeds (key, {col}) VALUES (?, ?) "
                 f"ON CONFLICT(key) DO UPDATE SET {col} = excluded.{col}", (key, now))
    conn.commit()


def api_missed(conn, key: str, now: float, why: str = "uuid") -> bool:
    row = conn.execute(f"SELECT {MISS_COLUMNS[why]} AS ts FROM da_feeds WHERE key = ?", (key,)).fetchone()
    return bool(row and row["ts"] and now - row["ts"] < FOLDER_MISS_TTL_S)


def fresh_api_folders(conn, now: float) -> set[str]:
    """UUID папок, прочитанных через API за последние API_FRESH_S."""
    return {r["uuid"].upper() for r in conn.execute(
        "SELECT uuid FROM da_feeds WHERE via = 'api' AND uuid IS NOT NULL AND last_read_ts >= ?",
        (now - API_FRESH_S,))}


# --- очередь ---

def priority_keys(conn, feeds: list[da.Feed], now: float) -> set[str]:
    """Её галерея и ленты, где лежат её открытые аукционы (за 3 недели)."""
    keys = {f.query for f in feeds if f.own}
    places: set[str] = set()
    rows = conn.execute(
        "SELECT place, extra FROM items WHERE source = 'deviantart' AND own = 1 AND lot_type IN ('auction', 'bta') "
        "AND COALESCE(status, '') != 'closed' AND COALESCE(published_ts, first_seen_ts) >= ?",
        (now - OPEN_AUCTION_DAYS * 86400,)).fetchall()
    for r in rows:
        if r["place"]:
            places.add(r["place"])
        try:
            extra = json.loads(r["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        if isinstance(extra, dict) and isinstance(extra.get("places"), list):
            places |= {str(p) for p in extra["places"]}
    return keys | {f.query for f in feeds if f.place in places}


def pick(feeds: list[da.Feed], last: dict[str, float], priority: set[str], k: int | None) -> list[da.Feed]:
    """Какие ленты читать: k=None — все в порядке настроек. Иначе важные (priority) и те, что дольше
    всех не читались (никогда — первыми). Важным — не больше k−1 мест, если есть другие ленты:
    одно место всегда за очередью, иначе при нескольких аукционах остальные папки не читались бы."""
    if k is None:
        return list(feeds)
    order = sorted(feeds, key=lambda f: last.get(f.query) or 0)
    hot = [f for f in order if f.query in priority]
    cold = [f for f in order if f.query not in priority]
    chosen = hot[:k - 1] if cold and k > 1 else hot[:k]
    chosen += cold[:k - len(chosen)]
    chosen += [f for f in hot if f not in chosen][:k - len(chosen)]
    return chosen


# --- API ---

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def folder_uuid(conn, client, feed: da.Feed, now: float) -> str:
    """UUID папки группы для /gallery/{folderid}: из da_folders, иначе /gallery/folders группы по названию
    из ссылки (одно точное совпадение). Не нашли — NoFolderUuid (промах помним неделю в da_feeds)."""
    group, folder_id = feed.user, feed.folder_id
    row = conn.execute("SELECT uuid FROM da_folders WHERE grp = ? AND folder_id = ?",
                       (group.lower(), folder_id)).fetchone()
    if row and row["uuid"]:
        return row["uuid"]
    if api_missed(conn, feed.query, now):
        raise NoFolderUuid(feed.query)
    want = _norm(feed.name)
    uuid = None
    if want:
        try:
            folders = client.gallery_folders(group)
        except (da_api.RateLimited, da_api.NotConnected):
            raise
        except da_api.DAApiError:
            # Группу переименовали или папки скрыты: не спрашиваем каждый проход.
            note_api_miss(conn, feed.query, now)
            raise
        matches = [f for f in folders if _norm(f["name"]) == want and f.get("folder_id")]
        uuid = str(matches[0]["folder_id"]) if len(matches) == 1 else None
    if not uuid:
        note_api_miss(conn, feed.query, now)
        raise NoFolderUuid(feed.query)
    db.save_folders(conn, group, [da.Folder(group=group, folder_id=str(folder_id), name=feed.name, uuid=uuid)], now)
    return uuid


def api_entry(dev: dict, place: str) -> dict | None:
    """Работа из выдачи /gallery → словарь как у RSS (+ uuid, comments, favourites)."""
    brief = da_browse.deviation_brief(dev)
    if not brief:
        return None
    tags = [str(t["tag_name"]) for t in dev.get("tags") or [] if isinstance(t, dict) and t.get("tag_name")]
    thumbs = [t for t in dev.get("thumbs") or [] if isinstance(t, dict) and t.get("src")]
    preview: dict = dev["preview"] if isinstance(dev.get("preview"), dict) else {}
    return {
        "deviation_id": brief["number"],
        "title": brief["title"],
        "url": brief["url"],
        "author": brief["author"],
        "tags": tags,
        "text": da._plain(str(dev.get("excerpt") or "")),
        "folder_links": [],
        "published_ts": brief["published_ts"],
        "rating": "adult" if dev.get("is_mature") else "nonadult",
        "thumb": thumbs[0]["src"] if thumbs else preview.get("src"),
        "place": place,
        "uuid": brief["uuid"],
        "comments": brief["comments"],
        "favourites": brief["favourites"],
    }


def _old_extra(row) -> dict:
    try:
        extra = json.loads(row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return extra if isinstance(extra, dict) else {}


def fill_text(conn, client, entries: list[dict], now: float, sleep=time.sleep) -> None:
    """Описание и теги: свежие (моложе 6 ч) — из базы, остальные — одним запросом метаданных.
    Если метаданные не пришли, оставляем то, что уже знали: повторное чтение не стирает описание."""
    need = []
    for e in entries:
        row = db.get_item(conn, f"da:{e['deviation_id']}")
        extra = _old_extra(row) if row else {}
        if row:
            e["text"] = str(extra.get("desc") or row["snippet"] or e["text"])
            e["tags"] = e["tags"] or [str(t) for t in extra.get("tags") or []]
            e["folder_links"] = [str(f) for f in extra.get("folder_links") or []]
            if extra.get("desc_ts"):
                e["desc_ts"] = extra["desc_ts"]
        if not (row and extra.get("desc_ts") and now - float(extra["desc_ts"]) < DESC_REFRESH_S):
            need.append(e)
    if not need:
        return
    try:
        # Нужны только описание и теги: без ext_stats запрос легче.
        meta = da_browse.metadata(client, [e["uuid"] for e in need], sleep=sleep, ext_stats=False)
    except (da_api.RateLimited, da_api.NotConnected):
        raise
    except da_api.DAApiError as err:
        log.info("deviation metadata for feed failed: %s", err)
        return
    for e in need:
        m = meta.get(e["uuid"])
        if not m:
            continue
        raw = m.get("description") or ""
        e["text"] = da._plain(raw)
        e["folder_links"] = da.folder_links(raw, e["author"])
        e["tags"] = [str(t) for t in m.get("tags") or []] or e["tags"]
        e["desc_ts"] = now


def newest_known(conn, place: str) -> float | None:
    """Самая новая уже известная работа этой ленты (по месту)."""
    row = conn.execute(
        "SELECT MAX(published_ts) FROM items WHERE source = 'deviantart' AND (place = ? OR EXISTS "
        "(SELECT 1 FROM json_each(COALESCE(json_extract(items.extra, '$.places'), '[]')) WHERE value = ?))",
        (place, place)).fetchone()
    return row[0] if row else None


def read_api(conn, client, feed: da.Feed, uuid: str | None, now: float, pages: int = 1, known=None,
             horizon_s: float = 3 * 86400, sleep=time.sleep, deep: bool = True,
             state: dict | None = None) -> list[dict]:
    """Лента через API. Листаем, как RSS (deviantart.fetch_feed): deep=False — дальше первой страницы,
    только если на ней всё новое; deep=True — ещё и пока знакомое свежее horizon_s.

    state — если передан, отказ на 2-й странице или на метаданных не выбрасывает прочитанное:
    возвращаем его (описания — какие уже были в базе) и ставим state["cut"] = исключение.
    Полная первая страница, где всё старше уже известного из этой ленты, — ApiUnfit (порядок не тот)."""
    path = f"/gallery/{uuid}" if uuid else "/gallery/all"
    params = {"username": feed.user, "mature_content": "true", "limit": API_PAGE}
    is_known = known or (lambda dev_id: False)
    entries: list[dict] = []
    offset = 0
    for page in range(max(1, pages)):
        if page:
            sleep(API_PAUSE_S)
            try:
                body = client._request("GET", path, params={**params, "offset": offset})
            except da_api.NotConnected:
                raise
            except da_api.DAApiError as e:
                if state is None:
                    raise
                state["cut"] = e
                break
        else:
            body = client._request("GET", path, params={**params, "offset": offset})
        found = [e for e in (api_entry(d, feed.place) for d in body.get("results") or [] if isinstance(d, dict)) if e]
        if not page and body.get("has_more"):
            _check_order(conn, feed, found, now)
        entries.extend(found)
        nxt = body.get("next_offset")
        if not found or not body.get("has_more") or nxt in (None, offset):
            break
        if not deep:
            if any(is_known(e["deviation_id"]) for e in found):
                break
        elif all(is_known(e["deviation_id"]) for e in found):
            stamps = [e["published_ts"] for e in found if e["published_ts"]]
            if not stamps or min(stamps) <= now - horizon_s:
                break
        offset = int(nxt)
    if entries:
        sleep(API_PAUSE_S)
        try:
            fill_text(conn, client, entries, now, sleep=sleep)
        except da_api.NotConnected:
            raise
        except da_api.RateLimited as e:
            if state is None:
                raise
            state["cut"] = e  # описания — из базы; новые дочитаем в следующий раз (desc_ts не стоит)
    return entries


def _check_order(conn, feed: da.Feed, found: list[dict], now: float) -> None:
    """Порядок /gallery/{folderid} для папки группы не документирован. Если на полной первой странице
    самая новая работа заметно старше уже известной из этой ленты, выдача не «сначала новые»:
    через API эта лента новых лотов не покажет — неделю читаем её через RSS."""
    stamps = [e["published_ts"] for e in found if e.get("published_ts")]
    known = newest_known(conn, feed.place)
    if stamps and known and max(stamps) < known - ORDER_SLACK_S:
        log.warning("API order for %s is not newest-first, reading it via RSS", feed.query)
        note_api_miss(conn, feed.query, now, "order")
        raise ApiUnfit(feed.query)


def save_stats(conn, entries: list[dict], now: float) -> None:
    """Число комментариев и фавов из выдачи API — снимок, как у auctions.folder_pass (без просмотров)."""
    rows = [(f"da:{e['deviation_id']}", now, e.get("favourites"), e.get("comments")) for e in entries
            if e.get("comments") is not None or e.get("favourites") is not None]
    if rows:
        conn.executemany("INSERT OR IGNORE INTO deviation_stats (deviation_id, ts, views, favourites, comments) "
                         "SELECT ?, ?, NULL, ?, ? WHERE EXISTS (SELECT 1 FROM items WHERE id = ?)",
                         [(*r, r[0]) for r in rows])
        conn.commit()
