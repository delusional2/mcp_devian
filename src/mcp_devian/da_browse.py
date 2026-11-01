"""Чтение рынка через официальный API DeviantArt (scope browse, он у нас уже есть). Только чтение.

Каждый помощник делает один запрос через DAClient._request (там 429 → RateLimited, нет входа →
NotConnected) и после него ждёт 1.5 с. RateLimited и NotConnected летят наверх: вызывающий
сразу прекращает весь проход.

Порядок выдачи /browse/tags и /gallery/{folderid} не документирован: на «сначала новые» не
полагаемся, свежесть фильтруем по published_ts.
"""

import time
from datetime import datetime

from . import da_api
from . import deviantart as da

PAUSE_S = 1.5
TAGS_LIMIT = 50
FOLDER_LIMIT = 24
METADATA_CHUNK = 50


def _ts(value) -> float | None:
    """published_time: строка с unix-временем ("1698765432") или ISO-дата."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def deviation_brief(dev: dict) -> dict | None:
    """Работа из выдачи API → {uuid, number, url, title, author, published_ts, comments, favourites}.
    Без номера в ссылке (журналы, статусы) — None."""
    url = str(dev.get("url") or "")
    m = da.DEVIATION_ID.search(url.split("?")[0].split("#")[0])
    if not m or not dev.get("deviationid"):
        return None
    stats = dev.get("stats") or {}
    author = dev.get("author") or {}
    return {
        "uuid": str(dev["deviationid"]),
        "number": m.group(1),
        "url": url,
        "title": str(dev.get("title") or ""),
        "author": (author.get("username") if isinstance(author, dict) else None) or None,
        "published_ts": _ts(dev.get("published_time")),
        "comments": _int(stats.get("comments")),
        "favourites": _int(stats.get("favourites")),
    }


def _briefs(body: dict) -> list[dict]:
    out = []
    for dev in body.get("results") or []:
        if isinstance(dev, dict):
            b = deviation_brief(dev)
            if b:
                out.append(b)
    return out


def browse_tags(client, tag: str, offset: int = 0, limit: int = TAGS_LIMIT, sleep=time.sleep
                ) -> tuple[list[dict], int | None]:
    """GET /browse/tags: работы с тегом. (работы, next_offset или None, если дальше нет)."""
    params = {"tag": tag.strip().lstrip("#"), "limit": max(1, min(int(limit), TAGS_LIMIT)), "offset": max(0, offset)}
    request = client._request  # нет такого метода — исключение до запроса, без паузы
    try:
        body = request("GET", "/browse/tags", params=params)
    finally:
        sleep(PAUSE_S)
    nxt = body.get("next_offset")
    return _briefs(body), (int(nxt) if body.get("has_more") and nxt is not None else None)


def tag_suggest(client, partial: str, sleep=time.sleep) -> list[str]:
    """GET /browse/tags/search: какие теги вообще существуют (подсказка, без количества)."""
    request = client._request
    try:
        body = request("GET", "/browse/tags/search", params={"tag_name": partial.strip().lstrip("#")})
    finally:
        sleep(PAUSE_S)
    out = []
    for r in body.get("results") or []:
        name = r.get("tag_name") if isinstance(r, dict) else r
        if name and str(name) not in out:
            out.append(str(name))
    return out


def folder_page(client, group: str, folderid: str, offset: int = 0, limit: int = FOLDER_LIMIT,
                sleep=time.sleep) -> list[dict]:
    """GET /gallery/{folderid}?username=группа: страница папки группы с UUID и статистикой каждой работы."""
    params = {"username": group, "mature_content": "false", "offset": max(0, offset),
              "limit": max(1, min(int(limit), FOLDER_LIMIT))}
    request = client._request
    try:
        body = request("GET", f"/gallery/{folderid}", params=params)
    finally:
        sleep(PAUSE_S)
    return _briefs(body)


def user_stats(client, name: str, sleep=time.sleep) -> dict:
    """GET /user/profile/{name}?expand=user.stats → {watchers, friends, deviations, type}."""
    request = client._request
    try:
        body = request("GET", f"/user/profile/{name.strip()}", params={"expand": "user.stats"})
    finally:
        sleep(PAUSE_S)
    user = body.get("user") or {}
    ustats = user.get("stats") or {}
    stats = body.get("stats") or {}
    return {"watchers": _int(ustats.get("watchers")), "friends": _int(ustats.get("friends")),
            "deviations": _int(stats.get("user_deviations")), "type": user.get("type")}


def metadata(client, uuids, sleep=time.sleep, ext_stats: bool = True) -> dict[str, dict]:
    """GET /deviation/metadata пачками по 50: {uuid: {title, author, description, tags, views, favourites,
    comments}}. В DAClient.deviation_metadata нет описания, а для разбора цены оно нужно.

    Сколько id можно за раз, документ не говорит (есть только ошибка 0 «слишком много»): на ней делим
    пачку пополам, как DAClient.deviation_metadata. ext_stats=False — без статистики (описание и теги
    приходят и так), если нужна только цена из описания."""
    ids = [str(u) for u in dict.fromkeys(uuids) if u]
    out: dict[str, dict] = {}
    chunk, i = METADATA_CHUNK, 0
    while i < len(ids):
        part = ids[i:i + chunk]
        params: list[tuple[str, str]] = [("deviationids[]", u) for u in part]
        if ext_stats:
            params.append(("ext_stats", "true"))
        request = client._request
        try:
            body = request("GET", "/deviation/metadata", params=params)
        except da_api.RateLimited:
            raise
        except da_api.DAApiError as e:
            if str(e.code) == "0" and chunk > 1:
                chunk = max(1, chunk // 2)
                continue
            raise
        finally:
            sleep(PAUSE_S)
        i += len(part)
        for meta in body.get("metadata") or []:
            if not isinstance(meta, dict) or not meta.get("deviationid"):
                continue
            stats = meta.get("stats") or {}
            author = meta.get("author") or {}
            out[str(meta["deviationid"])] = {
                "title": str(meta.get("title") or ""),
                "author": author.get("username") if isinstance(author, dict) else None,
                "description": str(meta.get("description") or ""),
                "tags": [t.get("tag_name") for t in meta.get("tags") or []
                         if isinstance(t, dict) and t.get("tag_name")],
                "views": _int(stats.get("views")),
                "favourites": _int(stats.get("favourites")),
                "comments": _int(stats.get("comments")),
            }
    return out
