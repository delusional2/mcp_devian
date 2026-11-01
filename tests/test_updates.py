import json

import httpx

from mcp_devian import updates
from mcp_devian.updates import DOWNLOAD_URL, check_for_update, notice_to_announce, parse_version


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _client(payload=None, status=200, calls=None, exc=None):
    def handler(request):
        if calls is not None:
            calls.append(request)
        if exc:
            raise exc
        return httpx.Response(status, json=payload if payload is not None else {})
    return httpx.Client(transport=httpx.MockTransport(handler))


REL = {"tag_name": "v0.5.0", "name": "Водяные знаки и обновления", "draft": False, "prerelease": False}


def test_parse_version():
    assert parse_version("v0.5.0") == (0, 5, 0, 1)
    assert parse_version("0.5") == (0, 5, 0, 1)
    assert parse_version("1.2.3-beta.1") < parse_version("1.2.3")
    assert parse_version("garbage") is None


def test_newer_release_gives_one_line_notice(tmp_path):
    calls = []
    note = check_for_update(current="0.4.0", http=_client(REL, calls=calls), now=Clock(),
                            cache_path=tmp_path / "u.json")
    assert note and "\n" not in note
    assert "0.5.0" in note and "0.4.0" in note and DOWNLOAD_URL in note
    assert "Водяные знаки и обновления" in note
    req = calls[0]
    assert str(req.url) == updates.RELEASES_API
    assert "mcp_devian" in req.headers["User-Agent"]


def test_same_or_older_version_is_none(tmp_path):
    assert check_for_update(current="0.5.0", http=_client(REL), now=Clock(), cache_path=tmp_path / "a.json") is None
    assert check_for_update(current="0.6.1", http=_client(REL), now=Clock(), cache_path=tmp_path / "b.json") is None


def test_checks_network_at_most_once_a_day(tmp_path):
    calls, clock, cache = [], Clock(), tmp_path / "u.json"
    http = _client(REL, calls=calls)
    first = check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache)
    clock.t += 3600
    second = check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache)
    assert first == second and len(calls) == 1
    clock.t += 24 * 3600
    check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache)
    assert len(calls) == 2


def test_cached_notice_disappears_after_she_updates(tmp_path):
    clock, cache = Clock(), tmp_path / "u.json"
    assert check_for_update(current="0.4.0", http=_client(REL), now=clock, cache_path=cache)
    boom = _client(exc=AssertionError("сеть не нужна"))
    assert check_for_update(current="0.5.0", http=boom, now=clock, cache_path=cache) is None


def test_network_error_is_none_and_not_retried_every_call(tmp_path):
    calls, clock, cache = [], Clock(), tmp_path / "u.json"
    http = _client(calls=calls, exc=httpx.ConnectError("offline"))
    assert check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache) is None
    assert check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache) is None
    assert len(calls) == 1
    clock.t += updates.RETRY_AFTER_ERROR + 1
    check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache)
    assert len(calls) == 2


def test_error_keeps_previously_known_release(tmp_path):
    clock, cache = Clock(), tmp_path / "u.json"
    check_for_update(current="0.4.0", http=_client(REL), now=clock, cache_path=cache)
    clock.t += 2 * 24 * 3600
    note = check_for_update(current="0.4.0", http=_client(exc=httpx.ReadTimeout("slow")), now=clock, cache_path=cache)
    assert note and "0.5.0" in note


def test_http_errors_and_bad_payloads_are_none(tmp_path):
    for i, (payload, status) in enumerate([({"message": "rate limit"}, 403), ({}, 404), ({"tag_name": "nope"}, 200),
                                           (["x"], 200), ({**REL, "draft": True}, 200), ({**REL, "prerelease": True}, 200)]):
        assert check_for_update(current="0.4.0", http=_client(payload, status), now=Clock(),
                                cache_path=tmp_path / f"{i}.json") is None


def test_non_json_body_is_none(tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    assert check_for_update(current="0.4.0", http=http, now=Clock(), cache_path=tmp_path / "u.json") is None


def test_corrupt_cache_is_ignored(tmp_path):
    cache = tmp_path / "u.json"
    cache.write_text("{not json")
    assert check_for_update(current="0.4.0", http=_client(REL), now=Clock(), cache_path=cache)
    json.loads(cache.read_text())


def test_clock_going_backwards_rechecks(tmp_path):
    calls, clock, cache = [], Clock(), tmp_path / "u.json"
    http = _client(REL, calls=calls)
    check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache)
    clock.t -= 10 * 24 * 3600
    check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache)
    assert len(calls) == 2


def test_title_is_single_line_and_short(tmp_path):
    rel = {**REL, "name": "Длинное\nназвание " + "x" * 300}
    note = check_for_update(current="0.4.0", http=_client(rel), now=Clock(), cache_path=tmp_path / "u.json")
    assert "\n" not in note and len(note) < 400


def test_title_falls_back_to_tag(tmp_path):
    rel = {**REL, "name": ""}
    note = check_for_update(current="0.4.0", http=_client(rel), now=Clock(), cache_path=tmp_path / "u.json")
    assert note and "«" not in note


def test_force_skips_cache(tmp_path):
    calls, clock, cache = [], Clock(), tmp_path / "u.json"
    http = _client(REL, calls=calls)
    check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache)
    check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache, force=True)
    assert len(calls) == 2


def test_default_cache_lives_in_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "DATA_DIR", tmp_path)
    check_for_update(current="0.4.0", http=_client(REL), now=Clock())
    assert (tmp_path / "update-check.json").exists()


def test_never_raises_even_if_cache_unwritable(tmp_path):
    cache = tmp_path / "dir-not-file"
    cache.mkdir()
    assert check_for_update(current="0.4.0", http=_client(REL), now=Clock(), cache_path=cache)


def test_announce_once_per_version(tmp_path):
    clock, cache = Clock(), tmp_path / "u.json"
    http = _client(REL)
    first = notice_to_announce(current="0.4.0", http=http, now=clock, cache_path=cache)
    assert first and "0.5.0" in first
    assert notice_to_announce(current="0.4.0", http=http, now=clock, cache_path=cache) is None
    # Но в настройках продолжаем показывать, пока не обновится.
    assert check_for_update(current="0.4.0", http=http, now=clock, cache_path=cache)
    newer = _client({**REL, "tag_name": "v0.6.0"})
    clock.t += 2 * 24 * 3600
    assert "0.6.0" in notice_to_announce(current="0.4.0", http=newer, now=clock, cache_path=cache)


# --- Регрессии после проверки ---

def test_own_client_follows_redirects(tmp_path, monkeypatch):
    real = httpx.Client
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if str(request.url) == updates.RELEASES_API:
            return httpx.Response(301, headers={"Location": "https://api.github.com/repos/new/name/releases/latest"})
        return httpx.Response(200, json=REL)

    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    note = check_for_update(current="0.4.0", now=Clock(), cache_path=tmp_path / "u.json")
    assert note and "0.5.0" in note and len(seen) == 2


def test_no_network_under_pytest_without_fake_client(tmp_path, monkeypatch):
    def boom(**kw):
        raise AssertionError("network client created in tests")

    monkeypatch.setattr(httpx, "Client", boom)
    assert check_for_update(current="0.4.0", now=Clock(), cache_path=tmp_path / "u.json") is None


def test_short_notice_for_macos(tmp_path):
    note = notice_to_announce(current="0.4.0", http=_client(REL), now=Clock(), cache_path=tmp_path / "u.json",
                              short=True)
    assert note and len(note) <= 220 and "http" not in note and "0.5.0" in note
