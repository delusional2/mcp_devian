"""Сигналы спроса из API DeviantArt: просмотры, фавы и комментарии лотов.

Цена показывает, за сколько купили; фавы и просмотры — что людям нравится, даже если оно
ещё не продалось. Снимки копятся в deviation_stats, по ним советник считает спрос по видам.
Работает только когда она подключила DeviantArt; бережёт запросы (бюджет, кэш промахов).
"""

import logging
import sqlite3
import time

from . import auctions, da_api, da_browse, db

# Чтобы каждую работу не пересчитывать каждый проход: не чаще раза в сутки.
SNAPSHOT_EVERY_S = 24 * 3600
LOOKBACK_DAYS = 30
MARKET_TYPES = ("auction", "bta", "flatsale", "ota", "adopt", "raffle")
# Если пачка метаданных падает не из-за размера, делим её пополам, чтобы найти и пропустить
# сломанную работу. Но не больше стольких запросов за проход: вдруг лежит весь API.
METADATA_CALLS_MAX = 12
# Размер чужих продавцов (watchers): не чаще раза в 30 дней на автора, не больше 20 новых за проход.
AUTHOR_TTL_DAYS = 30
AUTHOR_LOOKBACK_DAYS = 60
AUTHOR_BUDGET = 20

log = logging.getLogger(__name__)

MISS_SCHEMA = """
CREATE TABLE IF NOT EXISTS uuid_misses (
    url TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
"""


def _load_misses(conn) -> dict[str, float]:
    conn.executescript(MISS_SCHEMA)
    return {r["url"]: r["ts"] for r in conn.execute("SELECT url, ts FROM uuid_misses")}


def _save_misses(conn, misses: dict[str, float], before: dict[str, float]) -> None:
    gone = [u for u in before if u not in misses]
    conn.executemany("DELETE FROM uuid_misses WHERE url = ?", [(u,) for u in gone])
    conn.executemany("INSERT OR REPLACE INTO uuid_misses (url, ts) VALUES (?, ?)", list(misses.items()))
    conn.commit()


def candidates(conn, now: float | None = None, limit: int = 50) -> list:
    """Её лоты первыми, потом свежие рыночные лоты без снимка за последние сутки.

    Лоты, чей UUID недавно не нашёлся, пропускаем до конца срока промаха: иначе одни и те же
    ненаходимые лоты месяц занимали бы места в LIMIT, а новые до статистики не доходили бы."""
    now = now or time.time()
    conn.executescript(MISS_SCHEMA)
    since, fresh = now - LOOKBACK_DAYS * 86400, now - SNAPSHOT_EVERY_S
    types = ",".join(f"'{t}'" for t in MARKET_TYPES)
    sql = (
        "SELECT i.* FROM items i WHERE i.source = 'deviantart' AND COALESCE(i.published_ts, i.first_seen_ts) >= ? "
        "AND NOT EXISTS (SELECT 1 FROM deviation_stats s WHERE s.deviation_id = i.id AND s.ts >= ? "
        "AND s.views IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM uuid_misses m WHERE m.url = TRIM(i.url) AND m.ts > ?) AND {cond} "
        "ORDER BY COALESCE(i.published_ts, i.first_seen_ts) DESC LIMIT ?"
    )
    retry = now - da_api.UUID_MISS_TTL_S
    mine = conn.execute(sql.format(cond="i.own = 1 AND COALESCE(i.lot_type, '') != 'other'"),
                        (since, fresh, retry, limit)).fetchall()
    market = conn.execute(sql.format(cond=f"i.own = 0 AND i.lot_type IN ({types})"),
                          (since, fresh, retry, max(0, limit - len(mine)))).fetchall()
    return list(mine) + list(market)


def _recent_miss(misses: dict[str, float], url: str, now: float) -> bool:
    return now - float(misses.get((url or "").strip(), 0)) < da_api.UUID_MISS_TTL_S


def _metadata(client, uuids: list[str], sleep) -> dict[str, dict]:
    """Метаданные пачкой; при ошибке API делим пачку и пропускаем только сломанную часть."""
    out: dict[str, dict] = {}
    parts, calls, skipped = [uuids], 0, 0
    while parts:
        part = parts.pop()
        if calls >= METADATA_CALLS_MAX:
            skipped += len(part)
            continue
        if calls:
            sleep(1.0)
        calls += 1
        try:
            out.update(client.deviation_metadata(part, ext_stats=True))
        except (da_api.RateLimited, da_api.NotConnected):
            raise
        except da_api.DAApiError as e:
            if len(part) == 1:
                skipped += 1
                log.warning("deviation metadata failed for %s: %s", part[0], e)
                continue
            mid = len(part) // 2
            parts += [part[mid:], part[:mid]]
    if skipped:
        log.warning("deviation metadata: skipped %d of %d works after API errors", skipped, len(uuids))
    return out


def enrich_stats(conn, client, now: float | None = None, budget: int = 50, lookup_budget: int = 10,
                 sleep=time.sleep) -> dict:
    """Снять статистику для до `budget` лотов. Не больше `lookup_budget` поисков UUID (это дорого:
    до 2 страниц галереи автора на лот). На 429 и при отключённом DA — останавливаемся и говорим почему."""
    now = now or time.time()
    summary: dict = {"lots": 0, "snapshots": 0, "lookups": 0, "misses": 0, "stopped": None}
    misses = _load_misses(conn)
    before = dict(misses)
    pairs: list[tuple[sqlite3.Row, str]] = []
    try:
        for item in candidates(conn, now, budget):
            uuid = auctions.known_uuid(item)
            if not uuid:
                # Недавний промах — даже не запрос: бюджет поисков на него не тратим.
                if _recent_miss(misses, item["url"], now):
                    continue
                if summary["lookups"] >= lookup_budget:
                    continue
                summary["lookups"] += 1
                try:
                    uuid = client.deviation_uuid_from_url(item["url"], max_pages=2, miss_cache=misses)
                except (da_api.RateLimited, da_api.NotConnected):
                    raise
                except da_api.DAApiError:
                    # Например, ссылка не на работу: каждый проход повторять бесполезно — как промах.
                    misses[(item["url"] or "").strip()] = now
                    uuid = None
                sleep(1.0)  # промах листал галерею так же, как находка: пауза после любого поиска
                if not uuid:
                    summary["misses"] += 1
                    continue
                auctions.remember_uuid(conn, item, uuid)
            pairs.append((item, uuid))
        if pairs:
            meta = _metadata(client, [u for _, u in pairs], sleep)
            rows = []
            for item, uuid in pairs:
                m = meta.get(uuid)
                if not m:
                    continue
                rows.append((item["id"], now, m.get("views"), m.get("favourites"), m.get("comments")))
                # Теги автора: RSS их больше не отдаёт, а по ним видно вид/стиль лота. Повторное
                # чтение ленты их не стирает (db.KEEP_EXTRA).
                if m.get("tags"):
                    db.merge_extra(conn, item["id"], {"api_tags": [str(t) for t in m["tags"]]})
            conn.executemany("INSERT OR REPLACE INTO deviation_stats (deviation_id, ts, views, favourites, comments) "
                             "VALUES (?, ?, ?, ?, ?)", rows)
            conn.commit()
            summary["snapshots"] = len(rows)
        summary["lots"] = len(pairs)
        summary["authors"] = author_pass(conn, client, now, sleep=sleep)
    except da_api.Throttled as e:
        # Запрос не отправляли: общая пауза или кончился бюджет часа (da_access). Это не новый 429.
        summary["stopped"] = str(e)
    except da_api.RateLimited:
        summary["stopped"] = "DeviantArt просит притормозить (429) — продолжу в следующий раз"
    except da_api.NotConnected:
        summary["stopped"] = "DeviantArt не подключён"
    finally:
        _save_misses(conn, misses, before)
    return summary


def authors_to_check(conn, now: float, limit: int = AUTHOR_BUDGET) -> list[str]:
    """Чужие продавцы, чьи лоты видели за 60 дней, без свежей записи author_stats. Сначала самые активные."""
    rows = conn.execute(
        "SELECT lower(i.author) AS a, COUNT(*) AS n, MAX(COALESCE(i.published_ts, i.first_seen_ts)) AS last "
        "FROM items i LEFT JOIN author_stats s ON s.author = lower(i.author) "
        "WHERE i.source = 'deviantart' AND i.own = 0 AND COALESCE(i.author, '') != '' "
        "AND COALESCE(i.lot_type, 'other') != 'other' AND COALESCE(i.published_ts, i.first_seen_ts) >= ? "
        "AND (s.author IS NULL OR s.ts < ?) GROUP BY lower(i.author) ORDER BY n DESC, last DESC LIMIT ?",
        (now - AUTHOR_LOOKBACK_DAYS * 86400, now - AUTHOR_TTL_DAYS * 86400, limit)).fetchall()
    return [r["a"] for r in rows]


def author_pass(conn, client, now: float | None = None, budget: int = AUTHOR_BUDGET, sleep=time.sleep) -> int:
    """Сколько у чужих продавцов watchers и работ (кэш 30 дней): «уровень продавца» по факту, а не по
    числу его лотов, которые мы случайно видели (в живой папке 60 лотов — от 57 разных авторов).
    Возвращает, скольких проверили. RateLimited / NotConnected летят наверх — проход прекращается."""
    now = now or time.time()
    done = fails = 0
    for author in authors_to_check(conn, now, budget):
        try:
            st = da_browse.user_stats(client, author, sleep=sleep)
        except (da_api.RateLimited, da_api.NotConnected):
            raise
        except da_api.DAApiError as e:
            # Аккаунт удалён/переименован: запоминаем пустую запись, чтобы не спрашивать каждый час.
            log.info("user stats for %s failed: %s", author, e)
            st = {"watchers": None, "deviations": None}
        except Exception as e:  # noqa: BLE001 — сеть и т.п.: не записываем, попробуем потом
            log.warning("user stats for %s failed: %s", author, e)
            fails += 1
            if fails >= 3:
                break
            continue
        conn.execute("INSERT INTO author_stats (author, watchers, deviations, ts) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(author) DO UPDATE SET watchers = excluded.watchers, "
                     "deviations = excluded.deviations, ts = excluded.ts",
                     (author, st.get("watchers"), st.get("deviations"), now))
        conn.commit()
        done += 1
    return done


def author_size(conn, author: str | None) -> sqlite3.Row | None:
    """Последняя запись author_stats (watchers, deviations, ts) или None."""
    if not author:
        return None
    return conn.execute("SELECT * FROM author_stats WHERE author = ?", (author.lower(),)).fetchone()


def latest_stats(conn, item_id: str):
    return conn.execute("SELECT * FROM deviation_stats WHERE deviation_id = ? ORDER BY ts DESC LIMIT 1",
                        (item_id,)).fetchone()


def demand_by(conn, key_fn, days: int = LOOKBACK_DAYS, now: float | None = None, own: bool | None = False) -> list[dict]:
    """Спрос по ключу (например, вид из taxonomy): сколько лотов, в среднем фавов и просмотров на лот.

    key_fn(row) → ключ, список ключей или None. Берётся последний снимок каждого лота.
    own=False — только рынок, True — только её лоты, None — все.
    """
    now = now or time.time()
    cond = "" if own is None else f" AND i.own = {1 if own else 0}"
    rows = conn.execute(
        "SELECT i.*, s.views AS s_views, s.favourites AS s_favourites, s.comments AS s_comments FROM items i "
        "JOIN deviation_stats s ON s.deviation_id = i.id AND s.ts = "
        "(SELECT MAX(ts) FROM deviation_stats WHERE deviation_id = i.id) "
        f"WHERE i.source = 'deviantart' AND COALESCE(i.published_ts, i.first_seen_ts) >= ?{cond}",
        (now - days * 86400,)).fetchall()
    groups: dict[str, dict] = {}
    for row in rows:
        keys = key_fn(row)
        if keys is None:
            continue
        for key in ([keys] if isinstance(keys, str) else list(keys)):
            g = groups.setdefault(key, {"key": key, "lots": 0, "favourites": 0, "views": 0, "comments": 0,
                                        "with_views": 0, "favs_with_views": 0})
            g["lots"] += 1
            g["favourites"] += row["s_favourites"] or 0
            g["comments"] += row["s_comments"] or 0
            # Снимок со страницы папки — без просмотров: в среднее просмотров такие лоты не берём.
            if row["s_views"] is not None:
                g["with_views"] += 1
                g["views"] += row["s_views"]
                g["favs_with_views"] += row["s_favourites"] or 0
    out = []
    for g in groups.values():
        n = g["lots"]
        out.append({
            "key": g["key"], "lots": n,
            "favourites_avg": g["favourites"] / n, "comments_avg": g["comments"] / n,
            "views_avg": g["views"] / g["with_views"] if g["with_views"] else 0,
            "fav_per_view": g["favs_with_views"] / g["views"] if g["views"] else None,
        })
    return sorted(out, key=lambda d: (d["favourites_avg"], d["lots"]), reverse=True)
