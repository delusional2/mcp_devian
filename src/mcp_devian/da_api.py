"""DeviantArt: официальный OAuth2 API для публикации и рассылки по группам.

Вход — Authorization Code + PKCE (публичный клиент, без секрета): она нажимает
«Разрешить» на сайте DeviantArt, пароль никуда не вводится. Токены лежат в файле
с правами 600 в папке данных.

Рассылка в группы идёт только через stash/publish (groups[] + group_folders[]).
deviation/edit по старой документации стирает поля, которые не передали. Поэтому правка названия
(edit_deviation) сначала читает текущие значения (edit_state) и отправляет их все заново, а если
что-то прочитать не удалось — отказывается. После правки verify_edit перечитывает работу.
"""

import base64
import contextlib
import fcntl
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

from . import __version__, da_access
from .bids import Comment
from .config import DATA_DIR

AUTHORIZE_URL = "https://www.deviantart.com/oauth2/authorize"
TOKEN_URL = "https://www.deviantart.com/oauth2/token"
REVOKE_URL = "https://www.deviantart.com/oauth2/revoke"
API_BASE = "https://www.deviantart.com/api/v1/oauth2"
REDIRECT_URI = "http://localhost:8765/callback"
# user — для /user/whoami (по спеке нужны basic + user); gallery — папки её галереи;
# comment.post — её комментарий при закрытии аукциона. Новые права требуют ещё раз нажать «Разрешить».
SCOPES = "basic user browse stash publish gallery comment.post"
EDIT_SCOPES = ("stash", "publish")
GALLERY_SCOPES = ("browse", "gallery")
COMMENT_SCOPES = ("browse", "comment.post")
MINOR_VERSION = "20240701"
TOKEN_FILE = "da-token.json"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_FILE_BYTES = 30 * 1024 * 1024
# Обновляем токен чуть заранее, чтобы он не истёк посреди запроса.
EXPIRY_SKEW_S = 60
MAX_TRIES = 4  # первая попытка + 3 повтора (1, 2, 4 с) — только для 5xx на чтении
MAX_TITLE = 50
MATURE_LEVELS = ("strict", "moderate")
MATURE_CLASSES = ("nudity", "sexual", "gore", "language", "ideology")
# Сколько дополнительных запросов можно потратить на глубокие ветки ответов под одной работой.
DEEP_REPLY_REQUESTS = 20
UUID_MISS_TTL_S = 24 * 3600
METADATA_CHUNK = 50
# move_deviations / remove_deviations / copy_deviations: не больше 24 работ за вызов (старая документация).
GALLERY_CHUNK = 24
FOLDER_RECHECK_S = 3  # потерялся ответ на создание папки: пауза перед вторым взглядом на папки
FOLDER_NAME_MAX = 50
# Лицензию CC API показывает только строкой, а настройки её не отдаёт: правка могла бы её сбросить.
# Запрос мог дойти, а ответа нет или он нечитаем: для записи исход неизвестен.
LOST_RESPONSE = ("network", "bad_response")
CC_LICENSE = re.compile(r"creative\s*commons|\bcc[\s-]?by", re.I)
RECONNECT = ("Нужно заново подключить DeviantArt: для этого помощнику нужны новые права (правка работ, "
             "папки галереи, комментарии). Вызови accounts:da_connect — она просто ещё раз нажмёт «Разрешить» на DeviantArt.")


class DAApiError(Exception):
    def __init__(self, message: str, code: str | int | None = None, description: str | None = None,
                 status: int | None = None):
        super().__init__(message)
        self.code = code
        self.description = description
        self.status = status


class RateLimited(DAApiError):
    pass


class Throttled(RateLimited):
    """Запрос не отправлен: DeviantArt недавно просил притормозить (пауза) или исчерпан свой бюджет API."""

    def __init__(self, block: da_access.Block):
        super().__init__(block.message, "throttled")
        self.block = block

    @property
    def why(self) -> str:
        return self.block.why


class NotConnected(DAApiError):
    pass


class NeedsReconnect(NotConnected):
    """Подключение есть, но у него нет нужных прав (scope): нужно ещё раз нажать «Разрешить»."""


class UncertainPublish(DAApiError):
    """Запрос на публикацию ушёл, а ответа нет (обрыв, таймаут, 5xx): работа МОГЛА выложиться.

    Повторять нельзя — пусть она сначала проверит свою галерею.
    """


# Ошибки stash/publish по спеке; 0 и 1 — согласие с правилами DeviantArt.
PUBLISH_ERRORS = {
    0: "DeviantArt просит принять правила публикации (submission policy)",
    1: "DeviantArt просит принять условия использования (terms of service)",
    2: "категория не найдена",
    3: "неподходящая категория",
    4: "неверные настройки лицензии",
    5: "неверное разрешение показа",
    6: "публикация не удалась",
    7: "работа не найдена в Sta.sh",
    8: "нужна картинка-превью",
    9: "эта работа уже опубликована",
}


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
    low = f"{error} {desc}".lower()
    # Как DeviantArt сообщает о нехватке прав, не описано: ловим и стандартный insufficient_scope,
    # и «unauthorized»/403 со словом scope.
    if error == "insufficient_scope" or ("scope" in low and (resp.status_code == 403 or error == "unauthorized")):
        return NeedsReconnect(RECONNECT, code, desc, resp.status_code)
    if resp.status_code == 401 or error in ("invalid_token", "invalid_grant"):
        return NotConnected("Подключение к DeviantArt истекло или отозвано — нужно разрешить доступ заново.",
                            code, desc, resp.status_code)
    text = f"DeviantArt не принял запрос: {desc or error}"
    return DAApiError(text, code, desc, resp.status_code)


def _api_error_body(resp: httpx.Response) -> bool:
    """Ответ — ошибка API в JSON ({"error": …}): например, нехватка прав. Иначе 403 — блокировка."""
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and bool(body.get("error"))


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


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _refresh_lock(path: Path):
    """Обновление токена — по одному на всех: и между потоками, и между процессами
    (фоновая проверка, MCP-сервер и отправка черновика делят один файл токена)."""
    with _THREAD_LOCKS_GUARD:
        tlock = _THREAD_LOCKS.setdefault(str(path), threading.Lock())
    with tlock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _token_record(payload: dict, now: float, previous: dict | None = None) -> dict:
    if not payload.get("access_token"):
        raise DAApiError("DeviantArt не выдал токен доступа.", payload.get("error"), payload.get("error_description"))
    record = dict(previous or {})
    record.update(
        access_token=payload["access_token"],
        # Если DeviantArt вернул новый refresh_token, старый больше не годится.
        refresh_token=payload.get("refresh_token") or record.get("refresh_token"),
        expires_at=now + float(payload.get("expires_in") or 3600),
        # Только то, что DeviantArt действительно выдал: иначе запись «обещала» бы права, которых нет.
        scope=payload.get("scope") or record.get("scope") or "",
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
            # Чужой или устаревший запрос: не обрываем вход, ждём настоящий ответ DeviantArt.
            return "Что-то пошло не так", "Это не тот ответ, которого ждёт помощник. Вкладку можно закрыть."
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
        except DAApiError as e:
            outcome["error"] = e
            return "Не получилось", html.escape(str(e))
        # Имя — приятная деталь, а не условие: если whoami не ответил, подключение всё равно есть.
        name = ""
        try:
            # Без повторов: браузер ждёт страницу, а за 5xx-повторами он не дождётся ответа.
            body = DAClient(client_id, store, http=http)._request("GET", "/user/whoami", retry_server_errors=False)
            name = str(body.get("username") or "")
        except NotConnected as e:
            store.clear()
            outcome["error"] = e
            return "Не получилось", html.escape(str(e))
        except DAApiError:
            name = ""
        if name:
            record = store.load() or {}
            record["username"] = name
            store.save(record)
        outcome["username"] = name
        shown = f": {html.escape(name)}" if name else ""
        return "Готово, можно закрыть вкладку", f"DeviantArt подключён{shown}. Возвращайся в чат 💛"

    class Handler(BaseHTTPRequestHandler):
        # Браузеры открывают «запасные» соединения без запроса: без таймаута одно такое
        # соединение блокировало бы вход навсегда и держало порт. На localhost запрос
        # приходит сразу, поэтому ждём недолго: настоящий ответ стоит в очереди за молчуном.
        timeout = 2

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
                if time.time() >= deadline:
                    break
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


class _QuietHTTPServer(HTTPServer):
    def handle_error(self, request, client_address):  # обрыв или таймаут соединения — не шумим в лог
        pass


class _HTTPServerV6(_QuietHTTPServer):
    address_family = socket.AF_INET6


def _localhost_servers(port: int, handler) -> list[HTTPServer]:
    """`localhost` браузер может открыть и как 127.0.0.1, и как ::1 — слушаем оба.

    IPv4 обязателен; IPv6 — если он есть на маке (иначе молча пропускаем).
    """
    servers: list[HTTPServer] = [_QuietHTTPServer(("127.0.0.1", port), handler)]
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
    print(f"Готово: подключён аккаунт {result['username'] or '(имя не узнала)'}.")
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


def normalize_publish_fields(title: str, tags=()) -> tuple[str, list[str], list[str]]:
    """Ровно то, что уйдёт в DeviantArt: (название, теги, заметки об изменениях).

    Название не обрезаем молча — если длиннее 50 символов, это ошибка: пусть она сократит сама.
    Теги DeviantArt принимает только из латиницы, цифр и _: что поменялось или выпало — в заметках.
    """
    clean_title = (title or "").strip()
    if not clean_title:
        raise DAApiError("Нужно название работы.", "title_empty")
    if len(clean_title) > MAX_TITLE:
        raise DAApiError(f"Название длиннее {MAX_TITLE} символов ({len(clean_title)}) — DeviantArt его обрежет. "
                         "Попроси её сократить.", "title_too_long")
    clean_tags = _clean_tags(tags)
    notes: list[str] = []
    seen = {t.lower() for t in clean_tags}
    for t in tags or ():
        raw = str(t).strip()
        converted = re.sub(r"[^A-Za-z0-9_]", "", raw.replace(" ", "_"))
        if not converted:
            notes.append(f"тег «{raw}» не войдёт: DeviantArt принимает только латиницу, цифры и _")
        elif converted != raw and converted.lower() in seen:
            notes.append(f"тег «{raw}» станет {converted}")
    return clean_title, clean_tags, notes


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", name or "").strip().lower()


def _galleries(raw) -> list[dict] | None:
    """galleries из metadata (ext_gallery) → [{folder_id, name}]; None — DeviantArt их не прислал."""
    if not isinstance(raw, list):
        return None
    return [{"folder_id": str(g["folderid"]), "name": str(g.get("name") or "")}
            for g in raw if isinstance(g, dict) and g.get("folderid")]


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

    def _fresh_enough(self, record: dict) -> bool:
        return bool(record.get("access_token")) and float(record.get("expires_at", 0)) - EXPIRY_SKEW_S > self.clock()

    def _refresh(self, record: dict, rejected_access: str | None = None) -> dict:
        """Обновить токен под общим замком. Если другой процесс уже обновил — берём его результат."""
        with _refresh_lock(self.store.path):
            current = self.store.load()
            if not current:
                raise NotConnected("DeviantArt ещё не подключён: нужно один раз нажать «Разрешить» на сайте.")
            rotated = current.get("refresh_token") != record.get("refresh_token")
            new_access = rejected_access is not None and current.get("access_token") != rejected_access
            if rotated or new_access or (rejected_access is None and self._fresh_enough(current)):
                return current
            try:
                payload = _post_token(self.http, {"grant_type": "refresh_token", "client_id": self.client_id,
                                                  "refresh_token": current["refresh_token"]})
            except NotConnected:
                latest = self.store.load()
                # Удаляем только тот токен, который DeviantArt действительно отверг.
                if latest and latest.get("refresh_token") != current["refresh_token"]:
                    return latest
                self.store.clear()
                raise
            fresh = _token_record(payload, self.clock(), current)
            self.store.save(fresh)
            return fresh

    def _access_token(self, rejected: str | None = None) -> str:
        record = self.store.load()
        if not record:
            raise NotConnected("DeviantArt ещё не подключён: нужно один раз нажать «Разрешить» на сайте.")
        if rejected is not None or not self._fresh_enough(record):
            record = self._refresh(record, rejected_access=rejected)
        return record["access_token"]

    # запросы

    def _request(self, method: str, path: str, *, params=None, data=None, files=None,
                 retry_server_errors: bool = True) -> dict:
        url = f"{API_BASE}{path}"
        refreshed = False
        attempt = 0
        write = method.upper() != "GET"
        while True:
            # Чтение — в пределах общего бюджета и не во время паузы; запись по её команде не режем.
            block = da_access.before(da_access.API, write=write)
            if block:
                raise Throttled(block)
            token = self._access_token()
            try:
                resp = self.http.request(method, url, params=params, data=data, files=files, headers=_headers(token))
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # Соединения не было — запрос точно не дошёл.
                raise DAApiError(f"нет связи с DeviantArt: {type(e).__name__}", "network_unsent") from e
            except httpx.HTTPError as e:
                # Запрос мог дойти, а ответ потеряться.
                raise DAApiError(f"нет связи с DeviantArt: {type(e).__name__}", "network") from e
            limited = resp.status_code == 429 or (resp.status_code == 403 and not _api_error_body(resp))
            da_access.after(da_access.API, resp.status_code, resp.headers, limited=limited)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._access_token(rejected=token)
                continue
            # 429 — сразу наверх: вызывающий код прекращает проход, это и есть «притормозить».
            if resp.status_code == 429:
                raise RateLimited("DeviantArt просит притормозить (429). Попробуем позже.", 429, status=429)
            if limited:
                # 403 без ошибки API в теле — это не нехватка прав, а «не пускают» (как у RSS).
                raise RateLimited("DeviantArt не пускает к API (403). Попробуем позже.", 403, status=403)
            # 5xx повторяем только для чтения: повтор публикации мог бы выложить работу дважды.
            if resp.status_code >= 500 and retry_server_errors and attempt < MAX_TRIES - 1:
                self.sleep(2 ** attempt)
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise _error_from(resp)
            try:
                body = resp.json()
            except ValueError as e:
                raise DAApiError("DeviantArt вернул что-то странное.", "bad_response", status=resp.status_code) from e
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
                folders.append({"folder_id": f.get("folderid"), "name": f.get("name") or "", "parent": f.get("parent"),
                                # None — DeviantArt описание не прислал (а не «пустое»): переименовать, не стерев
                                # его, тогда нельзя.
                                "description": str(f.get("description") or "") if "description" in f else None})
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

        API отдаёт ответы максимум на 5 уровней вглубь, а ставки на аукционах часто идут
        цепочкой ответов. Поэтому комментарии, у которых есть ответы (replies > 0), но ни один
        ответ не пришёл, дочитываем отдельными запросами (commentid=…), в пределах DEEP_REPLY_REQUESTS.
        Если всё не поместилось в max_comments, оставляем самые НОВЫЕ (там последние ставки)
        и ставим self.comments_truncated = True.

        API берёт UUID работы, а не номер из ссылки: см. deviation_uuid_from_url.
        """
        path = f"/comments/deviation/{deviationid}"
        by_id: dict[str, Comment] = {}
        replies: dict[str, int] = {}
        answered: set[str] = set()  # у кого хоть один ответ пришёл (включая скрытые)
        self.comments_truncated = False

        def fetch(extra: dict) -> None:
            offset = 0
            while True:
                body = self._request("GET", path, params={"maxdepth": 5, "limit": 50, "offset": offset,
                                                          "expand": "comment.fulltext", **extra})
                for raw in body.get("thread") or []:
                    cid = str(raw.get("commentid") or "")
                    if cid:
                        replies[cid] = int(raw.get("replies") or 0)
                    if raw.get("parentid"):
                        answered.add(str(raw["parentid"]))
                    if raw.get("hidden") or not cid:
                        continue  # скрытые/удалённые автором — это не ставки
                    by_id.setdefault(cid, _comment_from_api(raw))
                if len(by_id) >= max_comments * 2:  # предохранитель от бесконечной ветки
                    self.comments_truncated = True
                    return
                if not body.get("has_more") or body.get("next_offset") in (None, offset):
                    return
                offset = int(body["next_offset"])

        fetch({})
        budget, visited = DEEP_REPLY_REQUESTS, set()
        while budget > 0:
            deep = [cid for cid in by_id if replies.get(cid, 0) > 0 and cid not in answered and cid not in visited]
            if not deep:
                break
            for cid in deep[:budget]:
                visited.add(cid)
                budget -= 1
                fetch({"commentid": cid})
        if budget <= 0:
            self.comments_truncated = True
        ordered = _with_depths(list(by_id.values()))
        if len(ordered) > max_comments:
            self.comments_truncated = True
            ordered = ordered[-max_comments:]
        return ordered

    def deviation_uuid_from_url(self, url: str, folders=(), max_pages: int = 10,
                                miss_cache: dict | None = None) -> str | None:
        """UUID работы по ссылке вида …/art/<slug>-<номер>.

        Документированного поиска по ссылке или номеру в API нет. Поэтому ищем работу
        в галерее автора (/gallery/all, свежие сверху), а если передали folders —
        ещё и в этих папках групп [(группа, folder_id)]. Порядок внутри папки задать
        нельзя (mode у /gallery/{folderid} работает только для «всех папок»), поэтому
        в папках находятся в основном свежие и популярные. Старые работы глубже
        max_pages*24 так не найти: тогда None.

        miss_cache — словарь {url: время промаха}, который хранит вызывающий: неудачный
        поиск не повторяем UUID_MISS_TTL_S, чтобы не тратить запросы каждый проход.
        """
        m = DEVIATION_URL.match(url.strip())
        if not m:
            raise DAApiError("Нужна ссылка на работу вида https://www.deviantart.com/автор/art/название-12345.",
                             "bad_url")
        key = url.strip()
        if miss_cache is not None and self.clock() - float(miss_cache.get(key, 0)) < UUID_MISS_TTL_S:
            return None
        author, number = m.group(1), m.group(2)
        found = self._scan_for(number, "/gallery/all", {"username": author}, max_pages)
        for group, folder_id in folders:
            found = found or self._scan_for(number, f"/gallery/{folder_id}", {"username": group}, max_pages)
        if miss_cache is not None:
            if found:
                miss_cache.pop(key, None)
            else:
                miss_cache[key] = self.clock()
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

    def deviation_metadata(self, uuids, ext_stats: bool = True) -> dict[str, dict]:
        """Просмотры, фавы, комментарии и теги работ: {uuid: {"views", "favourites", "comments", "downloads", "tags"}}.

        GET /deviation/metadata, scope browse. Максимум id за запрос документ не называет (есть только
        ошибка 0 «слишком много»), поэтому берём по METADATA_CHUNK и при ошибке 0 делим пачку пополам.
        RateLimited и NotConnected летят наверх — вызывающий прекращает проход.
        """
        ids = [str(u) for u in dict.fromkeys(uuids) if u]
        out: dict[str, dict] = {}
        chunk = METADATA_CHUNK
        i = 0
        while i < len(ids):
            part = ids[i:i + chunk]
            params: list[tuple[str, str]] = [("deviationids[]", u) for u in part]
            if ext_stats:
                params.append(("ext_stats", "true"))
            try:
                body = self._request("GET", "/deviation/metadata", params=params)
            except RateLimited:
                raise
            except DAApiError as e:
                if str(e.code) == "0" and chunk > 1:
                    chunk = max(1, chunk // 2)
                    continue
                raise
            for meta in body.get("metadata") or []:
                uuid = meta.get("deviationid")
                if not uuid:
                    continue
                stats = meta.get("stats") or {}
                out[str(uuid)] = {
                    "views": stats.get("views"),
                    "favourites": stats.get("favourites"),
                    "comments": stats.get("comments"),
                    "downloads": stats.get("downloads"),
                    "tags": [t.get("tag_name") for t in meta.get("tags") or [] if isinstance(t, dict) and t.get("tag_name")],
                }
            i += len(part)
        return out

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
        clean_title, clean, _ = normalize_publish_fields(title or path.stem, tags)
        data: dict = {"title": clean_title, "artist_comments": artist_comments}
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
                      feature: bool = True, mature_level: str | None = None, mature_classification=()) -> dict:
        """Опубликовать работу из Sta.sh (сразу в группы, если переданы).

        Никогда не повторяет запрос. Если ответа нет (обрыв, таймаут, 5xx) — UncertainPublish:
        работа могла выложиться, проверять надо её галерею.
        """
        if is_mature and mature_level not in MATURE_LEVELS:
            raise DAApiError("Для работы 18+ DeviantArt требует уровень: strict или moderate — пусть она выберет.",
                             "mature_level_required")
        bad = [c for c in mature_classification if c not in MATURE_CLASSES]
        if bad:
            raise DAApiError(f"Неизвестная пометка 18+: {', '.join(bad)}. Можно: {', '.join(MATURE_CLASSES)}.",
                             "bad_mature_classification")
        pairs = [(str(g), str(f)) for g, f in groups]
        data: dict = {
            "itemid": str(itemid),
            "is_mature": _bool(is_mature),
            "is_ai_generated": _bool(is_ai_generated),
            "noai": _bool(noai),
            "allow_comments": _bool(allow_comments),
            "feature": _bool(feature),
        }
        if is_mature:
            data["mature_level"] = mature_level
            if mature_classification:
                data["mature_classification[]"] = list(mature_classification)
        if galleryids:
            data["galleryids[]"] = [str(g) for g in galleryids]
        clean = _clean_tags(tags)
        if clean:
            data["tags[]"] = clean
        if pairs:
            # Порядок важен: i-я папка относится к i-й группе.
            data["groups[]"] = [g for g, _ in pairs]
            data["group_folders[]"] = [f for _, f in pairs]
        try:
            body = self._publish(data)
        except DAApiError as e:
            if str(e.code) not in ("0", "1"):
                raise
            # Ошибка 0/1 значит «не опубликовано, нужно согласие»: повторить с согласием безопасно.
            # В текущей спецификации этих полей нет, поэтому шлём их только в ответ на такую ошибку.
            body = self._publish({**data, "agree_submission": "true", "agree_tos": "true"})
        return {"url": body.get("url"), "deviationid": body.get("deviationid")}

    def _publish(self, data: dict) -> dict:
        try:
            return self._request("POST", "/stash/publish", data=data, retry_server_errors=False)
        except NotConnected:
            raise
        except DAApiError as e:
            if e.code in LOST_RESPONSE or (e.status or 0) >= 500:
                raise UncertainPublish("DeviantArt не ответил на публикацию — работа могла выложиться. "
                                       "Проверь свою галерею, прежде чем пробовать ещё раз.", e.code, e.description,
                                       e.status) from e
            code = e.code
            if isinstance(code, str) and code.isdigit():
                code = int(code)
            if isinstance(code, int) and code in PUBLISH_ERRORS:
                raise DAApiError(f"DeviantArt не опубликовал: {PUBLISH_ERRORS[code]}.", e.code, e.description,
                                 e.status) from e
            raise

    # права подключения

    def granted_scopes(self) -> set[str] | None:
        """Права, которые DeviantArt выдал этому подключению; None — неизвестно (DeviantArt их не назвал)."""
        record = self.store.load()
        if not record:
            raise NotConnected("DeviantArt ещё не подключён: нужно один раз нажать «Разрешить» на сайте.")
        raw = str(record.get("scope") or "").strip()
        return set(re.split(r"[\s,]+", raw)) if raw else None

    def require_scopes(self, *needed: str) -> None:
        """Проверка до запроса: подключение старое, а новых прав в нём нет → NeedsReconnect."""
        granted = self.granted_scopes()
        if granted is None:
            return  # не знаем — решит сам DeviantArt (его отказ тоже станет NeedsReconnect)
        missing = [s for s in needed if s not in granted]
        if missing:
            raise NeedsReconnect(RECONNECT, "missing_scope", " ".join(missing))

    def _write(self, path: str, data: dict, scopes: tuple[str, ...], what: str) -> dict:
        """Запрос, который что-то меняет у неё на DeviantArt. Никогда не повторяется.

        Нет ответа (обрыв, таймаут, 5xx, нечитаемый ответ) — UncertainPublish: изменение могло пройти.
        """
        self.require_scopes(*scopes)
        try:
            return self._request("POST", path, data=data, retry_server_errors=False)
        except (NotConnected, RateLimited):
            raise
        except DAApiError as e:
            if e.code in LOST_RESPONSE or (e.status or 0) >= 500:
                raise UncertainPublish(f"DeviantArt не ответил ({what}) — изменение могло пройти. "
                                       "Проверь на сайте, прежде чем повторять.", e.code, e.description,
                                       e.status) from e
            raise

    # её работы: чтение для правки

    def _metadata(self, uuids, *, gallery: bool = True) -> dict[str, dict]:
        ids = [str(u) for u in dict.fromkeys(uuids) if u]
        out: dict[str, dict] = {}
        for i in range(0, len(ids), METADATA_CHUNK):
            params: list[tuple[str, str]] = [("deviationids[]", u) for u in ids[i:i + METADATA_CHUNK]]
            params += [("ext_submission", "true")]
            if gallery:
                params.append(("ext_gallery", "true"))
            body = self._request("GET", "/deviation/metadata", params=params)
            for meta in body.get("metadata") or []:
                if isinstance(meta, dict) and meta.get("deviationid"):
                    out[str(meta["deviationid"])] = meta
        return out

    def deviations_brief(self, uuids) -> dict[str, dict]:
        """Название, автор и папки её работ: {uuid: {"title", "author", "galleries": [{folder_id, name}]}}."""
        out = {}
        for uuid, meta in self._metadata(uuids).items():
            out[uuid] = {"title": str(meta.get("title") or ""),
                         "author": str((meta.get("author") or {}).get("username") or ""),
                         "galleries": _galleries(meta.get("galleries")) or []}
        return out

    def gallery_all(self, username: str, max_pages: int = 10) -> list[dict]:
        """Её работы из «Всех» (свежие сверху): [{deviationid, title, url}]."""
        out: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            body = self._request("GET", "/gallery/all", params={"username": username, "limit": 24, "offset": offset})
            for dev in body.get("results") or []:
                if isinstance(dev, dict) and dev.get("deviationid"):
                    out.append({"deviationid": str(dev["deviationid"]), "title": str(dev.get("title") or ""),
                                "url": str(dev.get("url") or "")})
            if not body.get("has_more") or body.get("next_offset") in (None, offset):
                break
            offset = int(body["next_offset"])
        return out

    def edit_state(self, deviationid: str) -> dict:
        """Всё, что deviation/edit может стереть, если не прислать заново, — текущие значения работы.

        Если что-то из этого DeviantArt не показал, бросает DAApiError(code="edit_unsafe"): править
        вслепую нельзя. Не читаются через API вообще: размер показа и водяной знак DeviantArt,
        настройки лицензии CC, метки ИИ, теги-субъекты и место. Работу с лицензией CC не трогаем;
        метки ИИ отправляем её настройками; про остальное честно говорит превью.
        """
        meta = self._metadata([deviationid]).get(str(deviationid))
        if not meta:
            raise DAApiError("DeviantArt не показал эту работу — править её вслепую не буду.", "edit_unsafe")
        dev = self._request("GET", f"/deviation/{deviationid}")
        title = meta.get("title")
        galleries = _galleries(meta.get("galleries"))
        problems = []
        if not isinstance(title, str) or not title.strip():
            problems.append("название")
        if not isinstance(meta.get("is_mature"), bool):
            problems.append("пометку 18+")
        if meta.get("is_mature") is True and meta.get("mature_level") not in MATURE_LEVELS:
            problems.append("уровень 18+")
        if not isinstance(meta.get("allows_comments"), bool):
            problems.append("разрешены ли комментарии")
        if not isinstance(meta.get("tags"), list):
            problems.append("теги")
        if galleries is None:
            problems.append("папки галереи")
        raw_classes = meta.get("mature_classification")
        if meta.get("is_mature") is True and (not isinstance(raw_classes, list)
                                              or any(c not in MATURE_CLASSES for c in raw_classes)):
            problems.append("пометки 18+ (что именно в работе)")
        if not isinstance(meta.get("license"), str):
            problems.append("лицензию")
        if not isinstance(dev.get("is_downloadable"), bool):
            problems.append("разрешено ли скачивание")
        name = title if isinstance(title, str) and title.strip() else deviationid
        if problems:
            raise DAApiError(f"Не трогаю «{name}»: DeviantArt не показал {', '.join(problems)} — после правки "
                             "это могло бы сброситься.", "edit_unsafe")
        license_text = meta["license"]
        if CC_LICENSE.search(license_text):
            raise DAApiError(f"Не трогаю «{name}»: у работы лицензия «{license_text}», а её настройки API не "
                             "показывает — правка могла бы её сбросить. Название этой работы лучше поменять на сайте.",
                             "edit_unsafe")
        tags = [str(t["tag_name"]) for t in meta["tags"] if isinstance(t, dict) and t.get("tag_name")]
        return {
            "deviationid": str(deviationid),
            "title": str(title),
            "is_mature": bool(meta["is_mature"]),
            "mature_level": meta.get("mature_level") if meta["is_mature"] else None,
            "mature_classification": list(raw_classes or []) if meta["is_mature"] else [],
            "allow_comments": bool(meta["allows_comments"]),
            "tags": tags,
            "galleries": galleries or [],
            "allow_free_download": bool(dev["is_downloadable"]),
            "license": license_text,
            "can_post_comment": meta.get("can_post_comment") is not False,
            "author": str((meta.get("author") or {}).get("username") or ""),
            "url": str(dev.get("url") or ""),
        }

    # её работы: правка

    def edit_deviation(self, deviationid: str, state: dict, *, title: str | None = None, tags=None,
                       noai: bool = True, is_ai_generated: bool = False) -> dict:
        """Поменять название (и теги, если переданы), отправив заново всё, что читается (state из edit_state).

        Описание через API не меняется вообще (такого поля у deviation/edit нет). Никогда не повторяет
        запрос: нет ответа — UncertainPublish.
        """
        clean_title, _, _ = normalize_publish_fields(state["title"] if title is None else title)
        # Её нынешние теги отправляем как есть: «чистка» могла бы выкинуть то, что DeviantArt уже принял.
        send_tags = list(state["tags"]) if tags is None else _clean_tags(tags)
        data: dict = {
            "title": clean_title,
            "is_mature": _bool(state["is_mature"]),
            "allow_comments": _bool(state["allow_comments"]),
            "allow_free_download": _bool(state["allow_free_download"]),
            "noai": _bool(noai),
            "is_ai_generated": _bool(is_ai_generated),
        }
        if state["is_mature"]:
            data["mature_level"] = state["mature_level"]
            if state.get("mature_classification"):
                data["mature_classification[]"] = list(state["mature_classification"])
        if send_tags:
            data["tags[]"] = send_tags
        if state["galleries"]:
            data["galleryids[]"] = [str(g["folder_id"]) for g in state["galleries"]]
        body = self._write(f"/deviation/edit/{deviationid}", data, EDIT_SCOPES, "правка работы")
        return {"url": body.get("url"), "deviationid": body.get("deviationid") or deviationid}

    def verify_edit(self, deviationid: str, before: dict, title: str, tags=None) -> list[str]:
        """Перечитать работу после правки: что пропало или поменялось само (пусто — всё на месте)."""
        meta = self._metadata([deviationid]).get(str(deviationid))
        if not meta:
            raise DAApiError("DeviantArt не показал работу после правки.", "verify_failed")
        lost: list[str] = []
        if str(meta.get("title") or "") != title:
            lost.append(f"название стало «{meta.get('title')}»")
        want_tags = {t.casefold() for t in (before["tags"] if tags is None else _clean_tags(tags))}
        have_tags = {str(t.get("tag_name") or "").casefold() for t in meta.get("tags") or [] if isinstance(t, dict)}
        if want_tags - have_tags:
            lost.append(f"пропали теги: {', '.join(sorted(want_tags - have_tags))}")
        have_folders = {g["folder_id"] for g in _galleries(meta.get("galleries")) or []}
        gone = [g["name"] or g["folder_id"] for g in before["galleries"] if g["folder_id"] not in have_folders]
        if gone:
            lost.append(f"работа выпала из папок: {', '.join(gone)}")
        if meta.get("is_mature") is not None and bool(meta.get("is_mature")) != before["is_mature"]:
            lost.append("поменялась пометка 18+")
        elif before["is_mature"]:
            if meta.get("mature_level") != before["mature_level"]:
                lost.append(f"уровень 18+ стал «{meta.get('mature_level')}»")
            have_classes = meta.get("mature_classification")
            if not isinstance(have_classes, list) or set(have_classes) != set(before["mature_classification"]):
                lost.append("поменялись пометки 18+ (что именно в работе)")
        if meta.get("allows_comments") is not None and bool(meta.get("allows_comments")) != before["allow_comments"]:
            lost.append("поменялось, разрешены ли комментарии")
        return lost

    # её папки галереи

    def create_folder(self, name: str, *, username: str, parent_folderid: str | None = None,
                      description: str = "") -> dict:
        """Создать папку галереи → {folder_id, name}.

        Не идемпотентно: если ответ потерялся, перечитываем папки (дважды, с паузой: DeviantArt мог
        ещё создавать её) и ищем её по названию — второй такой же не создаём. Не нашли или не смогли
        перечитать — UncertainPublish: она могла появиться позже, повторять вслепую нельзя.
        """
        clean = " ".join((name or "").split())
        if not clean:
            raise DAApiError("Нужно название папки.", "folder_name_empty")
        if len(clean) > FOLDER_NAME_MAX:
            raise DAApiError(f"Название папки длиннее {FOLDER_NAME_MAX} символов.", "folder_name_too_long")
        data: dict = {"folder": clean}
        if description:
            data["description"] = description
        if parent_folderid:
            data["parent_folderid"] = parent_folderid
        try:
            body = self._write("/gallery/folders/create", data, GALLERY_SCOPES, "создание папки")
        except UncertainPublish:
            want = clean.casefold()  # то же сравнение, что в превью: регистр и пробелы, но не «-» и «_»
            for attempt in range(2):
                if attempt:
                    self.sleep(FOLDER_RECHECK_S)
                try:
                    existing = self.gallery_folders(username)
                except DAApiError as e:
                    raise UncertainPublish("DeviantArt не ответил на создание папки, а перечитать папки не вышло — "
                                           "папка могла создаться. Проверь галерею.") from e
                found = next((f for f in existing if " ".join(f["name"].split()).casefold() == want), None)
                if found:
                    return {"folder_id": found["folder_id"], "name": found["name"]}
            raise UncertainPublish(f"DeviantArt не ответил на создание папки «{clean}», и в галерее её пока нет — "
                                   "но она могла появиться позже. Проверь галерею, прежде чем повторять.",
                                   "folder_maybe_created") from None
        if not body.get("folderid"):
            raise DAApiError("DeviantArt не вернул номер новой папки.", "folder_not_created")
        return {"folder_id": str(body["folderid"]), "name": str(body.get("name") or clean)}

    def rename_folder(self, folderid: str, name: str, description: str = "") -> None:
        """Переименовать папку. Описание отправляем нынешнее: без него оно, возможно, сотрётся."""
        clean = " ".join((name or "").split())
        if not clean or len(clean) > FOLDER_NAME_MAX:
            raise DAApiError(f"Название папки — от 1 до {FOLDER_NAME_MAX} символов.", "folder_name_bad")
        body = self._write("/gallery/folders/update",
                           {"folderid": folderid, "name": clean, "description": description},
                           GALLERY_SCOPES, "переименование папки")
        if body.get("success") is False:
            raise DAApiError("DeviantArt не переименовал папку.", "folder_op_failed")

    def _folder_batches(self, path: str, base: dict, deviationids, what: str, on_chunk=None) -> None:
        """Пачками по GALLERY_CHUNK. on_chunk(n) — после каждой принятой пачки: если упадёт следующая,
        вызывающий знает, сколько работ уже перенесено."""
        ids = [str(d) for d in dict.fromkeys(deviationids) if d]
        for i in range(0, len(ids), GALLERY_CHUNK):
            if i:
                self.sleep(1.5)
            chunk = ids[i:i + GALLERY_CHUNK]
            body = self._write(path, {**base, "deviationids[]": chunk}, GALLERY_SCOPES, what)
            if body.get("success") is False:
                raise DAApiError(f"DeviantArt не выполнил: {what}.", "folder_op_failed")
            if on_chunk:
                on_chunk(len(chunk))

    def copy_deviations(self, target_folderid: str, deviationids, on_chunk=None) -> None:
        """Добавить работы в папку (из других папок они не пропадают)."""
        self._folder_batches("/gallery/folders/copy_deviations", {"target_folderid": target_folderid},
                             deviationids, "добавление работ в папку", on_chunk)

    def move_deviations(self, source_folderid: str, target_folderid: str, deviationids, on_chunk=None) -> None:
        """Переложить работы из одной папки в другую."""
        self._folder_batches("/gallery/folders/move_deviations",
                             {"source_folderid": source_folderid, "target_folderid": target_folderid},
                             deviationids, "перенос работ между папками", on_chunk)

    def remove_deviations(self, folderid: str, deviationids, on_chunk=None) -> None:
        """Убрать работы из папки (в «Всех» они остаются)."""
        self._folder_batches("/gallery/folders/remove_deviations", {"folderid": folderid}, deviationids,
                             "удаление работ из папки", on_chunk)

    # комментарий под её работой

    def post_comment(self, deviationid: str, body: str, reply_to: str | None = None) -> dict:
        """Комментарий под работой (её текст как есть). Не идемпотентно: нет ответа — UncertainPublish."""
        data = {"body": body}
        if reply_to:
            data["commentid"] = reply_to
        res = self._write(f"/comments/post/deviation/{deviationid}", data, COMMENT_SCOPES, "комментарий")
        return {"commentid": res.get("commentid")}

    def revoke(self) -> str:
        """Отключить DeviantArt. Локальный токен удаляется в любом случае.

        Возвращает: "revoked" — DeviantArt подтвердил отзыв; "local_only" — удалили только у неё
        на маке (DeviantArt не подтвердил: стоит отключить приложение в настройках DeviantArt,
        Connected Apps); "not_connected" — подключения и не было.
        """
        record = self.store.load()
        self.store.clear()
        if not record:
            return "not_connected"
        try:
            resp = self.http.post(REVOKE_URL, data={"token": record.get("refresh_token"),
                                                    "revoke_refresh_only": "false"}, headers=_headers())
        except httpx.HTTPError:
            return "local_only"
        try:
            ok = resp.status_code < 300 and bool((resp.json() or {}).get("success"))
        except (ValueError, AttributeError):
            ok = False
        return "revoked" if ok else "local_only"


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

ROOT_NAMES = {"Downloads": "Загрузки", "Desktop": "Рабочий стол", "Pictures": "Изображения"}
MAX_SCAN_PER_ROOT = 500


def recent_images_report(folders=None, limit: int = 10, now: float | None = None, deep: bool = False) -> dict:
    """Свежие картинки и папки, к которым macOS не дала доступ.

    По умолчанию смотрим только верхний уровень Загрузок, Рабочего стола и Изображений:
    первое обращение к каждой из этих папок macOS может один раз спросить разрешение,
    заходить глубже (в чужие папки, ссылки на внешние диски) незачем.
    Возвращает {"items": [...], "denied": ["Рабочий стол", ...]}.
    """
    now = now or time.time()
    home = Path.home()
    roots = [Path(f).expanduser() for f in folders] if folders else [home / "Downloads", home / "Desktop",
                                                                     home / "Pictures"]
    found: dict[Path, os.stat_result] = {}
    denied: list[str] = []

    def entries(d: Path) -> list[Path]:
        out = []
        with os.scandir(d) as it:
            for i, e in enumerate(it):
                if i >= MAX_SCAN_PER_ROOT:
                    break
                if e.name.startswith(".") or e.is_symlink():
                    continue
                out.append(Path(e.path))
        return out

    for root in roots:
        try:
            level1 = entries(root)
        except PermissionError:
            denied.append(ROOT_NAMES.get(root.name, root.name))
            continue
        except OSError:
            continue
        candidates = list(level1)
        if deep:
            for d in level1:
                if d.is_dir() and not d.name.endswith((".app", ".photoslibrary", ".bundle")):
                    with contextlib.suppress(OSError):
                        candidates += entries(d)
        for p in candidates:
            if p.suffix.lower() in IMAGE_EXTS:
                with contextlib.suppress(OSError):
                    st = p.stat()
                    if os.path.isfile(p):
                        found[p] = st
    ordered = sorted(found.items(), key=lambda kv: kv[1].st_mtime, reverse=True)[:limit]
    items = [{"path": str(p), "name": p.name, "modified": st.st_mtime, "size": st.st_size,
              "age_s": max(0.0, now - st.st_mtime)} for p, st in ordered]
    return {"items": items, "denied": denied}


def recent_images(folders=None, limit: int = 10, now: float | None = None, deep: bool = False) -> list[dict]:
    """Свежие картинки (только список; про отказ macOS в доступе — recent_images_report)."""
    return recent_images_report(folders, limit, now, deep)["items"]
