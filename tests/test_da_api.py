import base64
import hashlib
import json
import os
import socket
import stat
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from mcp_devian import da_api
from mcp_devian.da_api import DAApiError, DAClient, NotConnected, RateLimited, TokenStore

NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "da-token.json")


def connected(store, expires_at=NOW + 3600):
    store.save({"access_token": "AT", "refresh_token": "RT", "expires_at": expires_at})
    return store


class Router:
    """MockTransport: маршруты по (method, path) → функция(request) -> Response. Пишет все запросы."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        key = (request.method, request.url.path)
        handler = self.routes.get(key)
        if handler is None:
            return httpx.Response(404, json={"error": "not_found"})
        return handler(request)


def client(store, routes, sleeps=None, clock=lambda: NOW):
    router = Router(routes)
    http = httpx.Client(transport=httpx.MockTransport(router))
    c = DAClient("CID", store, http=http, sleep=(sleeps.append if sleeps is not None else lambda s: None),
                 clock=clock)
    return c, router


def form(request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode(), keep_blank_values=True)


# --- PKCE и URL ---

def test_pkce_pair_is_s256():
    verifier, challenge = da_api.pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and "=" not in challenge


def test_authorize_url_params():
    url = da_api.authorize_url("CID", "ST", "CH")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://www.deviantart.com/oauth2/authorize?")
    assert q == {"response_type": "code", "client_id": "CID", "redirect_uri": "http://localhost:8765/callback",
                 "scope": "basic user browse stash publish gallery comment.post", "state": "ST", "code_challenge": "CH",
                 "code_challenge_method": "S256"}


# --- Вход ---

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def oauth_routes(seen):
    def token(request):
        seen.append(form(request))
        return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})

    def whoami(request):
        assert request.headers["Authorization"] == "Bearer AT1"
        return httpx.Response(200, json={"username": "MothArt", "userid": "U-1"})

    return {("POST", "/oauth2/token"): token, ("GET", "/api/v1/oauth2/user/whoami"): whoami}


def browser_that(callback_query):
    """Фальшивый браузер: сразу «возвращается» на redirect_uri с нужными параметрами."""
    pages = []

    def opener(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        redirect = q["redirect_uri"].replace("localhost", "127.0.0.1")
        params = callback_query(q)
        with urllib.request.urlopen(f"{redirect}?{params}", timeout=5) as resp:
            pages.append(resp.read().decode())

    return opener, pages


def test_oauth_flow_end_to_end(store):
    seen = []
    http = httpx.Client(transport=httpx.MockTransport(Router(oauth_routes(seen))))
    opener, pages = browser_that(lambda q: f"code=CODE&state={q['state']}")
    port = free_port()
    result = da_api.run_oauth_flow("CID", store, port=port, timeout_s=10, opener=opener, http=http)
    assert result == {"username": "MothArt"}
    sent = seen[0]
    assert sent["grant_type"] == ["authorization_code"] and sent["code"] == ["CODE"]
    assert sent["client_id"] == ["CID"] and sent["redirect_uri"] == [f"http://localhost:{port}/callback"]
    assert "client_secret" not in sent and len(sent["code_verifier"][0]) >= 43
    saved = store.load()
    assert saved["refresh_token"] == "RT1" and saved["username"] == "MothArt"
    wait_for(lambda: pages)
    assert "можно закрыть вкладку" in pages[0]


def wait_for(cond, timeout=3.0):
    """Поддельный браузер дописывает страницу в своём потоке — ждём его."""
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)


def test_oauth_wrong_state_ignored_then_real_callback_works(store):
    """Чужой запрос со state=WRONG не обрывает вход: ждём настоящий ответ (#55)."""
    http = httpx.Client(transport=httpx.MockTransport(Router(oauth_routes([]))))
    pages = []

    def opener(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        base = q["redirect_uri"].replace("localhost", "127.0.0.1")
        for params in ("code=EVIL&state=WRONG", f"code=CODE&state={q['state']}"):
            with urllib.request.urlopen(f"{base}?{params}", timeout=5) as resp:
                pages.append(resp.read().decode())

    result = da_api.run_oauth_flow("CID", store, port=free_port(), timeout_s=10, opener=opener, http=http)
    assert result == {"username": "MothArt"} and store.load()["refresh_token"] == "RT1"
    wait_for(lambda: len(pages) == 2)
    assert "не тот ответ" in pages[0]


def test_oauth_idle_connection_does_not_hang(store):
    """Браузер открыл соединение и молчит: вход всё равно дожидается ответа и завершается (#12)."""
    http = httpx.Client(transport=httpx.MockTransport(Router(oauth_routes([]))))
    port = free_port()
    idle = []

    def opener(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        idle.append(socket.create_connection(("127.0.0.1", port)))  # молчащее «запасное» соединение
        time.sleep(0.2)
        base = q["redirect_uri"].replace("localhost", "127.0.0.1")
        with urllib.request.urlopen(f"{base}?code=CODE&state={q['state']}", timeout=15):
            pass

    t0 = time.time()
    result = da_api.run_oauth_flow("CID", store, port=port, timeout_s=20, opener=opener, http=http)
    assert result == {"username": "MothArt"} and time.time() - t0 < 8
    for sock in idle:
        sock.close()


def test_oauth_deadline_honoured_with_idle_connection(store):
    http = httpx.Client(transport=httpx.MockTransport(Router({})))
    port = free_port()
    idle = []
    opener = lambda url: idle.append(socket.create_connection(("127.0.0.1", port)))  # noqa: E731
    t0 = time.time()
    with pytest.raises(DAApiError, match="время вышло"):
        da_api.run_oauth_flow("CID", store, port=port, timeout_s=1, opener=opener, http=http)
    assert time.time() - t0 < 6  # не дольше таймаута соединения (2 с) + запас
    for sock in idle:
        sock.close()
    # Порт освобождён: повторный вход (сервер тоже ставит SO_REUSEADDR) может слушать его сразу.
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen()


def test_oauth_whoami_failure_is_not_fatal(store):
    """whoami не ответил (не про права) — подключение всё равно сохранено, имя неизвестно (#9)."""
    routes = oauth_routes([])
    routes[("GET", "/api/v1/oauth2/user/whoami")] = lambda r: httpx.Response(500)
    http = httpx.Client(transport=httpx.MockTransport(Router(routes)))
    opener, _ = browser_that(lambda q: f"code=CODE&state={q['state']}")
    result = da_api.run_oauth_flow("CID", store, port=free_port(), timeout_s=10, opener=opener, http=http)
    assert result == {"username": ""} and store.load()["refresh_token"] == "RT1"


def test_oauth_missing_scope_clears_token(store):
    routes = oauth_routes([])
    routes[("GET", "/api/v1/oauth2/user/whoami")] = lambda r: httpx.Response(
        403, json={"error": "insufficient_scope", "error_description": "scope user required"})
    http = httpx.Client(transport=httpx.MockTransport(Router(routes)))
    opener, _ = browser_that(lambda q: f"code=CODE&state={q['state']}")
    with pytest.raises(NotConnected, match="Разрешить"):
        da_api.run_oauth_flow("CID", store, port=free_port(), timeout_s=10, opener=opener, http=http)
    assert store.load() is None


def test_oauth_denied(store):
    http = httpx.Client(transport=httpx.MockTransport(Router(oauth_routes([]))))
    opener, _ = browser_that(lambda q: f"error=access_denied&state={q['state']}")
    with pytest.raises(DAApiError, match="не разрешён"):
        da_api.run_oauth_flow("CID", store, port=free_port(), timeout_s=10, opener=opener, http=http)


def test_oauth_timeout(store):
    http = httpx.Client(transport=httpx.MockTransport(Router({})))
    with pytest.raises(DAApiError, match="время вышло"):
        da_api.run_oauth_flow("CID", store, port=free_port(), timeout_s=1, opener=lambda url: None, http=http)


# --- Токены ---

def test_token_file_is_private(store):
    connected(store)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    store.clear()
    assert store.load() is None


def test_not_connected(store):
    c, _ = client(store, {})
    assert not c.is_connected()
    with pytest.raises(NotConnected):
        c.whoami()


def test_refresh_and_rotation(store):
    connected(store, expires_at=NOW + 30)  # истекает в пределах запаса 60 с
    seen = []

    def token(request):
        seen.append(form(request))
        return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600})

    def whoami(request):
        assert request.headers["Authorization"] == "Bearer AT2"
        return httpx.Response(200, json={"username": "MothArt", "userid": "U-1"})

    c, _ = client(store, {("POST", "/oauth2/token"): token, ("GET", "/api/v1/oauth2/user/whoami"): whoami})
    assert c.whoami()["username"] == "MothArt"
    assert seen[0]["grant_type"] == ["refresh_token"] and seen[0]["refresh_token"] == ["RT"]
    saved = store.load()
    assert saved["refresh_token"] == "RT2" and saved["expires_at"] == NOW + 3600


def test_refresh_keeps_old_refresh_token_when_not_rotated(store):
    connected(store, expires_at=NOW - 10)
    token = lambda r: httpx.Response(200, json={"access_token": "AT2", "expires_in": 3600})  # noqa: E731
    who = lambda r: httpx.Response(200, json={"username": "x"})  # noqa: E731
    c, _ = client(store, {("POST", "/oauth2/token"): token, ("GET", "/api/v1/oauth2/user/whoami"): who})
    c.whoami()
    assert store.load()["refresh_token"] == "RT"


def test_revoked_refresh_clears_store(store):
    connected(store, expires_at=NOW - 10)
    token = lambda r: httpx.Response(401, json={"error": "invalid_grant", "error_description": "revoked"})  # noqa: E731
    c, _ = client(store, {("POST", "/oauth2/token"): token})
    with pytest.raises(NotConnected, match="заново"):
        c.whoami()
    assert store.load() is None


def test_401_triggers_one_refresh(store):
    connected(store)
    calls = {"who": 0}

    def who(request):
        calls["who"] += 1
        if request.headers["Authorization"] == "Bearer AT":
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json={"username": "x"})

    token = lambda r: httpx.Response(200, json={"access_token": "AT9", "expires_in": 3600})  # noqa: E731
    c, _ = client(store, {("POST", "/oauth2/token"): token, ("GET", "/api/v1/oauth2/user/whoami"): who})
    assert c.whoami()["username"] == "x" and calls["who"] == 2


# --- Запросы, повторы, ошибки ---

def test_headers(store):
    connected(store)
    c, router = client(store, {("GET", "/api/v1/oauth2/user/whoami"): lambda r: httpx.Response(200, json={})})
    c.whoami()
    h = router.calls[0].headers
    assert h["dA-minor-version"] == "20240701" and h["User-Agent"].startswith("mcp_devian/")
    assert "gzip" in h["Accept-Encoding"] and h["Authorization"] == "Bearer AT"


def test_429_raises_immediately(store):
    """После 429 не долбим DeviantArt повторами: сразу наверх, вызывающий прекращает проход (#18)."""
    connected(store)
    sleeps = []
    c, router = client(store, {("GET", "/api/v1/oauth2/user/whoami"): lambda r: httpx.Response(429)}, sleeps=sleeps)
    with pytest.raises(RateLimited) as e:
        c.whoami()
    assert sleeps == [] and len(router.calls) == 1 and e.value.status == 429


def test_4xx_not_retried_and_parsed(store):
    connected(store)
    err = {"error": "invalid_request", "error_description": "Folder is closed", "error_code": 3}
    sleeps = []
    c, router = client(store, {("GET", "/api/v1/oauth2/user/profile/somegroup"):
                               lambda r: httpx.Response(400, json=err)}, sleeps=sleeps)
    with pytest.raises(DAApiError) as e:
        c.user_profile("somegroup")
    assert e.value.code == 3 and e.value.description == "Folder is closed" and "Folder is closed" in str(e.value)
    assert sleeps == [] and len(router.calls) == 1


def test_5xx_retried_for_reads_not_for_publish(store):
    connected(store)
    sleeps = []
    c, router = client(store, {("GET", "/api/v1/oauth2/user/whoami"): lambda r: httpx.Response(503),
                               ("POST", "/api/v1/oauth2/stash/publish"): lambda r: httpx.Response(500)},
                       sleeps=sleeps)
    with pytest.raises(DAApiError):
        c.whoami()
    assert len(router.calls) == 4
    with pytest.raises(da_api.UncertainPublish, match="могла выложиться"):
        c.stash_publish(1, is_mature=False)
    assert len(router.calls) == 5  # публикация не повторяется: иначе можно выложить дважды


def test_publish_network_errors(store):
    """Обрыв после отправки — «не знаю, выложилось ли»; не удалось соединиться — обычная ошибка (#13)."""
    connected(store)

    def read_timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    c, router = client(store, {("POST", "/api/v1/oauth2/stash/publish"): read_timeout})
    with pytest.raises(da_api.UncertainPublish):
        c.stash_publish(1, is_mature=False)
    assert len(router.calls) == 1

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    c, _ = client(store, {("POST", "/api/v1/oauth2/stash/publish"): refused})
    with pytest.raises(DAApiError) as e:
        c.stash_publish(1, is_mature=False)
    assert not isinstance(e.value, da_api.UncertainPublish) and e.value.code == "network_unsent"


def test_publish_agreement_retry_on_error_0_or_1(store):
    """Ошибки 0/1 значат «не опубликовано, нужно согласие»: повторяем один раз с согласием (#11)."""
    connected(store)
    seen = []

    def publish(request):
        seen.append(form(request))
        if len(seen) == 1:
            return httpx.Response(400, json={"error": "invalid_request", "error_description": "Must accept",
                                             "error_code": 0})
        return httpx.Response(200, json={"status": "success", "url": "u", "deviationid": "D"})

    c, _ = client(store, {("POST", "/api/v1/oauth2/stash/publish"): publish})
    assert c.stash_publish(1, is_mature=False)["deviationid"] == "D"
    assert "agree_submission" not in seen[0]
    assert seen[1]["agree_submission"] == ["true"] and seen[1]["agree_tos"] == ["true"]


def test_publish_error_codes_translated(store):
    connected(store)
    c, router = client(store, {("POST", "/api/v1/oauth2/stash/publish"): lambda r: httpx.Response(
        400, json={"error": "invalid_request", "error_description": "already", "error_code": 9})})
    with pytest.raises(DAApiError, match="уже опубликована"):
        c.stash_publish(1, is_mature=False)
    assert len(router.calls) == 1


def test_status_error_in_200_body(store):
    connected(store)
    c, _ = client(store, {("GET", "/api/v1/oauth2/user/whoami"):
                          lambda r: httpx.Response(200, json={"status": "error", "error_description": "nope"})})
    with pytest.raises(DAApiError, match="nope"):
        c.whoami()


# --- Группы и папки ---

def group_routes():
    pages = {
        "0": {"results": [{"folderid": "F-1", "name": "Featured"},
                          {"folderid": "F-2", "name": "Points Adopts",
                           "subfolders": [{"folderid": "F-3", "name": "Auctions - Open", "parent": "F-2"}]}],
              "has_more": True, "next_offset": 2},
        "2": {"results": [{"folderid": "F-4", "name": "OTA offer to adopt"}], "has_more": False, "next_offset": None},
    }

    def folders(request):
        assert request.url.params["username"] == "da-adopts" and request.url.params["limit"] == "50"
        return httpx.Response(200, json=pages[request.url.params["offset"]])

    def profile(request):
        return httpx.Response(200, json={"user": {"userid": "G-UUID", "username": "da-adopts", "type": "group"}})

    return {("GET", "/api/v1/oauth2/gallery/folders"): folders,
            ("GET", "/api/v1/oauth2/user/profile/da-adopts"): profile}


def test_gallery_folders_paginates_and_flattens(store):
    connected(store)
    c, _ = client(store, group_routes())
    names = [f["name"] for f in c.gallery_folders("da-adopts")]
    assert names == ["Featured", "Points Adopts", "Auctions - Open", "OTA offer to adopt"]


def test_resolve_group_folder(store):
    connected(store)
    c, _ = client(store, group_routes())
    r = c.resolve_group_folder("da-adopts", "points  adopts")
    assert r == {"group": "da-adopts", "group_id": "G-UUID", "folder_id": "F-2", "folder_name": "Points Adopts"}
    assert c.resolve_group_folder("da-adopts", "auctions_open")["folder_id"] == "F-3"


def test_resolve_group_folder_helpful_error(store):
    connected(store)
    c, _ = client(store, group_routes())
    with pytest.raises(DAApiError) as e:
        c.resolve_group_folder("da-adopts", "Money adopts")
    assert "Featured" in str(e.value) and "OTA offer to adopt" in str(e.value)
    with pytest.raises(DAApiError, match="несколько"):
        c.resolve_group_folder("da-adopts", "o")


# --- Загрузка и публикация ---

def test_stash_submit_validation(store, tmp_path):
    connected(store)
    c, router = client(store, {})
    with pytest.raises(DAApiError, match="не найден"):
        c.stash_submit(tmp_path / "nope.png", "t")
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"x")
    with pytest.raises(DAApiError, match="не картинка"):
        c.stash_submit(doc, "t")
    big = tmp_path / "big.png"
    with open(big, "wb") as fh:
        fh.truncate(da_api.MAX_FILE_BYTES + 1)
    with pytest.raises(DAApiError, match="30 МБ"):
        c.stash_submit(big, "t")
    assert router.calls == []


def test_stash_submit_multipart(store, tmp_path):
    connected(store)
    img = tmp_path / "moth.png"
    img.write_bytes(b"\x89PNG fake")
    seen = []

    def submit(request):
        seen.append(request.content)
        return httpx.Response(200, json={"status": "success", "itemid": 777})

    c, _ = client(store, {("POST", "/api/v1/oauth2/stash/submit"): submit})
    assert c.stash_submit(img, "Moth adopt", "Hi!", tags=["moth dragon", "adopt!", "Adopt"]) == 777
    body = seen[0].decode(errors="replace")
    assert 'filename="moth.png"' in body and "Moth adopt" in body
    assert "moth_dragon" in body and body.count('name="tags[]"') == 2  # adopt! → adopt, дубль Adopt убран


def test_stash_publish_paired_arrays(store):
    connected(store)
    seen = []

    def publish(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "success", "url": "https://da/art/x-1", "deviationid": "D-1"})

    c, _ = client(store, {("POST", "/api/v1/oauth2/stash/publish"): publish})
    out = c.stash_publish(42, is_mature=False, galleryids=["MY-F"], tags=["moth"],
                          groups=[("G-1", "F-1"), ("G-2", "F-2"), ("G-3", "F-3")])
    assert out == {"url": "https://da/art/x-1", "deviationid": "D-1"}
    body = seen[0].content.decode()
    pairs = [kv.split("=") for kv in body.split("&")]
    groups = [v for k, v in pairs if k == "groups%5B%5D"]
    folders = [v for k, v in pairs if k == "group_folders%5B%5D"]
    assert groups == ["G-1", "G-2", "G-3"] and folders == ["F-1", "F-2", "F-3"]
    f = form(seen[0])
    assert f["itemid"] == ["42"] and f["is_mature"] == ["false"] and f["noai"] == ["true"]
    assert f["is_ai_generated"] == ["false"] and f["galleryids[]"] == ["MY-F"] and "mature_level" not in f


def test_stash_publish_mature_level_only_when_mature(store):
    connected(store)
    seen = []
    c, router = client(store, {("POST", "/api/v1/oauth2/stash/publish"):
                               lambda r: seen.append(form(r)) or httpx.Response(200, json={"status": "success"})})
    c.stash_publish(1, is_mature=True, mature_level="moderate", mature_classification=["gore"])
    assert seen[0]["is_mature"] == ["true"] and seen[0]["mature_level"] == ["moderate"]
    assert seen[0]["mature_classification[]"] == ["gore"] and "groups[]" not in seen[0]


def test_mature_without_level_refused_before_request(store):
    """18+ без уровня DeviantArt отвергнет — не отправляем вовсе, пусть она выберет (#8)."""
    connected(store)
    c, router = client(store, {})
    with pytest.raises(DAApiError, match="strict или moderate"):
        c.stash_publish(1, is_mature=True)
    with pytest.raises(DAApiError, match="Неизвестная пометка"):
        c.stash_publish(1, is_mature=True, mature_level="strict", mature_classification=["spicy"])
    assert router.calls == []


def test_normalize_publish_fields():
    """Что уйдёт в DeviantArt — видно до её «го»: название не режем молча, теги объясняем (#15)."""
    title, tags, notes = da_api.normalize_publish_fields(" Moth adopt ", ["адопт", "open adopt", "OTA!", "chibi-ych"])
    assert title == "Moth adopt" and tags == ["open_adopt", "OTA", "chibiych"]
    assert any("адопт" in n and "не войдёт" in n for n in notes)
    assert any("open adopt" in n and "open_adopt" in n for n in notes)
    with pytest.raises(DAApiError, match="длиннее 50"):
        da_api.normalize_publish_fields("x" * 51)
    with pytest.raises(DAApiError):
        da_api.normalize_publish_fields("  ")


def test_stash_submit_refuses_long_title_before_upload(store, tmp_path):
    connected(store)
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    c, router = client(store, {})
    with pytest.raises(DAApiError, match="длиннее 50"):
        c.stash_submit(img, "y" * 72)
    assert router.calls == []


def test_revoke_tristate(store):
    """Отзыв: подтверждён DeviantArt / удалён только локально / подключения не было (#56)."""
    connected(store)
    seen = []
    c, _ = client(store, {("POST", "/oauth2/revoke"):
                          lambda r: seen.append(form(r)) or httpx.Response(200, json={"success": True})})
    assert c.revoke() == "revoked" and seen[0]["token"] == ["RT"]
    assert not c.is_connected() and c.revoke() == "not_connected"

    connected(store)
    c, _ = client(store, {("POST", "/oauth2/revoke"): lambda r: httpx.Response(400, json={"error": "bad"})})
    assert c.revoke() == "local_only" and not c.is_connected()

    connected(store)

    def down(request):
        raise httpx.ConnectError("no net", request=request)

    c, _ = client(store, {("POST", "/oauth2/revoke"): down})
    assert c.revoke() == "local_only" and not c.is_connected()


# --- Её картинки ---

def test_recent_images(tmp_path):
    dl, desk = tmp_path / "Downloads", tmp_path / "Desktop"
    (dl / "art").mkdir(parents=True)
    desk.mkdir()
    files = {dl / "newest.png": 100, desk / "oldest.jpg": 300, dl / "art" / "mid.webp": 200,
             dl / "notes.txt": 400, dl / ".hidden.png": 500}
    for p, mtime in files.items():
        p.write_bytes(b"x")
        os.utime(p, (NOW - mtime, NOW - mtime))
    # По умолчанию только верхний уровень: в подпапки не заходим (#19).
    out = da_api.recent_images([dl, desk, tmp_path / "missing"], limit=10, now=NOW)
    assert [i["name"] for i in out] == ["newest.png", "oldest.jpg"]
    assert out[0]["age_s"] == pytest.approx(100) and out[0]["size"] == 1
    deep = da_api.recent_images([dl, desk], limit=10, now=NOW, deep=True)
    assert [i["name"] for i in deep] == ["newest.png", "mid.webp", "oldest.jpg"]
    assert len(da_api.recent_images([dl, desk], limit=1, now=NOW)) == 1


def test_recent_images_skips_symlinks_and_reports_denied(tmp_path):
    dl, desk, outside = tmp_path / "Downloads", tmp_path / "Desktop", tmp_path / "ext"
    dl.mkdir()
    desk.mkdir()
    outside.mkdir()
    (outside / "far.png").write_bytes(b"x")
    (dl / "link.png").symlink_to(outside / "far.png")
    (dl / "ok.png").write_bytes(b"x")
    desk.chmod(0)
    try:
        report = da_api.recent_images_report([dl, desk], limit=10, now=NOW)
    finally:
        desk.chmod(0o755)
    assert [i["name"] for i in report["items"]] == ["ok.png"]
    assert report["denied"] == ["Рабочий стол"]


def test_connect_main_without_client_id(capsys):
    assert da_api.connect_main("") == 1
    assert "client_id" in capsys.readouterr().out


def test_token_record_requires_access_token():
    with pytest.raises(DAApiError):
        da_api._token_record({"error": "x"}, time.time())
    assert json.dumps(da_api._token_record({"access_token": "a", "expires_in": 10}, NOW))


# --- Комментарии (ставки) ---

def comment(cid, user, posted, body="", parent=None, hidden=None, excerpt=None):
    raw = {"commentid": cid, "parentid": parent, "posted": posted, "replies": 0, "hidden": hidden, "body": body,
           "is_liked": False, "is_featured": False, "likes": 0,
           "user": {"userid": "u", "username": user, "usericon": "", "type": "regular"}}
    if excerpt is not None:
        raw["text_content"] = {"excerpt": excerpt, "body": {"type": "draft", "features": "[]"}}
    return raw


def test_deviation_comments_paginates_and_flattens(store):
    seen = []

    def comments(request):
        seen.append(dict(request.url.params))
        if request.url.params["offset"] == "0":
            return httpx.Response(200, json={
                "has_more": True, "next_offset": 2, "has_less": False, "prev_offset": None, "thread": [
                    comment("C2", "bob", "2026-10-31T21:00:00-0700", "<p>$15&nbsp;please</p>"),
                    comment("C1", "alice", "2026-10-31T20:00:00-0700", "SB<br/>yay"),
                ]})
        return httpx.Response(200, json={
            "has_more": False, "next_offset": None, "has_less": True, "prev_offset": 0, "thread": [
                comment("C3", "owner", "2026-10-31T22:00:00-0700", "thanks!", parent="C2"),
                comment("C4", "carol", "2026-10-31T22:30:00-0700", "", parent="C3", excerpt="+5"),
                comment("C5", "spam", "2026-10-31T23:00:00-0700", "hidden", hidden="hidden_as_spam"),
            ]})

    c, _ = client(connected(store), {("GET", "/api/v1/oauth2/comments/deviation/UUID-1"): comments})
    out = c.deviation_comments("UUID-1")
    assert [x.comment_id for x in out] == ["C1", "C2", "C3", "C4"]  # по времени, скрытый пропущен
    assert [x.depth for x in out] == [0, 0, 1, 2] and out[3].parent_id == "C3"
    assert out[0].text == "SB\nyay" and out[1].text == "$15\xa0please" and out[3].text == "+5"
    assert out[0].posted_ts == pytest.approx(1793502000.0)
    assert seen[0]["maxdepth"] == "5" and seen[0]["limit"] == "50" and seen[0]["expand"] == "comment.fulltext"
    assert seen[1]["offset"] == "2"


def test_deviation_comments_respects_cap(store):
    def comments(request):
        off = int(request.url.params["offset"])
        return httpx.Response(200, json={"has_more": True, "next_offset": off + 50, "has_less": False,
                                         "prev_offset": None,
                                         "thread": [comment(f"C{off + i}", "u", 1_790_000_000 + off + i)
                                                    for i in range(50)]})

    c, router = client(connected(store), {("GET", "/api/v1/oauth2/comments/deviation/U"): comments})
    out = c.deviation_comments("U", max_comments=120)
    # Обрезаем, оставляя самые новые: там последние ставки (#16).
    assert len(out) == 120 and c.comments_truncated
    assert out[-1].posted_ts == max(x.posted_ts for x in out) and out[0].comment_id == f"C{250 - 120}"
    assert len(router.calls) == 5


def test_reply_with_unloaded_parent_is_still_a_reply(store):
    def comments(request):
        return httpx.Response(200, json={"has_more": False, "next_offset": None, "has_less": False,
                                         "prev_offset": None,
                                         "thread": [comment("C9", "x", "2026-10-31T20:00:00Z", "hi", parent="GONE")]})

    c, _ = client(connected(store), {("GET", "/api/v1/oauth2/comments/deviation/U"): comments})
    assert c.deviation_comments("U")[0].depth == 1


def gallery_page(items, has_more, next_offset):
    return {"has_more": has_more, "next_offset": next_offset, "results": [
        {"deviationid": uuid, "printid": None, "is_deleted": False, "url": url, "title": "t"} for uuid, url in items]}


def test_uuid_from_url_finds_in_author_gallery(store):
    def gallery(request):
        assert request.url.params["username"] == "fuvuart" and request.url.params["limit"] == "24"
        if request.url.params["offset"] == "0":
            return httpx.Response(200, json=gallery_page(
                [("U-OTHER", "https://www.deviantart.com/fuvuart/art/Other-1404000000")], True, 24))
        return httpx.Response(200, json=gallery_page(
            [("U-HIT", "https://www.deviantart.com/fuvuart/art/Adopt-3-OPEN-1404679584")], False, None))

    c, _ = client(connected(store), {("GET", "/api/v1/oauth2/gallery/all"): gallery})
    assert c.deviation_uuid_from_url("https://www.deviantart.com/fuvuart/art/Adopt-3-OPEN-1404679584") == "U-HIT"


def test_uuid_from_url_falls_back_to_group_folder(store):
    routes = {
        ("GET", "/api/v1/oauth2/gallery/all"): lambda r: httpx.Response(200, json=gallery_page([], False, None)),
        ("GET", "/api/v1/oauth2/gallery/F-1"): lambda r: httpx.Response(200, json=gallery_page(
            [("U-G", "https://www.deviantart.com/someone/art/Dragon-42")], False, None)),
    }
    c, router = client(connected(store), routes)
    assert c.deviation_uuid_from_url("https://www.deviantart.com/someone/art/Dragon-42",
                                     folders=[("da-adopts", "F-1")]) == "U-G"
    assert router.calls[-1].url.params["username"] == "da-adopts"


def test_uuid_from_url_not_found_and_page_cap(store):
    def gallery(request):
        off = int(request.url.params["offset"])
        return httpx.Response(200, json=gallery_page([], True, off + 24))

    c, router = client(connected(store), {("GET", "/api/v1/oauth2/gallery/all"): gallery})
    assert c.deviation_uuid_from_url("https://www.deviantart.com/a/art/x-1", max_pages=3) is None
    assert len(router.calls) == 3


@pytest.mark.parametrize("url", ["https://evil.example/a/art/x-1", "https://www.deviantart.com/a/gallery/1",
                                 "https://www.deviantart.com/a/art/no-number"])
def test_uuid_from_url_rejects_non_deviation_links(store, url):
    c, _ = client(connected(store), {})
    with pytest.raises(DAApiError):
        c.deviation_uuid_from_url(url)


# --- Вход: localhost бывает и IPv6 ---

def ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not ipv6_loopback(), reason="на машине нет IPv6 loopback")
def test_oauth_flow_over_ipv6(store):
    seen = []
    http = httpx.Client(transport=httpx.MockTransport(Router(oauth_routes(seen))))

    def opener(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        redirect = q["redirect_uri"].replace("localhost", "[::1]")
        with urllib.request.urlopen(f"{redirect}?code=CODE&state={q['state']}", timeout=5) as resp:
            resp.read()

    port = free_port()
    result = da_api.run_oauth_flow("CID", store, port=port, timeout_s=10, opener=opener, http=http)
    assert result == {"username": "MothArt"}
    # redirect_uri, который видит DeviantArt, остаётся localhost — только слушаем оба адреса.
    assert seen[0]["redirect_uri"] == [f"http://localhost:{port}/callback"]


def test_localhost_servers_ipv4_always(monkeypatch):
    monkeypatch.setattr(da_api.socket, "has_ipv6", False)
    servers = da_api._localhost_servers(free_port(), da_api.BaseHTTPRequestHandler)
    try:
        assert [s.server_address[0] for s in servers] == ["127.0.0.1"]
    finally:
        for s in servers:
            s.server_close()



# --- Обновление токена из нескольких процессов (#10, #44, #57) ---

def rotating_token_server(calls, delay=0.0):
    state = {"rt": "RT", "n": 0}

    def token(request):
        calls.append(form(request)["refresh_token"][0])
        time.sleep(delay)
        if form(request)["refresh_token"][0] != state["rt"]:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "used"})
        state["n"] += 1
        state["rt"] = f"RT{state['n'] + 1}"
        return httpx.Response(200, json={"access_token": f"AT{state['n'] + 1}", "refresh_token": state["rt"],
                                         "expires_in": 3600})

    return token


def test_two_clients_refresh_once_and_keep_new_token(store):
    import threading

    connected(store, expires_at=NOW - 10)
    calls = []
    routes = {("POST", "/oauth2/token"): rotating_token_server(calls, delay=0.2),
              ("GET", "/api/v1/oauth2/user/whoami"): lambda r: httpx.Response(200, json={"username": "m"})}
    c1, _ = client(TokenStore(store.path), routes)
    c2, _ = client(TokenStore(store.path), routes)
    errors = []

    def go(c):
        try:
            c.whoami()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go, args=(c,)) for c in (c1, c2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and calls == ["RT"]  # второй увидел свежий токен и не пошёл обновлять
    assert store.load()["refresh_token"] == "RT2"


def test_stale_refresh_does_not_delete_newer_token(store):
    """Процесс со старым refresh_token получил invalid_grant — свежий токен другого процесса не трогаем."""
    connected(store, expires_at=NOW - 10)
    old = store.load()
    calls = []
    routes = {("POST", "/oauth2/token"): rotating_token_server(calls)}
    c1, _ = client(TokenStore(store.path), routes)
    c1._refresh(old)                      # первый процесс: RT → RT2
    assert store.load()["refresh_token"] == "RT2"
    c2, _ = client(TokenStore(store.path), routes)
    got = c2._refresh(old)                # второй держит старую запись
    assert got["refresh_token"] == "RT2" and calls == ["RT"]
    assert store.load()["refresh_token"] == "RT2"


def test_rejected_refresh_still_clears_when_token_is_ours(store):
    connected(store, expires_at=NOW - 10)
    calls = []
    routes = {("POST", "/oauth2/token"): lambda r: calls.append(1) or httpx.Response(
        400, json={"error": "invalid_grant", "error_description": "revoked"})}
    c, _ = client(store, routes)
    with pytest.raises(NotConnected):
        c.whoami()
    assert store.load() is None and calls == [1]


def test_refresh_lock_works_across_processes(store, tmp_path):
    """Замок — файловый: другой процесс, держащий его, задерживает наше обновление."""
    import subprocess
    import sys

    connected(store, expires_at=NOW - 10)
    lock = str(store.path) + ".lock"
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({lock!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(1.0)\n")], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "locked"
    calls = []
    c, _ = client(store, {("POST", "/oauth2/token"): rotating_token_server(calls),
                          ("GET", "/api/v1/oauth2/user/whoami"): lambda r: httpx.Response(200, json={})})
    t0 = time.time()
    c.whoami()
    assert time.time() - t0 >= 0.5 and calls == ["RT"]
    holder.wait(timeout=5)


# --- Глубокие ветки ответов (#16) ---

def test_deep_reply_chain_is_fetched(store):
    """Ставки цепочкой ответов глубже 5 уровней: дочитываем ветку по commentid."""
    chain = [comment(f"C{i}", f"u{i}", 1_790_000_000 + i, f"${10 + i}", parent=(f"C{i - 1}" if i > 1 else None))
             for i in range(1, 9)]
    for raw in chain:
        raw["replies"] = 1 if raw["commentid"] != "C8" else 0
    seen = []

    def comments(request):
        params = dict(request.url.params)
        seen.append(params)
        root = params.get("commentid")
        start = 0 if not root else int(root[1:])           # ветка начинается после root
        part = chain[start:start + int(params["maxdepth"])]  # глубже API не отдаёт
        return httpx.Response(200, json={"has_more": False, "next_offset": None, "has_less": False,
                                         "prev_offset": None, "thread": part})

    c, _ = client(connected(store), {("GET", "/api/v1/oauth2/comments/deviation/U"): comments})
    out = c.deviation_comments("U")
    assert [x.comment_id for x in out] == [f"C{i}" for i in range(1, 9)]
    assert out[-1].depth == 7 and any(p.get("commentid") for p in seen)


# --- Поиск UUID с кешем промахов (#17) ---

def test_uuid_lookup_miss_cache(store):
    calls = []
    routes = {("GET", "/api/v1/oauth2/gallery/all"): lambda r: calls.append(1) or httpx.Response(
        200, json={"results": [], "has_more": False, "next_offset": None})}
    clock = {"t": NOW}
    c, _ = client(connected(store, expires_at=NOW + 10 * da_api.UUID_MISS_TTL_S), routes, clock=lambda: clock["t"])
    misses: dict = {}
    url = "https://www.deviantart.com/moth/art/Moth-123"
    assert c.deviation_uuid_from_url(url, max_pages=2, miss_cache=misses) is None
    assert c.deviation_uuid_from_url(url, max_pages=2, miss_cache=misses) is None
    assert len(calls) == 1 and url in misses
    clock["t"] += da_api.UUID_MISS_TTL_S + 1
    c.deviation_uuid_from_url(url, max_pages=2, miss_cache=misses)
    assert len(calls) == 2


def test_hidden_reply_does_not_trigger_extra_requests(store):
    """Ответ скрыт автором — он пришёл, просто не ставка: лишних запросов за веткой нет."""
    top = comment("C1", "anna", 1_790_000_000, "$10")
    top["replies"] = 1
    thread = [top, comment("C2", "kiwi", 1_790_000_100, "$15", parent="C1", hidden="hidden_by_owner")]
    c, router = client(connected(store), {("GET", "/api/v1/oauth2/comments/deviation/U"): lambda r: httpx.Response(
        200, json={"has_more": False, "next_offset": None, "has_less": False, "prev_offset": None, "thread": thread})})
    assert [x.comment_id for x in c.deviation_comments("U")] == ["C1"]
    assert len(router.calls) == 1 and not c.comments_truncated


# --- Статистика работ (/deviation/metadata) ---

META_PATH = ("GET", "/api/v1/oauth2/deviation/metadata")


def meta_reply(request):
    ids = request.url.params.get_list("deviationids[]")
    return httpx.Response(200, json={"metadata": [
        {"deviationid": u, "tags": [{"tag_name": "adopt"}, {"tag_name": "dragon"}],
         "stats": {"views": 100 + i, "favourites": 10 + i, "comments": i, "downloads": 0}}
        for i, u in enumerate(ids)]})


def test_deviation_metadata_parses_stats_and_tags(store):
    connected(store)
    c, router = client(store, {META_PATH: meta_reply})
    out = c.deviation_metadata(["U1", "U2", "U1", ""])
    assert out["U1"] == {"views": 100, "favourites": 10, "comments": 0, "downloads": 0, "tags": ["adopt", "dragon"]}
    assert out["U2"]["favourites"] == 11
    assert len(router.calls) == 1
    params = router.calls[0].url.params
    assert params.get_list("deviationids[]") == ["U1", "U2"] and params["ext_stats"] == "true"


def test_deviation_metadata_without_ext_stats(store):
    connected(store)
    c, router = client(store, {META_PATH: meta_reply})
    c.deviation_metadata(["U1"], ext_stats=False)
    assert "ext_stats" not in router.calls[0].url.params


def test_deviation_metadata_chunks(store):
    connected(store)
    c, router = client(store, {META_PATH: meta_reply})
    ids = [f"U{i}" for i in range(da_api.METADATA_CHUNK * 2 + 3)]
    out = c.deviation_metadata(ids)
    assert set(out) == set(ids)
    assert [len(r.url.params.get_list("deviationids[]")) for r in router.calls] == [
        da_api.METADATA_CHUNK, da_api.METADATA_CHUNK, 3]


def test_deviation_metadata_empty_makes_no_requests(store):
    connected(store)
    c, router = client(store, {META_PATH: meta_reply})
    assert c.deviation_metadata([]) == {}
    assert router.calls == []


def test_deviation_metadata_halves_chunk_on_too_many(store):
    connected(store)

    def reply(request):
        if len(request.url.params.get_list("deviationids[]")) > 10:
            return httpx.Response(400, json={"error": "invalid_request", "error_description": "Too many", "error_code": 0})
        return meta_reply(request)
    c, router = client(store, {META_PATH: reply})
    ids = [f"U{i}" for i in range(30)]
    out = c.deviation_metadata(ids)
    assert set(out) == set(ids)
    sizes = [len(r.url.params.get_list("deviationids[]")) for r in router.calls]
    assert sizes[:3] == [30, 25, 12] and all(s <= 10 for s in sizes[3:]) and sum(sizes[3:]) == 30


def test_deviation_metadata_other_errors_raise(store):
    connected(store)
    err = {"error": "invalid_request", "error_description": "Bad id", "error_code": 2}
    c, _ = client(store, {META_PATH: lambda r: httpx.Response(400, json=err)})
    with pytest.raises(DAApiError) as e:
        c.deviation_metadata(["U1"])
    assert e.value.code == 2


def test_deviation_metadata_rate_limited(store):
    connected(store)
    c, router = client(store, {META_PATH: lambda r: httpx.Response(429)})
    with pytest.raises(RateLimited):
        c.deviation_metadata([f"U{i}" for i in range(120)])
    assert len(router.calls) == 1


def test_deviation_metadata_not_connected(store):
    c, router = client(store, {META_PATH: meta_reply})
    with pytest.raises(NotConnected):
        c.deviation_metadata(["U1"])
    assert router.calls == []
