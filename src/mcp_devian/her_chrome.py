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


# --- Отклик в тред: один запуск osascript на всё ---
#
# argv: url треда, режим (inspect | fill | submit), скрипт формы, скрипт проверки поста, секунд на загрузку.
# Допущения, которые надо проверить на её залогиненной странице:
# - форма ответа есть на странице треда (на последней — точно); если на первой её нет,
#   идём по ссылке «Next» (a[rel=next]) до последней страницы, не больше MAX_HOPS раз;
# - после отправки Toyhouse сам открывает страницу с новым постом, и мы ищем его там.
REPLY_JXA = r"""
function run(argv) {
  const url = argv[0], mode = argv[1], formJs = argv[2], verifyJs = argv[3];
  const loadS = Number(argv[4]), maxHops = Number(argv[5]);
  const chrome = Application('Google Chrome');
  if (!chrome.running()) { chrome.launch(); }
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
  const probe = "document.querySelector('.forum-post') ? (document.readyState === 'complete' ? 'ready' : 'wait') : " +
    cf + " ? 'challenge' : ((document.readyState === 'complete' && " +
    "document.querySelector('.navbar, nav, footer, #main')) ? 'loaded' : 'wait')";
  let lastError = null;
  // Ждать, пока страница во вкладке догрузится. Возвращает ready | loaded | challenge | tab_closed | error | timeout.
  const waitPage = () => {
    const deadline = Date.now() + loadS * 1000;
    while (Date.now() < deadline) {
      delay(1);
      const tab = find();
      if (!tab) return 'tab_closed';
      let st;
      try { st = tab.execute({javascript: probe}); lastError = null; }
      catch (e) {
        lastError = String(e);
        if (/p=applescript|AppleScript|Apple ?Events|событий Apple/i.test(lastError)) return 'error';
        if (e.errorNumber === -1728) return 'tab_closed';
        continue;
      }
      if (st === 'ready' || st === 'loaded') return st;
      if (st === 'challenge') { /* часто проходит само — ждём до конца срока */ }
    }
    try { const tab = find(); if (tab && tab.execute({javascript: cf + " ? 1 : 0"}) === 1) return 'challenge'; } catch (e) {}
    return 'timeout';
  };
  const run_js = (js) => JSON.parse(find().execute({javascript: js}));
  let result = {state: 'timeout'}, keep = false;
  try {
    let st = waitPage();
    if (st !== 'ready') {
      if (st === 'challenge') { keep = true; activate(id); chrome.activate(); }
      result = {state: st === 'loaded' ? 'no_form' : st, error: lastError};
      return JSON.stringify(result);
    }
    // Формы нет на этой странице — идём к последней по «Next».
    const hasForm = "(Array.prototype.slice.call(document.querySelectorAll('form')).some(function (f) " +
      "{ return f.querySelector('.fr-element, textarea'); })) ? 1 : 0";
    const nextHref = "(document.querySelector('.pagination a[rel=next]') || {}).href || ''";
    for (let hop = 0; hop < maxHops && find().execute({javascript: hasForm}) !== 1; hop++) {
      const next = find().execute({javascript: nextHref});
      if (!next) break;
      find().url = next;
      st = waitPage();
      if (st !== 'ready') {
        if (st === 'challenge') { keep = true; activate(id); chrome.activate(); }
        result = {state: st === 'loaded' ? 'no_form' : st, error: lastError};
        return JSON.stringify(result);
      }
    }
    result = run_js(formJs);
    if (mode === 'inspect') { result = {state: 'inspected', form: result}; return JSON.stringify(result); }
    if (result.state === 'filled') { keep = true; activate(id); chrome.activate(); return JSON.stringify(result); }
    if (result.state !== 'submitted') return JSON.stringify(result);
    // Отправили один раз. Ждём, пока Toyhouse откроет страницу с постом, и ищем его. Повторно не жмём.
    keep = true;
    const before = find() ? find().url() : url;
    const deadline = Date.now() + 30 * 1000;
    while (Date.now() < deadline) {
      delay(1);
      const tab = find();
      if (!tab) break;
      let ready = false;
      try { ready = tab.execute({javascript: "document.readyState === 'complete' ? 1 : 0"}) === 1; } catch (e) {}
      if (ready && tab.url() !== before) break;
    }
    try {
      const found = run_js(verifyJs);
      if (found.found) { result = {state: 'posted', permalink: found.permalink}; keep = false; }
      else { result = {state: 'submitted_unverified'}; }
    } catch (e) { result = {state: 'submitted_unverified', error: String(e)}; }
    return JSON.stringify(result);
  } finally {
    if (!keep && result.state !== 'tab_closed') {
      try { const tab = find(); if (tab) tab.close(); } catch (e) {}
      if (beforeId !== null) activate(beforeId);
    } else if (keep) {
      activate(id);
    }
  }
}
"""

MAX_HOPS = 60
REPLY_MESSAGES = {
    "posted": "Отклик опубликован.",
    "submitted_unverified": ("Отклик отправлен, но я не нашла его на странице. Вкладка открыта — "
                             "пусть она глянет; второй раз не отправляю."),
    "filled": "Текст вставлен в форму ответа в её Chrome. Проверь и нажми «Post» сама.",
    "no_form": "На странице треда не нашлось формы ответа: возможно, тред закрыт или нужен вход.",
    "closed": "Тред закрыт для ответов.",
    "not_logged_in": "В её Chrome она не залогинена на Toyhouse: пусть войдёт и попросит ещё раз.",
    "fill_failed": "Не получилось вставить текст в форму ответа. Ничего не отправлено.",
    "no_submit_button": "Не нашла кнопку отправки. Текст в форме — можно отправить вручную.",
    "already_submitted": "На этой странице отклик уже отправлялся — второй раз не отправляю.",
    "challenge": "Toyhouse просит подтвердить, что она человек: вкладка открыта, нужно нажать галочку и попросить ещё раз.",
    "timeout": "Страница треда не загрузилась вовремя. Ничего не отправлено.",
    "inspected": "Форма ответа осмотрена.",
}


def _reply_call(thread_url: str, mode: str, form_js: str, verify_js: str, runner, load_s: int) -> dict:
    if not thread_url.startswith("https://toyhou.se/~forums/"):
        raise ToyhouseError("Нужна ссылка на тред форума Toyhouse.")
    if not chrome_installed():
        raise ChromeSetupError(NOT_INSTALLED_HINT)
    # Запас: несколько загрузок страниц (переходы к последней) + ожидание после отправки.
    timeout = LAUNCH_WAIT_S + load_s * 4 + 30 + ERROR_EXTRA_S
    try:
        done = (runner or subprocess.run)(
            ["osascript", "-l", "JavaScript", "-e", REPLY_JXA, thread_url, mode, form_js, verify_js,
             str(load_s), str(MAX_HOPS)],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise ToyhouseError("Chrome не ответил вовремя. Отправился ли отклик — неизвестно: "
                            "посмотрите тред, прежде чем пробовать снова.") from e
    except OSError as e:
        raise ToyhouseError(f"не получилось запустить osascript: {e}") from e
    if done.returncode != 0:
        raise classify_error(done.stderr or done.stdout)
    try:
        answer = json.loads(done.stdout.strip() or "{}")
    except json.JSONDecodeError as e:
        raise ToyhouseError(f"Chrome вернул что-то странное: {done.stdout[:200]}") from e
    state = answer.get("state")
    if state == "tab_closed":
        raise ToyhouseError(CLOSED_HINT)
    if state == "error":
        raise classify_error(answer.get("error") or "")
    answer["message"] = REPLY_MESSAGES.get(state, f"Неожиданный ответ: {state}")
    return answer


def run_reply(thread_url: str, html: str, submit: bool, username: str, marker: str, runner=None,
              load_s: int = 30) -> dict:
    """Вставить её отклик в форму ответа треда в её Chrome и, если submit, отправить один раз.

    Возвращает {"state", "message", ...}: posted (+permalink), submitted_unverified, filled,
    no_form, closed, not_logged_in, fill_failed, no_submit_button, challenge, timeout.
    Ошибки настройки Chrome бросаются как ChromeSetupError.
    """
    from .th_reply import fill_reply_js, verify_post_js

    return _reply_call(thread_url, "submit" if submit else "fill", fill_reply_js(html, submit),
                       verify_post_js(marker, username), runner, load_s)


def inspect_reply_form(thread_url: str, runner=None, load_s: int = 30) -> dict:
    """Только посмотреть форму ответа (ничего не вводит и не отправляет), вкладку закрыть."""
    from .th_reply import INSPECT_REPLY_FORM_JS

    answer = _reply_call(thread_url, "inspect", INSPECT_REPLY_FORM_JS, "", runner, load_s)
    if answer.get("state") == "inspected" and isinstance(answer.get("form"), str):
        answer["form"] = json.loads(answer["form"])
    return answer


@contextmanager
def open_her_chrome(interactive: bool = False, on_challenge=None):
    """Сериализуем с ботом: одновременно читает Toyhouse только кто-то один (иначе BrowserBusy)."""
    with browser_lock():
        yield HerChromeReader(interactive=interactive, on_challenge=on_challenge)


# --- DeviantArt: комментарии (ставки) под работой ---
#
# Разметка DeviantArt (снято с публичной страницы 01.11.2026). Классы CSS там случайные,
# поэтому опираемся только на атрибуты:
# - у тела комментария есть data-commentid (и data-itemid = номер работы), текст — в [data-editor-viewer];
# - блок комментария — ближайший предок с data-indent="<глубина>";
# - автор — a[data-username] внутри блока, время — <time datetime="ISO">;
# - ещё комментарии подгружает кнопка «Load more». Кликаем её, пока число комментариев растёт.
# argv: url, номер работы, секунд на загрузку, сколько раз жать «Load more».
DA_COMMENTS_JXA = r"""
function run(argv) {
  const url = argv[0], itemId = argv[1], loadS = Number(argv[2]), maxMore = Number(argv[3]);
  const chrome = Application('Google Chrome');
  if (!chrome.running()) { chrome.launch(); }
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
  const probe = "(document.readyState === 'complete' && document.getElementById('comments')) ? 'ready' : " +
    cf + " ? 'challenge' : 'wait'";
  const countJs = "document.querySelectorAll('[data-commentid][data-itemid=" + JSON.stringify(itemId) + "]').length";
  const clickJs = "(() => { const re = /^(load more|show more|загрузить ещё|загрузить еще|показать ещё|показать еще)$/i;" +
    " const b = [...document.querySelectorAll('button')].find(x => re.test(x.textContent.trim()));" +
    " if (!b || b.disabled) return 'none'; b.click(); return 'clicked'; })()";
  const extractJs = "JSON.stringify([...document.querySelectorAll('[data-commentid][data-itemid=" +
    JSON.stringify(itemId) + "]')].map(el => {" +
    " const box = el.closest('[data-indent]') || el;" +
    " const own = (sel) => [...box.querySelectorAll(sel)].find(n => (n.closest('[data-indent]') || box) === box);" +
    " const u = own('a[data-username]'); const t = own('time[datetime]');" +
    " const v = el.querySelector('[data-editor-viewer]');" +
    " return {comment_id: el.getAttribute('data-commentid'), user: u ? u.getAttribute('data-username') : ''," +
    " posted: t ? t.getAttribute('datetime') : '', depth: Number(box.getAttribute('data-indent') || 0)," +
    " text: (v ? v.innerText : el.innerText || '').trim()}; }))";

  let state = 'wait', lastError = null;
  const deadline = Date.now() + loadS * 1000;
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
      if (state === 'ready') break;
    }
    if (state !== 'ready') return JSON.stringify({state: state === 'wait' ? 'timeout' : state, error: lastError});
    delay(1.5);  // комментарии дорисовываются после загрузки страницы
    let clicks = 0;
    for (; clicks < maxMore; clicks++) {
      const tab = find();
      if (!tab) return JSON.stringify({state: 'closed'});
      const before = Number(tab.execute({javascript: countJs}) || 0);
      if (tab.execute({javascript: clickJs}) !== 'clicked') break;
      let grown = false;
      for (let w = 0; w < 12 && !grown; w++) {  // до 6 секунд на подгрузку
        delay(0.5);
        grown = Number(tab.execute({javascript: countJs}) || 0) > before;
      }
      if (!grown) break;
    }
    const tab = find();
    if (!tab) return JSON.stringify({state: 'closed'});
    return JSON.stringify({state: 'ready', clicks: clicks, result: tab.execute({javascript: extractJs})});
  } finally {
    if (state !== 'challenge' && state !== 'closed') {
      try { const tab = find(); if (tab) tab.close(); } catch (e) {}
    }
    if (state !== 'challenge' && beforeId !== null) activate(beforeId);
  }
}
"""

DA_COMMENTS_HINT_CLOSED = "Вкладку DeviantArt закрыли раньше, чем комментарии загрузились. Можно попросить ещё раз."


def run_deviantart_comments(deviation_url: str, max_load_more: int = 20, runner=None, load_s: int = 30) -> list:
    """Комментарии под работой DeviantArt, прочитанные в её Chrome (если DA не подключён через API).

    Возвращает list[bids.Comment] по времени (старые первыми); parent_id — по вложенности.
    """
    from .bids import Comment
    from .da_api import DEVIATION_URL

    m = DEVIATION_URL.match(deviation_url.strip())
    if not m:
        raise ToyhouseError("Нужна ссылка на работу DeviantArt вида https://www.deviantart.com/автор/art/название-12345.")
    if not chrome_installed():
        raise ChromeSetupError(NOT_INSTALLED_HINT)
    item_id = m.group(2)
    # Загрузка + до 6 с на каждую подгрузку + запас.
    timeout = LAUNCH_WAIT_S + load_s + max_load_more * 7 + ERROR_EXTRA_S
    try:
        done = (runner or subprocess.run)(
            ["osascript", "-l", "JavaScript", "-e", DA_COMMENTS_JXA, deviation_url.strip(), item_id,
             str(load_s), str(max_load_more)],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise ToyhouseError("Chrome не ответил вовремя (вкладка DeviantArt могла остаться открытой)") from e
    except OSError as e:
        raise ToyhouseError(f"не получилось запустить osascript: {e}") from e
    if done.returncode != 0:
        raise classify_error(done.stderr or done.stdout)
    try:
        answer = json.loads(done.stdout.strip() or "{}")
    except json.JSONDecodeError as e:
        raise ToyhouseError(f"Chrome вернул что-то странное: {done.stdout[:200]}") from e
    state = answer.get("state")
    if state == "closed":
        raise ToyhouseError(DA_COMMENTS_HINT_CLOSED)
    if state == "error" or (state != "ready" and answer.get("error")):
        raise classify_error(answer.get("error") or "")
    if state == "challenge":
        raise ChallengeError("DeviantArt в Chrome просит подтвердить, что ты человек: вкладка открыта, нажми галочку")
    if state != "ready":
        raise ToyhouseError(f"Страница работы не загрузилась: {deviation_url}")
    try:
        items = json.loads(answer.get("result") or "[]")
    except json.JSONDecodeError as e:
        raise ToyhouseError("скрипт вернул не JSON") from e
    return comments_from_page(items if isinstance(items, list) else [], Comment)


def comments_from_page(items: list, comment_cls) -> list:
    """Строки со страницы → Comment. Родитель — ближайший выше комментарий с меньшей глубиной."""
    from .da_api import _posted_ts

    out: list = []
    stack: list[tuple[int, str]] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict) or not it.get("comment_id") or it["comment_id"] in seen:
            continue
        seen.add(it["comment_id"])
        depth = max(0, int(it.get("depth") or 0))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if (stack and depth > 0) else None
        stack.append((depth, it["comment_id"]))
        out.append(comment_cls(comment_id=str(it["comment_id"]), parent_id=parent, user=str(it.get("user") or ""),
                               posted_ts=_posted_ts(it.get("posted")), text=str(it.get("text") or "").strip(),
                               depth=depth))
    return sorted(out, key=lambda c: (c.posted_ts, c.comment_id))
