"""DeviantArt: официальный OAuth2 API для публикации и рассылки по группам.

Вход — Authorization Code + PKCE (публичный клиент, без секрета): она нажимает
«Разрешить» на сайте DeviantArt, пароль никуда не вводится. Токены лежат в файле
с правами 600 в папке данных.

Рассылка в группы идёт только через stash/publish (groups[] + group_folders[]).
deviation/edit не трогаем: по старой документации он стирает поля, которые не передали.
"""

import base64
import contextlib
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import __version__
from .bids import Comment
from .config import DATA_DIR

AUTHORIZE_URL = "https://www.deviantart.com/oauth2/authorize"
TOKEN_URL = "https://www.deviantart.com/oauth2/token"
REVOKE_URL = "https://www.deviantart.com/oauth2/revoke"
API_BASE = "https://www.deviantart.com/api/v1/oauth2"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPES = "basic browse stash publish"
MINOR_VERSION = "20240701"
TOKEN_FILE = "da-token.json"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_FILE_BYTES = 30 * 1024 * 1024
# Обновляем токен чуть заранее, чтобы он не истёк посреди запроса.
EXPIRY_SKEW_S = 60
MAX_TRIES = 4  # первая попытка + 3 повтора (1, 2, 4 с)


class DAApiError(Exception):
    def __init__(self, message: str, code: str | int | None = None, description: str | None = None):
        super().__init__(message)
        self.code = code
        self.description = description


class RateLimited(DAApiError):
    pass


class NotConnected(DAApiError):
    pass


def _headers(token: str | None = None) -> dict[str, str]:
    h = {
        "User-Agent": f"mcp_devian/{__version__} (personal assistant)",
        "Accept-Encoding": "gzip",
        "dA-minor-version": MINOR_VERSION,
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _error_from(resp: httpx.Response) -> DAApiError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    error = body.get("error") or f"http_{resp.status_code}"
    desc = body.get("error_description") or ""
    code = body.get("error_code", error)
    if resp.status_code == 401 or error in ("invalid_token", "invalid_grant"):
        return NotConnected("Подключение к DeviantArt истекло или отозвано — нужно разрешить доступ заново.",
                            code, desc)
    text = f"DeviantArt не принял запрос: {desc or error}"
    return DAApiError(text, code, desc)


# --- Токены ---

class TokenStore:
    """Токены в JSON-файле с правами 600; запись атомарная."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DATA_DIR / TOKEN_FILE

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".da-token-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False))
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def _token_record(payload: dict, now: float, previous: dict | None = None) -> dict:
    if not payload.get("access_token"):
        raise DAApiError("DeviantArt не выдал токен доступа.", payload.get("error"), payload.get("error_description"))
    record = dict(previous or {})
    record.update(
        access_token=payload["access_token"],
        # Если DeviantArt вернул новый refresh_token, старый больше не годится.
        refresh_token=payload.get("refresh_token") or record.get("refresh_token"),
        expires_at=now + float(payload.get("expires_in") or 3600),
        scope=payload.get("scope") or record.get("scope") or SCOPES,
    )
    return record


# --- OAuth ---

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(client_id: str, state: str, challenge: str, redirect_uri: str = REDIRECT_URI) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _post_token(http: httpx.Client, data: dict) -> dict:
    try:
        resp = http.post(TOKEN_URL, data=data, headers=_headers())
    except httpx.HTTPError as e:
        raise DAApiError(f"нет связи с DeviantArt: {type(e).__name__}") from e
    if resp.status_code >= 400:
        raise _error_from(resp)
    try:
        return resp.json()
    except ValueError as e:
        raise DAApiError("DeviantArt вернул что-то странное вместо токена.") from e


PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8"><title>DeviantArt</title>
<body style="font-family:-apple-system,sans-serif;max-width:32em;margin:4em auto;line-height:1.5">
<h2>{title}</h2><p>{text}</p></body></html>"""


def run_oauth_flow(client_id: str, store: TokenStore, port: int = 8765, timeout_s: float = 300,
                   opener=webbrowser.open, http: httpx.Client | None = None) -> dict:
    """Открыть страницу «Разрешить» в браузере и дождаться возврата на localhost.

    Блокирует, пока она не нажмёт кнопку (или до таймаута). Возвращает {"username": ...}.
    """
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://localhost:{port}/callback"
    outcome: dict = {}
    own_http = http is None
    http = http or httpx.Client(timeout=30)

    def handle(params: dict[str, str]) -> tuple[str, str]:
        if params.get("state") != state:
            outcome["error"] = DAApiError("Ответ DeviantArt не совпал с запросом (state). Попробуй подключиться ещё раз.")
            return "Что-то пошло не так", "Ответ не совпал с запросом. Вернись в чат и попробуй ещё раз."
        if params.get("error"):
            outcome["error"] = DAApiError("Доступ к DeviantArt не разрешён.", params.get("error"),
                                          params.get("error_description"))
            return "Доступ не разрешён", "Ничего страшного: можно вернуться в чат и попробовать снова."
        code = params.get("code")
        if not code:
            outcome["error"] = DAApiError("DeviantArt не прислал код подтверждения.")
            return "Что-то пошло не так", "Вернись в чат и попробуй ещё раз."
        try:
            payload = _post_token(http, {"grant_type": "authorization_code", "client_id": client_id,
                                         "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier})
            store.save(_token_record(payload, time.time()))
            me = DAClient(client_id, store, http=http).whoami()
            record = store.load() or {}
            record["username"] = me["username"]
            store.save(record)
        except DAApiError as e:
            outcome["error"] = e
            return "Не получилось", html.escape(str(e))
        outcome["username"] = me["username"]
        return "Готово, можно закрыть вкладку", f"DeviantArt подключён: {html.escape(me['username'])}. Возвращайся в чат 💛"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — имя задаёт http.server
            url = urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            title, text = handle(params)
            body = PAGE.format(title=html.escape(title), text=text).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # stdout сервера MCP занят протоколом
            pass

    try:
        servers = _localhost_servers(port, Handler)
    except OSError as e:
        if own_http:
            http.close()
        raise DAApiError(f"Не получилось открыть порт {port} для входа: {e}") from e
    try:
        # Браузер открываем в отдельном потоке: сервер уже слушает, а opener может блокировать.
        threading.Thread(target=opener, args=(authorize_url(client_id, state, challenge, redirect_uri),),
                         daemon=True).start()
        deadline = time.time() + timeout_s
        while time.time() < deadline and not outcome:
            for server in servers:
                server.handle_request()
                if outcome:
                    break
    finally:
        for server in servers:
            server.server_close()
        if own_http:
            http.close()
    if "error" in outcome:
        raise outcome["error"]
    if "username" not in outcome:
        raise DAApiError("Не дождалась ответа от DeviantArt: время вышло. Попробуй подключиться ещё раз.")
    return {"username": outcome["username"]}


class _HTTPServerV6(HTTPServer):
    address_family = socket.AF_INET6


def _localhost_servers(port: int, handler) -> list[HTTPServer]:
    """`localhost` браузер может открыть и как 127.0.0.1, и как ::1 — слушаем оба.

    IPv4 обязателен; IPv6 — если он есть на маке (иначе молча пропускаем).
    """
    servers: list[HTTPServer] = [HTTPServer(("127.0.0.1", port), handler)]
    if socket.has_ipv6:
        with contextlib.suppress(OSError):
            servers.append(_HTTPServerV6(("::1", port), handler))
    for server in servers:
        server.timeout = 0.25
    return servers


def connect_main(client_id: str) -> int:
    """Точка входа для команды подключения: открывает браузер и ждёт «Разрешить»."""
    if not client_id:
        print("Нет client_id приложения DeviantArt.")
        return 1
    print("Открываю DeviantArt в браузере. Нажми «Разрешить» (Authorize) — пароль сюда вводить не нужно.")
    try:
        result = run_oauth_flow(client_id, TokenStore())
    except DAApiError as e:
        print(f"Не получилось: {e}")
        return 1
    print(f"Готово: подключён аккаунт {result['username']}.")
    return 0


# --- Клиент API ---

def _bool(v: bool) -> str:
    return "true" if v else "false"


def _clean_tags(tags) -> list[str]:
    out, seen = [], set()
    for t in tags or ():
        clean = re.sub(r"[^A-Za-z0-9_]", "", str(t).replace(" ", "_"))
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            out.append(clean)
    return out


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", name or "").strip().lower()


class DAClient:
    def __init__(self, client_id: str, store: TokenStore, http: httpx.Client | None = None, sleep=time.sleep,
                 clock=time.time):
        self.client_id = client_id
        self.store = store
        self.http = http or httpx.Client(timeout=60)
        self.sleep = sleep
        self.clock = clock

    # токены

    def is_connected(self) -> bool:
        return self.store.load() is not None

    def _refresh(self, record: dict) -> dict:
        try:
            payload = _post_token(self.http, {"grant_type": "refresh_token", "client_id": self.client_id,
                                              "refresh_token": record["refresh_token"]})
        except NotConnected:
            self.store.clear()
            raise
        fresh = _token_record(payload, self.clock(), record)
        self.store.save(fresh)
        return fresh

    def _access_token(self, force_refresh: bool = False) -> str:
        record = self.store.load()
        if not record:
            raise NotConnected("DeviantArt ещё не подключён: нужно один раз нажать «Разрешить» на сайте.")
        if force_refresh or not record.get("access_token") or \
                float(record.get("expires_at", 0)) - EXPIRY_SKEW_S <= self.clock():
            record = self._refresh(record)
        return record["access_token"]

    # запросы

    def _request(self, method: str, path: str, *, params=None, data=None, files=None,
                 retry_server_errors: bool = True) -> dict:
        url = f"{API_BASE}{path}"
        refreshed = False
        attempt = 0
        while True:
            token = self._access_token()
            try:
                resp = self.http.request(method, url, params=params, data=data, files=files, headers=_headers(token))
            except httpx.HTTPError as e:
                raise DAApiError(f"нет связи с DeviantArt: {type(e).__name__}") from e
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._access_token(force_refresh=True)
                continue
            # 429 значит «не обработано, притормози» — повторять безопасно. 5xx — только где разрешено:
            # повтор публикации после 500 мог бы выложить работу дважды.
            retryable = resp.status_code == 429 or (resp.status_code >= 500 and retry_server_errors)
            if retryable and attempt < MAX_TRIES - 1:
                self.sleep(2 ** attempt)
                attempt += 1
                continue
            if resp.status_code == 429:
                raise RateLimited("DeviantArt просит притормозить (429). Попробуем позже.", 429)
            if resp.status_code >= 400:
                raise _error_from(resp)
            try:
                body = resp.json()
            except ValueError as e:
                raise DAApiError("DeviantArt вернул что-то странное.") from e
            if isinstance(body, dict) and body.get("status") == "error":
                raise _error_from(resp)
            return body if isinstance(body, dict) else {"results": body}

    # чтение

    def whoami(self) -> dict:
        body = self._request("GET", "/user/whoami")
        return {"username": body.get("username"), "userid": body.get("userid")}

    def user_profile(self, name: str) -> dict:
        body = self._request("GET", f"/user/profile/{name.strip()}")
        user = body.get("user") or {}
        return {"userid": user.get("userid"), "username": user.get("username"), "type": user.get("type")}

    def gallery_folders(self, username: str) -> list[dict]:
        folders: list[dict] = []
        offset = 0
        while True:
            body = self._request("GET", "/gallery/folders",
                                 params={"username": username, "limit": 50, "offset": offset})
            stack = list(body.get("results") or [])
            while stack:
                f = stack.pop(0)
                folders.append({"folder_id": f.get("folderid"), "name": f.get("name") or "", "parent": f.get("parent")})
                stack[:0] = f.get("subfolders") or []
            if not body.get("has_more") or body.get("next_offset") in (None, offset):
                return folders
            offset = int(body["next_offset"])

    def resolve_group_folder(self, group: str, folder_name: str) -> dict:
        profile = self.user_profile(group)
        if not profile.get("userid"):
            raise DAApiError(f"Не нашла группу {group} на DeviantArt.", "group_not_found")
        folders = self.gallery_folders(profile["username"] or group)
        want = _norm(folder_name)
        exact = [f for f in folders if _norm(f["name"]) == want]
        loose = exact or [f for f in folders if want and want in _norm(f["name"])]
        if len(loose) == 1:
            f = loose[0]
            return {"group": profile["username"] or group, "group_id": profile["userid"],
                    "folder_id": f["folder_id"], "folder_name": f["name"]}
        names = ", ".join(f["name"] for f in folders) or "папок нет"
        what = "несколько похожих папок" if loose else "такой папки нет"
        raise DAApiError(f"В группе {group} {what} «{folder_name}». Есть: {names}.", "folder_not_found")

    # комментарии (ставки на аукционах — это комментарии под работой)

    def deviation_comments(self, deviationid: str, max_comments: int = 500) -> list[Comment]:
        """Все комментарии под работой, включая ответы, по времени (сначала старые).

        API берёт UUID работы, а не номер из ссылки: см. deviation_uuid_from_url.
        """
        out: list[Comment] = []
        offset = 0
        while len(out) < max_comments:
            body = self._request("GET", f"/comments/deviation/{deviationid}",
                                 params={"maxdepth": 5, "limit": 50, "offset": offset,
                                         "expand": "comment.fulltext"})
            for raw in body.get("thread") or []:
                if raw.get("hidden"):
                    continue  # скрытые/удалённые автором — это не ставки
                out.append(_comment_from_api(raw))
            if not body.get("has_more") or body.get("next_offset") in (None, offset):
                break
            offset = int(body["next_offset"])
        return _with_depths(out)[:max_comments]

    def deviation_uuid_from_url(self, url: str, folders=(), max_pages: int = 10) -> str | None:
        """UUID работы по ссылке вида …/art/<slug>-<номер>.

        Документированного поиска по ссылке или номеру в API нет. Поэтому ищем работу
        в галерее автора (/gallery/all, свежие сверху), а если передали folders —
        ещё и в этих папках групп [(группа, folder_id)]. Старые работы глубже
        max_pages*24 так не найти: тогда None.
        """
        m = DEVIATION_URL.match(url.strip())
        if not m:
            raise DAApiError("Нужна ссылка на работу вида https://www.deviantart.com/автор/art/название-12345.",
                             "bad_url")
        author, number = m.group(1), m.group(2)
        found = self._scan_for(number, "/gallery/all", {"username": author}, max_pages)
        for group, folder_id in folders:
            found = found or self._scan_for(number, f"/gallery/{folder_id}", {"username": group}, max_pages)
        return found

    def _scan_for(self, number: str, path: str, params: dict, max_pages: int) -> str | None:
        offset = 0
        for _ in range(max_pages):
            body = self._request("GET", path, params={**params, "limit": 24, "offset": offset})
            for dev in body.get("results") or []:
                m = DEVIATION_URL.match(str(dev.get("url") or ""))
                if m and m.group(2) == number and dev.get("deviationid"):
                    return str(dev["deviationid"])
            if not body.get("has_more") or body.get("next_offset") in (None, offset):
                return None
            offset = int(body["next_offset"])
        return None

    # публикация

    def stash_submit(self, file_path: str | Path, title: str, artist_comments: str = "", tags=()) -> int:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise DAApiError(f"Файл не найден: {path}", "bad_file")
        if path.suffix.lower() not in IMAGE_EXTS:
            raise DAApiError(f"Это не картинка ({path.suffix or 'без расширения'}): нужен png, jpg, gif или webp.",
                             "bad_file")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise DAApiError("Файл больше 30 МБ — DeviantArt его не примет.", "bad_file")
        data: dict = {"title": title.strip()[:50] or path.stem[:50], "artist_comments": artist_comments}
        clean = _clean_tags(tags)
        if clean:
            data["tags[]"] = clean
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            body = self._request("POST", "/stash/submit", data=data, files={"file": (path.name, fh, mime)},
                                 retry_server_errors=False)
        itemid = body.get("itemid")
        if itemid is None:
            raise DAApiError("DeviantArt не вернул номер загруженной работы.")
        return int(itemid)

    def stash_publish(self, itemid: int, *, is_mature: bool, galleryids=(), tags=(), groups=(),
                      is_ai_generated: bool = False, noai: bool = True, allow_comments: bool = True,
                      feature: bool = True, mature_level: str | None = None) -> dict:
        pairs = [(str(g), str(f)) for g, f in groups]
        data: dict = {
            "itemid": str(itemid),
            "is_mature": _bool(is_mature),
            "is_ai_generated": _bool(is_ai_generated),
            "noai": _bool(noai),
            "allow_comments": _bool(allow_comments),
            "feature": _bool(feature),
        }
        if is_mature and mature_level:
            data["mature_level"] = mature_level
        if galleryids:
            data["galleryids[]"] = [str(g) for g in galleryids]
        clean = _clean_tags(tags)
        if clean:
            data["tags[]"] = clean
        if pairs:
            # Порядок важен: i-я папка относится к i-й группе.
            data["groups[]"] = [g for g, _ in pairs]
            data["group_folders[]"] = [f for _, f in pairs]
        body = self._request("POST", "/stash/publish", data=data, retry_server_errors=False)
        return {"url": body.get("url"), "deviationid": body.get("deviationid")}

    def revoke(self) -> bool:
        record = self.store.load()
        self.store.clear()
        if not record:
            return False
        # Токен у нас уже удалён; если DeviantArt недоступен, отзыв на их стороне — лучшее усилие.
        with contextlib.suppress(httpx.HTTPError):
            self.http.post(REVOKE_URL, data={"token": record.get("refresh_token"), "revoke_refresh_only": "false"},
                           headers=_headers())
        return True


# --- Разбор комментариев ---

DEVIATION_URL = re.compile(r"^https://www\.deviantart\.com/([A-Za-z0-9_-]+)/art/[^/?#]*?-?(\d+)/?(?:[?#].*)?$")
_TAG = re.compile(r"<[^>]+>")


def _posted_ts(value) -> float:
    """posted у DeviantArt — ISO 8601 вида 2026-10-31T20:30:45-0700 (формат в спеке не описан)."""
    if value is None:
        return 0.0
    if isinstance(value, int | float) or str(value).isdigit():
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _plain(body_html: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>", "\n", body_html or "", flags=re.I)
    return re.sub(r"[ \t]+", " ", html.unescape(_TAG.sub("", text))).strip()


def _comment_from_api(raw: dict) -> Comment:
    text = _plain(raw.get("body") or "")
    if not text:
        text = ((raw.get("text_content") or {}).get("excerpt") or "").strip()
    return Comment(comment_id=str(raw.get("commentid") or ""), parent_id=raw.get("parentid") or None,
                   user=str((raw.get("user") or {}).get("username") or ""),
                   posted_ts=_posted_ts(raw.get("posted")), text=text)


def _with_depths(comments: list[Comment]) -> list[Comment]:
    """Глубина ответа — по цепочке parentid; итог по времени, старые первыми."""
    by_id = {c.comment_id: c for c in comments}
    for c in comments:
        depth, parent, seen = 0, c.parent_id, set()
        while parent and parent in by_id and parent not in seen and depth < 10:
            seen.add(parent)
            depth += 1
            parent = by_id[parent].parent_id
        c.depth = depth if c.parent_id else 0
        if c.parent_id and c.parent_id not in by_id:
            c.depth = max(c.depth, 1)  # родителя не загрузили, но это точно ответ
    return sorted(comments, key=lambda c: (c.posted_ts, c.comment_id))


# --- Её картинки ---

def recent_images(folders=None, limit: int = 10, now: float | None = None) -> list[dict]:
    """Свежие картинки в Загрузках, на Рабочем столе и в Изображениях (и на уровень глубже)."""
    now = now or time.time()
    home = Path.home()
    roots = [Path(f).expanduser() for f in folders] if folders else [home / "Downloads", home / "Desktop",
                                                                     home / "Pictures"]
    found: dict[Path, os.stat_result] = {}
    for root in roots:
        try:
            level1 = [p for p in root.iterdir() if not p.name.startswith(".")]
        except OSError:
            continue
        candidates = list(level1)
        for d in level1:
            if d.is_dir() and not d.name.endswith((".app", ".photoslibrary", ".bundle")):
                try:
                    candidates += [p for p in d.iterdir() if not p.name.startswith(".")]
                except OSError:
                    continue
        for p in candidates:
            if p.suffix.lower() in IMAGE_EXTS and p.is_file():
                try:
                    found[p] = p.stat()
                except OSError:
                    continue
    ordered = sorted(found.items(), key=lambda kv: kv[1].st_mtime, reverse=True)[:limit]
    return [{"path": str(p), "name": p.name, "modified": st.st_mtime, "size": st.st_size,
             "age_s": max(0.0, now - st.st_mtime)} for p, st in ordered]
