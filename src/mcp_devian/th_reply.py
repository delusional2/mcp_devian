"""Отклики в LF-треды Toyhouse: её шаблон, подбор персов, скрипты для формы ответа.

Текст отклика — всегда её собственный шаблон слово в слово (правило 4.8 Toyhouse
запрещает тексты, написанные ИИ). Здесь только подставляются поля и всё
экранируется, чтобы в пост не попал ни один скрипт.
"""

import html as html_lib
import json
import re

from .classify import keyword_hits

SLOTS = ("name", "url", "price", "note")
SLOT_RE = re.compile(r"\{(\w*)\}")
TOYHOUSE_URL = re.compile(r"^https://toyhou\.se/[\w\-./~%?=&#]*$")
MARKER_LEN = 40


def _check_slots(template: str) -> list[str]:
    used = SLOT_RE.findall(template)
    unknown = sorted({s for s in used if s not in SLOTS})
    if unknown:
        allowed = " ".join("{" + s + "}" for s in SLOTS)
        raise ValueError(f"В шаблоне неизвестные поля: {', '.join('{' + u + '}' for u in unknown)}. Можно: {allowed}")
    return used


def _link(url: str) -> str:
    safe = html_lib.escape(url, quote=True)
    # Ссылкой делаем только адреса Toyhouse: всё остальное — просто текст.
    if TOYHOUSE_URL.match(url):
        return f'<a href="{safe}">{safe}</a>'
    return safe


def _sanitize(markup: str) -> str:
    """Подстраховка: в готовом HTML не должно быть скриптов и обработчиков событий."""
    markup = re.sub(r"<\s*script\b.*?<\s*/\s*script\s*>", "", markup, flags=re.I | re.S)
    markup = re.sub(r"<\s*/?\s*script\b[^>]*>", "", markup, flags=re.I)
    markup = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", markup, flags=re.I)
    markup = re.sub(r"javascript\s*:", "", markup, flags=re.I)
    return markup


def _render_line(line: str, values: dict[str, str]) -> tuple[str, str]:
    """(html, text) одной строки шаблона."""
    out_html, out_text, pos = [], [], 0
    for m in SLOT_RE.finditer(line):
        literal = line[pos:m.start()]
        out_html.append(html_lib.escape(literal))
        out_text.append(literal)
        slot, value = m.group(1), values[m.group(1)]
        out_html.append(_link(value) if slot == "url" else html_lib.escape(value).replace("\n", "<br>"))
        out_text.append(value)
        pos = m.end()
    out_html.append(html_lib.escape(line[pos:]))
    out_text.append(line[pos:])
    return "".join(out_html), "".join(out_text)


def render_reply(template: str, character: dict, note: str = "") -> dict:
    """Её шаблон + поля перса → {"html", "text", "marker"}.

    marker — кусок текста без ссылки, по которому потом проверяем, что пост появился.
    """
    if not (template or "").strip():
        raise ValueError("Шаблон пустой: попроси её прислать текст отклика своими словами.")
    used = _check_slots(template)
    values = {
        "name": str(character.get("name") or "").strip(),
        "url": str(character.get("url") or "").strip(),
        "price": str(character.get("price") or "").strip(),
        "note": (note or "").strip(),
    }
    missing = [s for s in dict.fromkeys(used) if s != "note" and not values[s]]
    if missing:
        raise ValueError(f"Для шаблона не хватает: {', '.join(missing)} — заполни это у перса.")

    paragraphs_html, lines_text = [], []
    for para in re.split(r"\n\s*\n", template.strip()):
        rendered = [_render_line(line, values) for line in para.split("\n")]
        paragraphs_html.append("<p>" + "<br>".join(h for h, _ in rendered) + "</p>")
        lines_text.append("\n".join(t for _, t in rendered))
    text = "\n\n".join(lines_text)
    plain = " ".join((text.replace(values["url"], " ") if values["url"] else text).split())
    return {"html": _sanitize("".join(paragraphs_html)), "text": text, "marker": plain[:MARKER_LEN].strip()}


def _forms(term: str) -> list[str]:
    """Слово и его неправильное множественное: wolf → wolves, puppy → puppies."""
    forms = [term]
    low = term.lower()
    if low.endswith("fe"):
        forms.append(term[:-2] + "ves")
    elif low.endswith("f"):
        forms.append(term[:-1] + "ves")
    elif low.endswith("y") and len(low) > 2 and low[-2] not in "aeiou":
        forms.append(term[:-1] + "ies")
    return forms


def match_characters(title: str, snippet: str | None, characters: list[dict]) -> list[tuple[dict, list[str]]]:
    """Какие её персы подходят к треду: по тегам и имени в названии и первом посте."""
    found = []
    for ch in characters:
        terms = [t for t in (ch.get("tags") or []) if isinstance(t, str) and t.strip()]
        if ch.get("name"):
            terms.append(str(ch["name"]))
        hits = [t for t in terms if keyword_hits(_forms(t), title or "", snippet or "")]
        if hits:
            found.append((ch, hits))
    found.sort(key=lambda pair: len(pair[1]), reverse=True)
    return found


_LF_PART = re.compile(r"\b(?:lf|looking\s+for|seeking|iso)\b\s*[:\-]?\s*(.+?)(?=\||\bo(?:f|ffering)?\s*:|$)", re.I)
_NOT_WANTED = re.compile(r"\bno\s+(?!offers?\b|ping)([a-z][a-z\-]{2,})\b", re.I)
_BUDGET = re.compile(r"(?:budget|up\s+to|under|max(?:imum)?)\s*(?:of\s*)?[:\-]?\s*"
                     r"(\$\s?\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s?(?:\$|usd|pts|points|€|eur))", re.I)
# «Закрыт» смотрим только в названии: в первом посте «commissions closed» — про другое.
CLOSED_TITLE = re.compile(r"\[\s*closed\s*\]|\(\s*closed\s*\)|\bclosed\b|\bnot\s+looking\b|\bfound\b", re.I)
RULES = [
    (re.compile(r"\bno\s+offers?\b|\bnot\s+(?:taking|accepting)\s+offers?\b|\boffers?\s+closed\b", re.I),
     "просят без предложений (no offers)"),
    (re.compile(r"\b(?:\$|usd|money|cash|paypal)\s*only\b|\bonly\s+(?:\$|usd|money|cash|paypal)", re.I),
     "платят только деньгами"),
    (re.compile(r"\btrades?\s+only\b|\bonly\s+trades?\b", re.I), "только обмен"),
    (re.compile(r"\bart\s+only\b|\bonly\s+(?:for\s+)?art\b", re.I), "только за арт"),
    (re.compile(r"\b(?:pts|points)\s+only\b|\bonly\s+(?:pts|points)\b", re.I), "только за поинты"),
]


def op_restrictions(title: str, snippet: str | None) -> list[str]:
    """Явные условия автора треда, чтобы показать их ей рядом с черновиком. Осторожно: только очевидное."""
    blob = f"{title or ''}\n{snippet or ''}"
    found: list[str] = []
    m = _LF_PART.search(title or "")
    if m and m.group(1).strip():
        found.append(f"ищут: {' '.join(m.group(1).split())[:80]}")
    if CLOSED_TITLE.search(title or "") and not RULES[0][0].search(title or ""):
        found.append("тред, похоже, закрыт")
    for pattern, label in RULES:
        if pattern.search(blob) and label not in found:
            found.append(label)
    budget = _BUDGET.search(blob)
    if budget:
        found.append(f"бюджет: до {' '.join(budget.group(1).split())}")
    for word in dict.fromkeys(w.lower() for w in _NOT_WANTED.findall(blob)):
        found.append(f"не хотят: {word}")
    return found


# --- Скрипты для страницы треда (выполняются в её Chrome) ---

# Общие куски: как найти форму ответа и понять, залогинена ли она.
_FIND_FORM = r"""
  var forms = Array.prototype.slice.call(document.querySelectorAll('form')).filter(function (f) {
    return f.querySelector('.fr-element, textarea');
  });
  var form = forms.length ? forms[forms.length - 1] : null;
  var loggedOut = !!document.querySelector('a[href$="/~account/login"], a[href*="/~account/login?"]');
  var closed = !!document.querySelector('.thread-locked, .forum-thread-locked, .locked-thread, .fa-lock');
"""

INSPECT_REPLY_FORM_JS = r"""(function () {""" + _FIND_FORM + r"""
  var out = {present: !!form, logged_out: loggedOut, closed_marker: closed};
  var next = document.querySelector('.pagination a[rel=next]');
  out.on_last_page = !next;
  var user = document.querySelector('.navbar .display-user a, .navbar .user-name-badge, .navbar a[href*="/~account/settings"]');
  out.username_guess = user ? user.textContent.replace(/\s+/g, ' ').trim() : null;
  if (form) {
    var fr = form.querySelector('.fr-element');
    var ta = form.querySelector('textarea');
    out.action = form.getAttribute('action');
    out.method = (form.getAttribute('method') || 'get').toLowerCase();
    out.textarea = ta ? {name: ta.name || null, id: ta.id || null} : null;
    out.editor = fr ? 'froala' : (ta ? 'textarea' : null);
    out.froala_api = (window.FroalaEditor && window.FroalaEditor.INSTANCES) ? 'FroalaEditor' :
      ((window.jQuery && window.jQuery.fn && window.jQuery.fn.froalaEditor) ? 'jquery' : null);
    var btn = form.querySelector('button[type=submit], input[type=submit]');
    out.submit = btn ? {tag: btn.tagName.toLowerCase(), text: (btn.textContent || btn.value || '').trim(),
                        name: btn.name || null} : null;
    // Только имена скрытых полей, без значений: в значениях может быть токен.
    out.hidden_inputs = Array.prototype.slice.call(form.querySelectorAll('input[type=hidden]'))
      .map(function (i) { return i.name; });
    out.ic_ooc = /\bOOC\b|\bIC\b/.test(form.textContent) ||
      !!form.querySelector('[name*=character], [name*=ic_], select[name*=character]');
  }
  return JSON.stringify(out);
})()"""


def fill_reply_js(html: str, submit: bool) -> str:
    """Скрипт: вставить её отклик в форму ответа и (если submit) один раз нажать «отправить»."""
    content = json.dumps(html)  # JSON-строка — безопасный JS-литерал, без подстановки в код
    return r"""(function () {
  var html = """ + content + """, doSubmit = """ + ("true" if submit else "false") + """;
""" + _FIND_FORM + r"""
  function text(s) {
    var doc = new DOMParser().parseFromString(s, 'text/html');
    return (doc.body.textContent || '').replace(/\s+/g, ' ').trim();
  }
  if (!form) {
    if (loggedOut) return JSON.stringify({state: 'not_logged_in'});
    if (closed) return JSON.stringify({state: 'closed'});
    return JSON.stringify({state: 'no_form'});
  }
  if (window.__mcpDevianSubmitted) return JSON.stringify({state: 'already_submitted'});
  var fr = form.querySelector('.fr-element'), ta = form.querySelector('textarea'), inst = null;
  if (fr && window.FroalaEditor && window.FroalaEditor.INSTANCES) {
    inst = window.FroalaEditor.INSTANCES.filter(function (e) {
      return e.el === fr || (e.$el && e.$el[0] === fr) || (ta && e.$oel && e.$oel[0] === ta);
    })[0] || null;
  }
  if (!inst && fr && ta && window.jQuery) { inst = window.jQuery(ta).data('froala.editor') || null; }
  try {
    if (inst) {
      inst.html.set(html);
      inst.undo.saveStep();
      inst.events.trigger('contentChanged');
      if (ta) ta.value = inst.html.get();
    } else if (fr) {
      fr.innerHTML = html;
      fr.dispatchEvent(new Event('input', {bubbles: true}));
      if (ta) { ta.value = html; ta.dispatchEvent(new Event('change', {bubbles: true})); }
    } else if (ta) {
      ta.value = html;
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      ta.dispatchEvent(new Event('change', {bubbles: true}));
    } else {
      return JSON.stringify({state: 'fill_failed', reason: 'нет поля для текста'});
    }
  } catch (e) {
    return JSON.stringify({state: 'fill_failed', reason: String(e)});
  }
  var got = inst ? inst.html.get() : (fr ? fr.innerHTML : ta.value);
  var want = text(html).slice(0, 40);
  if (text(got).indexOf(want) < 0) return JSON.stringify({state: 'fill_failed', reason: 'текст не встал в поле'});
  form.scrollIntoView({block: 'center'});
  if (!doSubmit) return JSON.stringify({state: 'filled', editor: inst ? 'froala' : (fr ? 'froala-dom' : 'textarea')});
  var btn = form.querySelector('button[type=submit], input[type=submit]') ||
    Array.prototype.slice.call(form.querySelectorAll('button')).filter(function (b) {
      return /post|reply|submit|ответ|отправ/i.test(b.textContent || '');
    })[0];
  if (!btn) return JSON.stringify({state: 'no_submit_button'});
  window.__mcpDevianSubmitted = true;  // второй вызов на этой странице ничего не отправит
  btn.click();
  return JSON.stringify({state: 'submitted'});
})()"""


def verify_post_js(marker: str, username: str) -> str:
    """Скрипт: есть ли на странице пост от неё с этим текстом. Ссылка на него — если нашли."""
    return r"""(function () {
  var marker = """ + json.dumps(" ".join((marker or "").split())) + """, user = """ + json.dumps(
        (username or "").strip().lower()) + r""";
  var posts = Array.prototype.slice.call(document.querySelectorAll('.forum-post'));
  for (var i = posts.length - 1; i >= 0; i--) {
    var p = posts[i];
    var author = p.querySelector('.forum-post-user-badge a, .display-user a');
    var name = author ? author.textContent.replace(/\s+/g, ' ').trim().toLowerCase() : '';
    var body = p.querySelector('.user-content');
    var content = body ? body.textContent.replace(/\s+/g, ' ').trim() : '';
    if ((!user || name === user) && marker && content.indexOf(marker) >= 0) {
      var link = p.querySelector('a.forum-post-permalink');
      var anchor = p.querySelector('a[id^="post-"]');
      var permalink = link ? link.href : (anchor ? location.href.split('#')[0] + '#' + anchor.id : location.href);
      return JSON.stringify({found: true, permalink: permalink});
    }
  }
  return JSON.stringify({found: false, permalink: null});
})()"""
