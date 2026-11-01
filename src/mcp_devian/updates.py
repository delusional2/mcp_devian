"""«Есть обновление»: раз в сутки смотрим последний релиз на GitHub и сравниваем с установленной версией.

Никогда не падает: нет сети, лимит GitHub, странный ответ — просто None. Результат кешируется
в DATA_DIR/update-check.json, так что get_settings не ждёт сеть чаще раза в день.
"""

import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from . import __version__
from .config import DATA_DIR

log = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/delusional2/mcp_devian/releases/latest"
DOWNLOAD_URL = "https://github.com/delusional2/mcp_devian/releases/latest/download/mcp-devian.mcpb"
CHECK_EVERY = 24 * 3600
# После ошибки сети не дёргаем GitHub на каждый вызов, но и сутки не ждём.
RETRY_AFTER_ERROR = 6 * 3600
TIMEOUT = 5.0
CACHE_NAME = "update-check.json"
_TITLE_MAX = 80
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:([-+])\S*)?$")


def parse_version(tag: str) -> tuple[int, int, int, int] | None:
    """'v1.2.3' → (1, 2, 3, 1); пререлиз '1.2.3-beta' → (1, 2, 3, 0) — он младше самого релиза."""
    m = _VERSION_RE.match(str(tag or "").strip())
    if not m:
        return None
    major, minor, patch, sep = m.groups()
    return int(major), int(minor), int(patch or 0), 0 if sep == "-" else 1


def _cache_path(cache_path: Path | None) -> Path:
    return Path(cache_path) if cache_path else DATA_DIR / CACHE_NAME


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".update-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        log.warning("update cache not saved: %s", e)


def _clean_title(name: object, tag: str) -> str:
    title = " ".join(str(name or "").split())
    if not title or title.lstrip("v") == tag.lstrip("v"):
        return ""
    return title if len(title) <= _TITLE_MAX else title[: _TITLE_MAX - 1].rstrip() + "…"


def _fetch(http: httpx.Client | None) -> dict:
    """{'latest': '0.5.0', 'title': '...'} или {'error': '...'}.

    Под pytest без подставного клиента в сеть не ходим: тесты get_settings и фона не должны звать GitHub.
    """
    own = http is None
    if own and os.environ.get("PYTEST_CURRENT_TEST"):
        return {"error": "offline in tests"}
    # Репозиторий переименуют — GitHub ответит 301 на новый адрес; идём за ним.
    client = http or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    try:
        resp = client.get(RELEASES_API, timeout=TIMEOUT, headers={
            "User-Agent": f"mcp_devian/{__version__} (update check)",
            "Accept": "application/vnd.github+json",
        })
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        data = resp.json()
        if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
            return {"error": "no release"}
        tag = str(data.get("tag_name") or "")
        if not parse_version(tag):
            return {"error": f"bad tag {tag[:40]!r}"}
        latest = tag.strip().lstrip("v")
        return {"latest": latest, "title": _clean_title(data.get("name"), latest)}
    except (httpx.HTTPError, ValueError) as e:
        return {"error": type(e).__name__}
    finally:
        if own:
            client.close()


def _state(http, now: Callable[[], float], cache_path, force: bool) -> tuple[dict, Path]:
    """Кеш, при необходимости освежённый из сети. Без исключений."""
    path = _cache_path(cache_path)
    cache = _load(path)
    t = now()
    checked = cache.get("checked_at")
    age = t - checked if isinstance(checked, (int, float)) else None
    wait = RETRY_AFTER_ERROR if cache.get("error") else CHECK_EVERY
    if force or age is None or age < 0 or age >= wait:
        got = _fetch(http)
        cache["checked_at"] = t
        if "latest" in got:
            cache.update(latest=got["latest"], title=got["title"])
            cache.pop("error", None)
        else:
            cache["error"] = got["error"]  # что знали о релизе раньше — оставляем
        _save(path, cache)
    return cache, path


def _notice(cache: dict, current: str, short: bool = False) -> str | None:
    latest = cache.get("latest")
    new, cur = parse_version(latest or ""), parse_version(current)
    if not new or not cur or new <= cur:
        return None
    if short:  # для уведомления macOS: ссылка там не кликается, а длинный текст обрезается
        return f"Есть обновление {latest} (у тебя {current}) — спроси Claude про настройки."
    title = f": «{cache['title']}»" if cache.get("title") else ""
    return (f"Есть обновление {latest} (у тебя {current}){title}. "
            f"Скачай {DOWNLOAD_URL} и открой файл — Claude Desktop обновит расширение, настройки сохранятся.")


def check_for_update(*, current: str = __version__, http: httpx.Client | None = None,
                     now: Callable[[], float] = time.time, cache_path: Path | None = None,
                     force: bool = False) -> str | None:
    """Однострочное «есть обновление …» или None. В сеть — не чаще раза в сутки; никогда не падает."""
    try:
        cache, _ = _state(http, now, cache_path, force)
        return _notice(cache, current)
    except Exception as e:  # проверка обновлений не должна ломать ничего вокруг
        log.warning("update check failed: %s", e)
        return None


def notice_to_announce(*, current: str = __version__, http: httpx.Client | None = None,
                       now: Callable[[], float] = time.time, cache_path: Path | None = None,
                       short: bool = False) -> str | None:
    """Для фонового уведомления: то же сообщение, но про каждую версию — только один раз.

    short=True — короткая строка без ссылки (для уведомления macOS, до 220 знаков)."""
    try:
        cache, path = _state(http, now, cache_path, False)
        note = _notice(cache, current, short)
        if not note or cache.get("announced") == cache.get("latest"):
            return None
        cache["announced"] = cache["latest"]
        _save(path, cache)
        return note
    except Exception as e:
        log.warning("update announce failed: %s", e)
        return None
