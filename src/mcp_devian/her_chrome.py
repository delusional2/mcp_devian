"""Режим chrome: Toyhouse читается в её собственном Google Chrome через Apple Events.

Сервер открывает вкладку в её обычном (не инкогнито) окне Chrome, ждёт страницу,
выполняет скрипт-экстрактор (toyhouse.EXTRACT_*_JS), закрывает вкладку и
возвращает ту, что была открыта. Это её обычный браузер с её логином, поэтому
Cloudflare видит обычного посетителя; если он всё же спросит «я человек»,
галочку нажимает она — вкладка ждёт её.

Нужно один раз:
1. Chrome → «Вид» → «Разработчикам» → «Разрешить JavaScript из событий Apple».
2. Разрешить Claude управлять Google Chrome, когда macOS спросит.
"""

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .toyhouse import (
    EXTRACT_BOARD_JS,
    EXTRACT_POST_JS,
    ChallengeError,
    ThreadRow,
    ToyhouseError,
    board_url,
    browser_lock,
    rows_from_extracted,
)

SETUP_HINT = (
    "Один раз в Google Chrome: меню «Вид» → «Разработчикам» → «Разрешить JavaScript из событий Apple» "
    "(View → Developer → Allow JavaScript from Apple Events)."
)
PERMISSION_HINT = (
    "macOS не разрешила Claude управлять Google Chrome: Системные настройки → Конфиденциальность и безопасность → "
    "Автоматизация → Claude → включить Google Chrome."
)
NOT_INSTALLED_HINT = "Не нашла Google Chrome. Его нужно поставить с google.com/chrome или выбрать режим bot."
CLOSED_HINT = "Вкладку Toyhouse закрыли раньше, чем страница загрузилась. Можно просто попросить проверить ещё раз."

CHROME_PATHS = [Path("/Applications/Google Chrome.app"), Path.home() / "Applications" / "Google Chrome.app"]
LAUNCH_WAIT_S = 5
ERROR_EXTRA_S = 30

# JXA: аргументы приходят через argv, поэтому скрипт-экстрактор не надо экранировать.
# argv: url, js, ready-селектор, секунд на загрузку, секунд ждать галочку, оставить ли вкладку с проверкой.
JXA = r"""
function run(argv) {
  const url = argv[0], js = argv[1], ready = argv[2];
  const loadS = Number(argv[3]), challengeS = Number(argv[4]), keepOnChallenge = argv[5] === '1';
  const chrome = Application('Google Chrome');
  if (!chrome.running()) { chrome.launch(); }

  // Окно — первое обычное (не инкогнито). После запуска Chrome даём сессии восстановиться.
  let modes = [];
  for (let i = 0; i < 20; i++) {
    try { modes = chrome.windows.mode(); } catch (e) { modes = []; }
    if (modes.indexOf('normal') >= 0) break;
    delay(0.25);
  }
  if (modes.indexOf('normal') < 0) {
    chrome.Window({mode: 'normal'}).make();
    delay(0.5);
    modes = chrome.windows.mode();
  }
  const win = chrome.windows.byId(chrome.windows.id()[modes.indexOf('normal')]);
  let beforeId = null;
  try { beforeId = win.activeTab.id(); } catch (e) {}

  win.tabs.push(chrome.Tab({url: url}));
  const ids = win.tabs.id();
  const id = ids[ids.length - 1];

  // Вкладку ищем по id: она могла переехать в другое окно.
  const find = () => {
    try { const t = win.tabs.byId(id); t.id(); return t; } catch (e) {}
    for (const wid of chrome.windows.id()) {
      try { const t = chrome.windows.byId(wid).tabs.byId(id); t.id(); return t; } catch (e) {}
    }
    return null;
  };
  const activate = (tabId) => {
    try { const i = win.tabs.id().indexOf(tabId); if (i >= 0) win.activeTabIndex = i + 1; } catch (e) {}
  };

  const cf = "(window._cf_chl_opt || document.getElementById('challenge-error-text') || " +
    "document.getElementById('challenge-form'))";
  const probe = "document.querySelector(" + JSON.stringify(ready) + ") ? 'ready' : " + cf + " ? 'challenge' : " +
    "((document.readyState === 'complete' && document.querySelector('.navbar, nav, footer, #main')) ? 'loaded' : 'wait')";

  let state = 'wait', lastError = null, waiting = false;
  let deadline = Date.now() + loadS * 1000;
  try {
    while (Date.now() < deadline) {
      delay(1);
      const tab = find();
      if (!tab) { state = 'closed'; break; }
      try { state = tab.execute({javascript: probe}); lastError = null; }
      catch (e) {
        lastError = String(e);
        if (/p=applescript|AppleScript|Apple ?Events|событий Apple/i.test(lastError)) { state = 'error'; break; }
        if (e.errorNumber === -1728) { state = 'closed'; break; }
        continue;
      }
      if (state === 'ready') {
        return JSON.stringify({state: state, result: tab.execute({javascript: js})});
      }
      if (state === 'loaded') { return JSON.stringify({state: state}); }
      // Проверка Cloudflare часто проходит сама за пару секунд, поэтому просто ждём дальше.
      // Если нам разрешено ждать её — показываем вкладку и продлеваем срок.
      if (state === 'challenge' && !waiting && challengeS > 0) {
        waiting = true;
        activate(id);
        chrome.activate();
        deadline = Date.now() + challengeS * 1000;
      }
    }
    return JSON.stringify({state: state, error: lastError});
  } finally {
    const keep = keepOnChallenge && state === 'challenge';
    if (!keep && state !== 'closed') {
      try { const tab = find(); if (tab) tab.close(); } catch (e) {}
    }
    if (!keep && beforeId !== null) activate(beforeId);
  }
}
"""


class ChromeSetupError(ToyhouseError):
    """Chrome не даёт выполнять скрипты или его нет: нужна разовая настройка."""


def chrome_installed() -> bool:
    """Без Apple Events: просто смотрим, есть ли приложение."""
    return any(p.exists() for p in CHROME_PATHS)


def classify_error(text: str) -> ToyhouseError:
    low = text.lower()
    if "-1743" in text or "not authorized" in low or "не разрешено" in low:
        return ChromeSetupError(PERMISSION_HINT)
    # Текст ошибки переводится на язык Chrome, а ссылка ?p=applescript и слово AppleScript — нет.
    if ("p=applescript" in low or "applescript" in low or "apple events" in low or "appleevents" in low
            or "событий apple" in low or "apple-событ" in low):
        return ChromeSetupError(SETUP_HINT)
    if "-2700" in text or "can't be found" in low or "не удается найти" in low or "-10814" in text:
        return ChromeSetupError(NOT_INSTALLED_HINT)
    if "-1728" in text:
        return ToyhouseError(CLOSED_HINT)
    return ToyhouseError(f"Chrome: {text.strip()[:200]}")


def run_in_chrome(url: str, js: str, ready_selector: str, load_s: int = 30, challenge_wait_s: int = 0,
                  keep_on_challenge: bool = True, runner=None) -> str | None:
    """Открыть url во вкладке её Chrome, дождаться ready_selector, выполнить js.

    Возвращает результат js или None, если страница загрузилась, но нужного на ней нет
    (пустая доска, удалённый тред).
    """
    if not chrome_installed():
        raise ChromeSetupError(NOT_INSTALLED_HINT)
    timeout = LAUNCH_WAIT_S + load_s + challenge_wait_s + ERROR_EXTRA_S
    try:
        done = (runner or subprocess.run)(
            ["osascript", "-l", "JavaScript", "-e", JXA, url, js, ready_selector, str(load_s),
             str(challenge_wait_s), "1" if keep_on_challenge else "0"],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise ToyhouseError("Chrome не ответил вовремя (вкладка Toyhouse могла остаться открытой)") from e
    except OSError as e:
        raise ToyhouseError(f"не получилось запустить osascript: {e}") from e
    if done.returncode != 0:
        raise classify_error(done.stderr or done.stdout)
    try:
        answer = json.loads(done.stdout.strip() or "{}")
    except json.JSONDecodeError as e:
        raise ToyhouseError(f"Chrome вернул что-то странное: {done.stdout[:200]}") from e
    state = answer.get("state")
    if state == "ready":
        return answer.get("result") or ""
    if state == "loaded":
        return None
    if state == "closed":
        raise ToyhouseError(CLOSED_HINT)
    if answer.get("error"):
        raise classify_error(answer["error"])
    if state == "challenge":
        where = "вкладка открыта, нажми галочку" if keep_on_challenge else "нужно нажать галочку"
        raise ChallengeError(f"Toyhouse в Chrome просит подтвердить, что ты человек: {where}")
    raise ToyhouseError(f"Страница не загрузилась: {url}")


class HerChromeReader:
    """То же, что toyhouse.Reader, но читает в её Google Chrome."""

    def __init__(self, interactive: bool = False, on_challenge=None, challenge_wait_s: int = 120, runner=None):
        self.interactive = interactive
        self.on_challenge = on_challenge
        self.challenge_wait_s = challenge_wait_s
        self.runner = runner

    def _run(self, url: str, js: str, ready: str) -> str | None:
        wait = self.challenge_wait_s if self.interactive else 0
        if not wait:
            # По расписанию никого рядом нет: вкладку с проверкой оставляем ей.
            return run_in_chrome(url, js, ready, keep_on_challenge=True, runner=self.runner)
        try:
            # Первый заход без лишних вкладок: если проверка сама не прошла, вкладку закрываем...
            return run_in_chrome(url, js, ready, keep_on_challenge=False, runner=self.runner)
        except ChallengeError:
            # ...зовём её и открываем одну вкладку, которая ждёт галочку.
            if self.on_challenge:
                self.on_challenge()
            return run_in_chrome(url, js, ready, challenge_wait_s=wait, keep_on_challenge=True, runner=self.runner)

    def board(self, board_id: int, page: int, now: float) -> list[ThreadRow]:
        raw = self._run(board_url(board_id, page), EXTRACT_BOARD_JS, "tr.forum-thread-row")
        if raw is None:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ToyhouseError("скрипт вернул не JSON") from e
        return rows_from_extracted(items if isinstance(items, list) else [], board_id, now)

    def first_post(self, url: str) -> str | None:
        raw = self._run(url, EXTRACT_POST_JS, ".forum-post")
        return (raw or "").strip() or None


@contextmanager
def open_her_chrome(interactive: bool = False, on_challenge=None):
    """Сериализуем с ботом: одновременно читает Toyhouse только кто-то один (иначе BrowserBusy)."""
    with browser_lock():
        yield HerChromeReader(interactive=interactive, on_challenge=on_challenge)
