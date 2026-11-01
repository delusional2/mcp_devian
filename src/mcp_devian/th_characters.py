"""Персонажи Toyhouse: создать перса в нужной папке, отметить продажу, передать покупателю, разложить по папкам.

У Toyhouse нет API, поэтому всё делается в её собственном Chrome (как отклики в her_chrome):
скрипт открывает нужную страницу, сверяет адрес и форму, ЗАПОЛНЯЕТ поля и оставляет вкладку ей.
«Save» / «Transfer» всегда нажимает она — скрипты здесь ничего не кликают и не отправляют.

Поля форм Toyhouse отсюда проверить нельзя (Cloudflare, нужен её вход). Поэтому сначала идёт
разовый осмотр формы на её маке (inspect_js → parse_inspect, результат хранится в th_forms),
а заполнение работает только по именам полей из этого осмотра. Поменял Toyhouse форму —
заполнение честно скажет not_found и попросит осмотреть заново.

Тексты профиля — только её слова, как есть (правило 4.8 Toyhouse запрещает тексты от ИИ).
"""

import difflib
import html as html_lib
import json
import mimetypes
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import her_chrome
from .toyhouse import ToyhouseError

TH = "https://toyhou.se"
# Не подтверждено вживую: адрес формы создания и подстраницы перса. Осмотр покажет настоящие (links в результате);
# адреса edit/transfer берутся из сохранённого осмотра, эти — только запасные.
CREATE_PATH = "/~characters/create"
CREATE_URL = f"{TH}{CREATE_PATH}"
DEFAULT_SUFFIX = {"edit": "/edit", "transfer": "/ownership/transfer"}
KINDS = ("create", "edit", "transfer")
CURRENCIES = ("usd", "points", "eur", "gbp")
SALE_STATUSES = {"sale": "for sale", "trade": "for trade", "nfs": "not for sale"}
MOVE_MAX = 5           # столько вкладок за раз: «Save» на каждой жмёт она
IMAGES_MAX = 10
ATTACH_MAX_BYTES = 40 * 1024 * 1024   # больше этого в страницу не передаём: выберет сама
INSPECT_FIELDS_MAX = 200
SUBMIT_HOLD_MS = 2000   # глушилка отправки держится столько после заполнения (отправка с задержкой)
FORM_MAX_AGE_S = 30 * 86400
BUTTON = {"create": "Save", "edit": "Save", "transfer": "Transfer"}

CHAR_URL = re.compile(r"^https://toyhou\.se/(\d+)\.([^/?#\s]*)")
USERNAME = re.compile(r"^[A-Za-z0-9_.\-]{2,40}$")

_STATUS_ALIASES = {
    "sale": {"sale", "for sale", "fs", "selling", "продаю", "продажа", "на продажу", "продаётся", "продается"},
    "trade": {"trade", "for trade", "ft", "trading", "обмен", "на обмен"},
    "nfs": {"", "nfs", "not for sale", "не продаётся", "не продается", "не продаю", "нет"},
}


@dataclass
class NewCharacter:
    name: str
    images: list[str]
    folder: str = ""
    world: str = ""
    tags: list[str] = field(default_factory=list)
    price: float | None = None
    currency: str | None = None
    status: str = "nfs"      # sale | trade | nfs
    designer: str = ""       # её ник на Toyhouse
    profile: str = ""        # её текст, как есть


# --- Проверка ввода ---

def normalize_status(value: str | None) -> str:
    v = " ".join(str(value or "").lower().replace("-", " ").split())
    for key, names in _STATUS_ALIASES.items():
        if v in names or v == key:
            return key
    raise ValueError(f"Непонятный статус «{value}»: for sale / for trade / not for sale.")


def _price(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    s = "".join(str(value).strip().lstrip("$€£").split())
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", s):      # 1,500 / 1,500.50 — запятая тысяч
        s = s.replace(",", "")
    elif re.fullmatch(r"\d+,\d{1,2}", s):                    # 12,5 / 12,50 — десятичная запятая
        s = s.replace(",", ".")
    try:
        if "," in s:
            raise ValueError
        price = float(s)
    except ValueError:
        raise ValueError(f"Цена должна быть числом, а не «{value}».") from None
    if price != price or price in (float("inf"), float("-inf")):
        raise ValueError(f"Цена должна быть числом, а не «{value}».")
    return price


def _num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _folder_parts(path: str) -> list[str]:
    return [" ".join(p.split()) for p in re.split(r"\s*(?:/|>|→|»|\\)\s*", path.strip())]


def _username(value: str) -> str:
    return str(value or "").strip().lstrip("@")


def _check_image(file_path: str):
    """posting.check_image (только Загрузки, Рабочий стол, Изображения), но со словами про Toyhouse, а не DA."""
    from .posting import check_image

    path, why = check_image(file_path)
    if path is None and "DeviantArt" in why:
        why = re.sub(r"\s*—\s*DeviantArt такой не примет\.?", " — слишком большой файл, уменьши его.", why)
    return path, why


def check_character(data: dict, check_image=None) -> dict:
    """Её ввод → {"character": NewCharacter, "images": [{path,name,type,size,sha256}], "errors", "warnings"}.

    check_image(path) → (Path | None, причина) — по умолчанию posting.check_image (только Загрузки,
    Рабочий стол, Изображения). Текст профиля не меняется ни на символ.
    """
    from .posting import fingerprint

    if check_image is None:
        check_image = _check_image
    errors: list[str] = []
    warnings: list[str] = []
    name = " ".join(str(data.get("name") or "").split())
    if not name:
        errors.append("Нужно имя перса.")
    try:
        status = normalize_status(data.get("status"))
    except ValueError as e:
        errors.append(str(e))
        status = "nfs"
    try:
        price = _price(data.get("price"))
        if price is not None and price < 0:
            errors.append("Цена не может быть отрицательной.")
    except ValueError as e:
        errors.append(str(e))
        price = None
    currency = str(data.get("currency") or "").strip().lower() or None
    if currency and currency not in CURRENCIES:
        errors.append(f"Валюта: {', '.join(CURRENCIES)} (а не «{currency}»).")
    if price is not None and not currency:
        currency = "usd"
        warnings.append("Валюта не указана — ставлю USD.")
    if status == "sale" and price is None:
        warnings.append("Статус «for sale», а цены нет — покупатели будут спрашивать.")
    if status == "nfs" and price is not None:
        warnings.append("Статус «not for sale» — цену в форму не ставлю.")

    raw_images = data.get("images") or []
    raw_images = [raw_images] if isinstance(raw_images, str) else list(raw_images)
    metas: list[dict] = []
    if not raw_images:
        errors.append("Нужна хотя бы одна картинка (файл с мака).")
    elif len(raw_images) > IMAGES_MAX:
        errors.append(f"Картинок больше {IMAGES_MAX} — остальные лучше добавить в галерею потом.")
    else:
        for item in raw_images:
            path, why = check_image(str(item))
            if path is None:
                errors.append(why)
                continue
            path = Path(path).resolve()
            fp = fingerprint(path)
            metas.append({"path": str(path), "name": path.name, "size": fp["size"], "sha256": fp["sha256"],
                          "type": mimetypes.guess_type(path.name)[0] or "image/png"})

    folder = str(data.get("folder") or "").strip()
    if folder and not all(_folder_parts(folder)):
        errors.append(f"Путь папки «{folder}» с пустой частью — нужно вроде «Adopts/For sale».")
    designer = _username(data.get("designer") or "")
    if designer and not USERNAME.match(designer):
        errors.append(f"Ник дизайнера «{designer}» не похож на ник Toyhouse.")
    tags_raw = data.get("tags") or []
    tags_raw = tags_raw.split(",") if isinstance(tags_raw, str) else tags_raw
    tags: list[str] = []
    for t in tags_raw:
        t = " ".join(str(t).strip().lstrip("#").split())[:50]
        if t and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    character = NewCharacter(name=name, images=[m["path"] for m in metas], folder=folder,
                             world=" ".join(str(data.get("world") or "").split()), tags=tags,
                             price=price, currency=currency, status=status, designer=designer,
                             profile=str(data.get("profile") or "").strip())
    return {"character": character, "images": metas, "errors": errors, "warnings": warnings}


# --- Адреса ---

def character_id(url: str) -> str | None:
    m = CHAR_URL.match((url or "").strip())
    return m.group(1) if m else None


def character_base(url: str) -> str | None:
    m = CHAR_URL.match((url or "").strip())
    return f"{TH}/{m.group(1)}.{m.group(2)}" if m else None


_SITE_PATH = re.compile(r"^/~(?!account(?:/|$))[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*$")


def create_path(url: str) -> str | None:
    """Путь служебной страницы Toyhouse (/~...) из адреса формы создания; не toyhou.se, вход/аккаунт — None."""
    m = re.match(r"^https://toyhou\.se(/[^?#\s]*?)/?(?:[?#].*)?$", (url or "").strip())
    return m.group(1) if m and _SITE_PATH.match(m.group(1)) else None


def _check_path(path: str) -> str:
    if not _SITE_PATH.match(path or ""):
        raise ValueError("Адрес формы создания должен быть страницей toyhou.se/~… (не вход и не аккаунт).")
    return path


def url_pattern(kind: str, char_id: str = "", path: str = CREATE_PATH) -> str:
    """Регулярка адреса (годится и для Python, и для JS): создание — только форма создания; остальное — этот перс."""
    if kind == "create":
        return r"^https://toyhou\.se" + re.escape(_check_path(path)) + r"/?(?:[?#].*)?$"
    if not re.fullmatch(r"\d+", char_id or ""):
        raise ValueError("Нужен номер перса Toyhouse.")
    return rf"^https://toyhou\.se/{char_id}\.[^/?#]*(?:[/?#].*)?$"


def target_url(kind: str, character_url: str, form: dict | None) -> str:
    """Адрес формы edit/transfer для этого перса: путь из сохранённого осмотра (у другого перса), иначе запасной."""
    base = character_base(character_url) or ""
    seen = (form or {}).get("url") or ""
    seen_base = character_base(seen)
    if seen_base:
        return base + seen[len(seen_base):].split("?")[0].split("#")[0]
    return base + DEFAULT_SUFFIX.get(kind, "")


# --- Разбор осмотра ---

_NONE_FOLDERS = {"", "none", "(none)", "no folder", "(no folder)", "-", "—", "root", "(root)"}
# Отступ — пробелы; знаки-«ступеньки» (—, -, », ., |) — только если за ними пробел: «.hidden» — это имя.
_INDENT = re.compile(r"^(?:\s|[\-–—»›·|.>]+(?=\s))*")


def folder_paths(options: list[dict]) -> list[dict]:
    """Варианты выбора папки → [{"value", "path"}]: вложенность по отступу (или data-depth), «Adopts/For sale»."""
    rows: list[dict[str, Any]] = []
    for o in options or []:
        if not isinstance(o, dict):
            continue
        text = str(o.get("text") or "")
        label = " ".join(_INDENT.sub("", text).split())
        value = str(o.get("value") or "")
        if not value or label.lower() in _NONE_FOLDERS:
            continue
        depth = o.get("depth")
        try:
            depth = int(depth) if depth not in (None, "") else None
        except (TypeError, ValueError):
            depth = None
        lead = _INDENT.match(text)  # шаблон допускает пустое совпадение — None не бывает
        rows.append({"value": value, "label": label, "indent": len(lead.group()) if lead else 0, "depth": depth,
                     "group": " ".join(str(o.get("group") or "").split())})
    explicit = any(r["depth"] is not None for r in rows)
    ranks = {n: i for i, n in enumerate(sorted({r["indent"] for r in rows}))}
    out: list[dict] = []
    stack: list[str] = []
    for r in rows:
        depth = (r["depth"] or 0) if explicit else ranks[r["indent"]]
        stack = stack[:depth] + [r["label"]]
        path = "/".join(stack)
        out.append({"value": r["value"], "path": f"{r['group']}/{path}" if r["group"] else path})
    return out


def _norm_path(path: str) -> tuple[str, ...]:
    return tuple(p.lower() for p in _folder_parts(path))


def match_folder(query: str, folders: list[dict]) -> dict:
    """Путь папки → {"value", "path", "suggestions"}. Точное совпадение, иначе однозначный «хвост» пути."""
    want = _norm_path(query)
    keyed = [(_norm_path(f["path"]), f) for f in folders]
    exact = [f for k, f in keyed if k == want]
    if len(exact) == 1:
        return {"value": exact[0]["value"], "path": exact[0]["path"], "suggestions": []}
    if exact:  # две папки с одинаковым путём — не угадываем
        return {"value": None, "path": None, "suggestions": [f"{f['path']} (№{f['value']})" for f in exact]}
    tail = [f for k, f in keyed if len(k) > len(want) and k[-len(want):] == want]
    if len(tail) == 1:
        return {"value": tail[0]["value"], "path": tail[0]["path"], "suggestions": []}
    if tail:
        return {"value": None, "path": None, "suggestions": [f["path"] for f in tail]}
    paths = [f["path"] for f in folders]
    close = difflib.get_close_matches(query.strip(), paths, n=3, cutoff=0.6)
    if not close:
        close = [p for p in paths if p.lower() in difflib.get_close_matches(query.lower(), [x.lower() for x in paths],
                                                                            n=3, cutoff=0.5)]
    return {"value": None, "path": None, "suggestions": close}


def _key(f: dict) -> str:
    return f"{f.get('name', '')} {f.get('id', '')} {f.get('label', '')}".lower()


def _group_fields(fields: list | None) -> list[dict]:
    """Радио-кнопки одного имени — одно поле с вариантами. Значения скрытых полей не храним вообще."""
    out: list[dict] = []
    radios: dict[str, dict] = {}
    for f in fields or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        typ = str(f.get("type") or "").lower()
        tag = str(f.get("tag") or "").lower()
        if typ == "hidden":
            continue
        if typ == "radio":
            g = radios.get(f["name"])
            if not g:
                g = radios[f["name"]] = {"tag": "input", "type": "radio", "name": f["name"], "label": "", "options": []}
                out.append(g)
            g["options"].append({"value": str(f.get("value") or ""), "text": str(f.get("label") or "")})
            g["autosubmit"] = g.get("autosubmit") or bool(f.get("autosubmit"))
            g["label"] = (g["label"] + " " + str(f.get("label") or "")).strip()
            continue
        out.append({"tag": tag, "type": typ, "name": str(f["name"]), "id": str(f.get("id") or ""),
                    "label": str(f.get("label") or ""), "multiple": bool(f.get("multiple")),
                    "options": [o for o in (f.get("options") or []) if isinstance(o, dict)],
                    "widget": str(f.get("widget") or ""), "autosubmit": bool(f.get("autosubmit"))})
    return out


def _opts_text(f: dict) -> str:
    return " ".join(str(o.get("text") or "") + " " + str(o.get("value") or "") for o in f.get("options") or []).lower()


def _detect_roles(fields: list[dict], kind: str) -> dict[str, dict]:
    roles: dict[str, dict] = {}
    used: set[str] = set()

    def take(role, pred):
        for f in fields:
            if f["name"] not in used and pred(f):
                roles[role] = f
                used.add(f["name"])
                return

    is_text = lambda f: f["tag"] == "input" and f["type"] in ("text", "", "search", "number")  # noqa: E731
    is_choice = lambda f: f["tag"] == "select" or f["type"] == "radio"  # noqa: E731
    if kind == "transfer":
        take("transfer_type", lambda f: f["type"] == "radio" and re.search(r"\buser", _opts_text(f)))
        take("recipient", lambda f: is_text(f) and re.search(r"recipient|user|owner|\bto\b", _key(f)))
        return roles
    take("image", lambda f: f["type"] == "file")
    take("name", lambda f: is_text(f) and (f["name"] in ("name", "character_name", "character[name]")
                                          or re.match(r"^\s*(character\s+)?name\b", f["label"], re.I)))
    take("folder", lambda f: is_choice(f) and "folder" in _key(f))
    take("world", lambda f: "world" in _key(f))
    take("tags", lambda f: re.search(r"\btags?\b|tag\[|tags_?", _key(f)))
    take("status", lambda f: is_choice(f) and (re.search(r"sale|trade", _opts_text(f))
                                               or re.search(r"trade|market|listing", _key(f))))
    take("currency", lambda f: is_choice(f) and ("currenc" in _key(f) or re.search(r"\busd\b|\$|€|£", _opts_text(f))))
    take("price", lambda f: is_text(f) and re.search(r"price|amount|cost", _key(f)))
    take("designer", lambda f: is_text(f) and re.search(r"design|creator|credit|artist", _key(f)))
    take("profile", lambda f: f["tag"] == "textarea" and (re.search(r"profile|descr|bio|content|body|about", _key(f))
                                                          or "froala" in f["widget"]))
    return roles


ROLES = {"create": ("name", "image", "folder", "world", "tags", "status", "price", "currency", "designer", "profile"),
         "edit": ("folder", "status", "price", "currency"),
         "transfer": ("recipient", "transfer_type")}


def _kind_from_url(url: str) -> str:
    if re.match(url_pattern("create"), url or ""):
        return "create"
    return "transfer" if "transfer" in (url or "").lower() else "edit"


def _field_kind(role: str, f: dict) -> str:
    if role == "tags":
        return "tags"
    if f["type"] == "radio":
        return "radio"
    if f["tag"] == "select":
        return "select"
    if f["type"] == "file":
        return "file"
    if role == "profile" and f["tag"] == "textarea":
        return "html"
    return "text"


def parse_inspect(raw, kind: str | None = None) -> dict:
    """Результат inspect_js (строка JSON или dict) → компактное описание формы для планов.

    {"state", "kind", "url", "roles": {роль: имя поля}, "kinds": {имя: text|select|radio|tags|html|file},
     "missing": [роли без поля], "folders": [{value, path}], "options": {роль: [{value, text}]},
     "image_multiple", "submits", "links", "logged_out", "autosubmit": [поля, которые сами шлют форму],
     "truncated": осмотр видел не все поля}
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"state": "bad_result", "kind": kind}
    if not isinstance(raw, dict):
        return {"state": "bad_result", "kind": kind}
    url = str(raw.get("url") or "")
    kind = kind or _kind_from_url(url)
    base = {"kind": kind, "url": url.split("#")[0], "logged_out": bool(raw.get("logged_out")),
            "links": [x for x in (raw.get("links") or []) if isinstance(x, dict)][:30]}
    if raw.get("state") != "inspected":
        return {**base, "state": str(raw.get("state") or "bad_result")}
    forms = [f for f in raw.get("forms") or [] if isinstance(f, dict)]
    best: dict | None = None
    best_roles: dict[str, dict] = {}
    best_fields: list[dict] = []
    for f in forms:
        fields = _group_fields(f.get("fields"))
        roles = _detect_roles(fields, kind)
        if len(roles) > len(best_roles):
            best, best_roles, best_fields = f, roles, fields
    if not best or not best_roles:
        return {**base, "state": "not_found"}
    options = {r: [{"value": str(o.get("value") or ""), "text": " ".join(str(o.get("text") or "").split())}
                   for o in f["options"]] for r, f in best_roles.items() if f["options"] and r != "folder"}
    return {**base, "state": "inspected",
            "roles": {r: f["name"] for r, f in best_roles.items()},
            "kinds": {f["name"]: _field_kind(r, f) for r, f in best_roles.items()},
            "missing": [r for r in ROLES[kind] if r not in best_roles],
            "folders": folder_paths(best_roles["folder"]["options"]) if "folder" in best_roles else [],
            "options": options,
            "image_multiple": bool(best_roles.get("image", {}).get("multiple")),
            "submits": [str(s)[:40] for s in best.get("submits") or []][:5],
            "autosubmit": [f["name"] for f in best_roles.values() if f.get("autosubmit")],
            "truncated": bool(best.get("truncated")),
            "unknown_fields": [f["name"] for f in best_fields if f["name"] not in {x["name"] for x in best_roles.values()}][:20]}


# --- Сохранённые осмотры ---

SCHEMA = """
CREATE TABLE IF NOT EXISTS th_forms (
    kind TEXT PRIMARY KEY,         -- create | edit | transfer
    url TEXT,
    ts REAL NOT NULL,
    data TEXT NOT NULL             -- parse_inspect(...) как JSON
);
"""


def save_form(conn, kind: str, form: dict, now: float | None = None) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO th_forms (kind, url, ts, data) VALUES (?, ?, ?, ?)",
                 (kind, form.get("url") or "", now or time.time(), json.dumps(form, ensure_ascii=False)))
    conn.commit()


def load_form(conn, kind: str, now: float | None = None, max_age_s: float = FORM_MAX_AGE_S) -> dict | None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT ts, data FROM th_forms WHERE kind = ?", (kind,)).fetchone()
    if not row or (now or time.time()) - row[0] > max_age_s:
        return None
    return json.loads(row[1])


# --- Планы ---

def _choose(options: list[dict], key: str) -> list[dict]:
    """Вариант статуса продажи по смыслу: «for sale», но не «not for sale» и не «for sale or trade»."""
    exact = {"sale": {"for sale", "sale", "selling"}, "trade": {"for trade", "trade", "trading"},
             "nfs": {"not for sale", "nfs", "none", "not listed", "not for sale or trade", "not for trade or sale"}}[key]
    norm = [(o, " ".join(re.sub(r"[^\w\s]", " ", o["text"].lower()).split())) for o in options]
    hits = [o for o, t in norm if t in exact]
    if hits:
        return hits
    if key == "sale":
        return [o for o, t in norm if "sale" in t and "not" not in t and "trade" not in t]
    if key == "trade":
        return [o for o, t in norm if "trade" in t and "not" not in t and "sale" not in t]
    return [o for o, t in norm if t.startswith("not") or "nfs" in t]


_CURRENCY_RE = {"usd": r"\busd\b|\$|dollar", "eur": r"\beur\b|€|euro", "gbp": r"\bgbp\b|£|pound",
                "points": r"point|\bpts\b"}


def _field(form: dict, role: str, value, label: str) -> dict:
    name = form["roles"][role]
    return {"role": role, "name": name, "kind": form["kinds"][name], "value": value, "label": label}


def _need_form(form: dict | None, what: str) -> str:
    if not form or form.get("state") != "inspected":
        return (f"Форма {what} ещё не осмотрена на её маке — нужен разовый осмотр (th_character:inspect), "
                "только чтение, ничего не заполняет.")
    return ""


def _listing_fields(form: dict, status: str, price: float | None, currency: str | None,
                    problems: list[str], notes: list[str]) -> list[dict]:
    out = []
    if "status" not in form["roles"]:
        notes.append(f"Статуса продажи в форме нет — «{SALE_STATUSES[status]}» поставишь сама.")
    else:
        hits = _choose(form.get("options", {}).get("status", []), status)
        if len(hits) == 1:
            out.append(_field(form, "status", hits[0]["value"], SALE_STATUSES[status]))
        else:
            variants = ", ".join(o["text"] for o in form.get("options", {}).get("status", [])) or "—"
            problems.append(f"Не поняла, какой вариант в форме значит «{SALE_STATUSES[status]}». Варианты: {variants}.")
    if status == "nfs" or price is None:
        return out
    if "price" in form["roles"]:
        out.append(_field(form, "price", _num(price), "цена"))
    else:
        notes.append("Поля цены в форме нет — впишешь сама.")
    if "currency" in form["roles"]:
        hits = [o for o in form.get("options", {}).get("currency", [])
                if re.search(_CURRENCY_RE.get(currency or "usd", "$^"), f"{o['text']} {o['value']}", re.I)]
        if len(hits) == 1:
            out.append(_field(form, "currency", hits[0]["value"], (currency or "usd").upper()))
        else:
            variants = ", ".join(o["text"] for o in form.get("options", {}).get("currency", [])) or "—"
            problems.append(f"Валюты «{(currency or 'usd').upper()}» в форме нет (есть: {variants}).")
    elif currency and currency != "usd":
        notes.append(f"Выбора валюты в форме нет — проверь, что цена будет в {currency.upper()}.")
    return out


def _target(kind: str, url: str, char_id: str, fields: list[dict], path: str = CREATE_PATH, **extra) -> dict:
    return {"kind": kind, "url": url, "char_id": char_id, "path": path if kind == "create" else "",
            "url_re": url_pattern(kind, char_id, path), "fields": fields,
            "images": [], "image_field": None, "image_multiple": False, **extra}


def _plan(kind: str, targets: list[dict], problems: list[str], notes: list[str], form: dict | None = None,
          **extra) -> dict:
    auto = set((form or {}).get("autosubmit") or [])
    hot = sorted({f["name"] for t in targets for f in t["fields"] if f["name"] in auto})
    if hot:
        problems.append(f"Поле {', '.join(hot)} в этой форме само отправляет её при выборе — "
                        "скриптом его не трогаю, заполни эту форму сама.")
    return {"kind": kind, "ok": not problems and bool(targets), "problems": problems, "notes": notes,
            "targets": targets, **extra}


def profile_html(text: str) -> str:
    """Её текст → безопасный HTML: абзацы и переносы, всё остальное экранировано."""
    paras = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    return "".join("<p>" + "<br>".join(html_lib.escape(line) for line in p.split("\n")) + "</p>" for p in paras)


def plan_create(checked: dict, form: dict | None) -> dict:
    """Что именно вписать в форму создания (по полям из осмотра). ok=False — есть problems, заполнять нельзя."""
    ch: NewCharacter = checked["character"]
    problems = list(checked.get("errors") or [])
    notes: list[str] = []
    missing_form = _need_form(form, "создания перса")
    summary = {"name": ch.name, "folder": ch.folder, "world": ch.world, "tags": ch.tags, "status": ch.status,
               "price": ch.price, "currency": ch.currency, "designer": ch.designer, "profile": ch.profile,
               "images": [{"name": m["name"], "size": m["size"]} for m in checked.get("images") or []]}
    if missing_form or form is None:  # form=None всегда даёт missing_form; проверка — для сужения типа
        return _plan("create", [], [missing_form] + problems, notes, summary=summary, title=ch.name,
                     warnings=checked.get("warnings"))
    roles = form["roles"]
    fields: list[dict] = []
    if "name" in roles:
        fields.append(_field(form, "name", ch.name, "имя"))
    else:
        problems.append("В форме не нашла поле имени — похоже, это не та форма. Нужен новый осмотр.")
    if ch.folder:
        if "folder" not in roles:
            notes.append(f"Выбора папки в форме нет — в «{ch.folder}» переложу потом (th_character:move).")
        else:
            m = match_folder(ch.folder, form.get("folders") or [])
            if m["value"] is None:
                hint = f" Может, {' / '.join(m['suggestions'])}?" if m["suggestions"] else ""
                problems.append(f"Папки «{ch.folder}» на Toyhouse нет или их несколько.{hint} "
                                "Новую папку создай на Toyhouse сама, потом осмотрим форму заново.")
            else:
                summary["folder"] = m["path"]
                fields.append(_field(form, "folder", m["value"], m["path"]))
    if ch.world:
        if "world" not in roles:
            notes.append(f"Мира в форме создания нет — в «{ch.world}» перса добавишь на странице мира.")
        elif form["kinds"][roles["world"]] in ("select", "radio"):
            hits = [o for o in form.get("options", {}).get("world", []) if o["text"].lower() == ch.world.lower()]
            if len(hits) == 1:
                fields.append(_field(form, "world", hits[0]["value"], ch.world))
            else:
                problems.append(f"Мира «{ch.world}» среди вариантов формы нет.")
        else:
            fields.append(_field(form, "world", ch.world, ch.world))
    if ch.tags:
        if "tags" in roles:
            fields.append(_field(form, "tags", ch.tags, "теги"))
        else:
            notes.append("Поля тегов в форме нет — теги добавишь сама.")
    fields += _listing_fields(form, ch.status, ch.price, ch.currency, problems, notes)
    if ch.designer:
        if "designer" in roles:
            fields.append(_field(form, "designer", ch.designer, "дизайнер"))
        else:
            notes.append("Поля дизайнера в форме нет — кредит добавишь на странице перса.")
    if ch.profile:
        if "profile" in roles:
            kind = form["kinds"][roles["profile"]]
            fields.append(_field(form, "profile", profile_html(ch.profile) if kind == "html" else ch.profile, "профиль"))
        else:
            notes.append("Текста профиля в форме создания нет — вставишь на странице редактирования.")
    path = create_path(form.get("url") or "") or CREATE_PATH
    target = _target("create", TH + path, "", fields, path=path)
    images = checked.get("images") or []
    if images:
        if "image" in roles:
            multiple = bool(form.get("image_multiple"))
            use = images if multiple else images[:1]
            if len(images) > len(use):
                notes.append("Поле картинки берёт один файл — остальные добавишь в галерею после сохранения.")
            fit, total = [], 0
            for m in use:
                if total + int(m["size"]) > ATTACH_MAX_BYTES:
                    break
                fit.append(m)
                total += int(m["size"])
            if len(fit) < len(use):
                left = ", ".join(m["name"] for m in use[len(fit):])
                notes.append(f"Слишком тяжело передавать в страницу (больше {ATTACH_MAX_BYTES // 1048576} МБ): "
                             f"{left} выберешь сама.")
            if fit:
                target.update(images=fit, image_field=roles["image"], image_multiple=multiple)
        else:
            notes.append("Поля для картинки в форме не нашла — выберешь её сама.")
    return _plan("create", [target], problems, notes, form, summary=summary, title=ch.name,
                 warnings=checked.get("warnings") or [])


def plan_transfer(character_url: str, buyer: str, form: dict | None, name: str = "",
                  warnings: list[str] | None = None) -> dict:
    """Передача перса покупателю: вписать его ник в форму передачи. «Transfer» жмёт она."""
    problems: list[str] = []
    notes: list[str] = []
    cid = character_id(character_url)
    buyer = _username(buyer)
    if not cid:
        problems.append("Нужна ссылка на перса Toyhouse вида https://toyhou.se/12345.имя.")
    if not USERNAME.match(buyer):
        problems.append(f"Ник покупателя «{buyer}» не похож на ник Toyhouse.")
    need = _need_form(form, "передачи")
    if need:
        problems.append(need)
    summary = {"name": name, "url": character_base(character_url) or character_url, "buyer": buyer}
    title = f"{name or summary['url']} → {buyer}"
    warnings = list(warnings or [])
    if problems or form is None or cid is None:  # без формы/ссылки problems уже не пуст; это сужение типов
        return _plan("transfer", [], problems, notes, summary=summary, title=title, warnings=warnings)
    fields: list[dict] = []
    if "transfer_type" in form["roles"]:
        opts = form.get("options", {}).get("transfer_type", [])
        hits = [o for o in opts if re.search(r"\buser", f"{o['value']} {o['text']}", re.I)]
        if len(hits) > 1:
            hits = [o for o in hits if not re.search(r"off.?site|e-?mail|external|\bnon\b",
                                                     f"{o['value']} {o['text']}", re.I)]
        if len(hits) == 1:
            fields.append(_field(form, "transfer_type", hits[0]["value"], "пользователю Toyhouse"))
        else:
            variants = ", ".join(o["text"] or o["value"] for o in opts) or "—"
            warnings.append(f"Не поняла, какой вариант передачи — «пользователю Toyhouse» (есть: {variants}). "
                            "Выбери его сама перед «Transfer».")
    if "recipient" in form["roles"]:
        fields.append(_field(form, "recipient", buyer, "кому"))
    else:
        problems.append("В форме передачи не нашла поле ника получателя — нужен новый осмотр.")
    target = _target("transfer", target_url("transfer", character_url, form), cid, fields)
    return _plan("transfer", [target], problems, notes, form, summary=summary, title=title, warnings=warnings)


def plan_listing(character_url: str, status: str, price, currency: str | None, form: dict | None,
                 name: str = "") -> dict:
    """Сменить статус продажи на странице редактирования (например, после продажи — not for sale)."""
    problems: list[str] = []
    notes: list[str] = []
    cid = character_id(character_url)
    if not cid:
        problems.append("Нужна ссылка на перса Toyhouse вида https://toyhou.se/12345.имя.")
    try:
        status = normalize_status(status)
        price = _price(price)
    except ValueError as e:
        problems.append(str(e))
    need = _need_form(form, "редактирования перса")
    if need:
        problems.append(need)
    summary = {"name": name, "url": character_base(character_url) or character_url, "status": status,
               "price": price, "currency": currency}
    title = f"{name or summary['url']}: {SALE_STATUSES.get(status, status)}"
    if problems or form is None or cid is None:  # без формы/ссылки problems уже не пуст; это сужение типов
        return _plan("listing", [], problems, notes, summary=summary, title=title)
    fields = _listing_fields(form, status, price, (currency or "usd").lower() if price is not None else None,
                             problems, notes)
    if not fields and not problems:
        problems.append("В форме редактирования нечего менять для статуса продажи.")
    target = _target("edit", target_url("edit", character_url, form), cid, fields)
    return _plan("listing", [target], problems, notes, form, summary=summary, title=title)


def plan_move(character_urls: list[str], folder: str, form: dict | None) -> dict:
    """Переложить персов в папку: у каждого выбрать папку на странице редактирования (до MOVE_MAX за раз)."""
    problems: list[str] = []
    notes: list[str] = []
    urls = list(dict.fromkeys(u.strip() for u in character_urls or [] if u and u.strip()))
    bad = [u for u in urls if not character_id(u)]
    if bad:
        problems.append(f"Это не ссылки на персов Toyhouse: {', '.join(bad[:3])}.")
    if not urls:
        problems.append("Нужна хотя бы одна ссылка на перса.")
    if len(urls) > MOVE_MAX:
        problems.append(f"За раз не больше {MOVE_MAX} персов — у каждого «Save» нажимаешь ты.")
    need = _need_form(form, "редактирования перса")
    if need:
        problems.append(need)
    elif form is not None and "folder" not in form["roles"]:  # need пуст ⇒ form не None
        problems.append("На странице редактирования не нашла выбор папки — нужен новый осмотр.")
    summary = {"folder": folder, "urls": urls}
    title = f"в папку {folder}"
    if problems or form is None:  # без формы problems уже не пуст; это сужение типа
        return _plan("move", [], problems, notes, summary=summary, title=title)
    m = match_folder(folder, form.get("folders") or [])
    if m["value"] is None:
        hint = f" Может, {' / '.join(m['suggestions'])}?" if m["suggestions"] else ""
        problems.append(f"Папки «{folder}» нет или их несколько.{hint}")
        return _plan("move", [], problems, notes, summary=summary, title=title)
    summary["folder"] = m["path"]
    targets = [_target("edit", target_url("edit", u, form), character_id(u) or "",  # плохие ссылки отсеяны выше
                       [_field(form, "folder", m["value"], m["path"])]) for u in urls]
    return _plan("move", targets, problems, notes, form, summary=summary, title=f"в папку {m['path']}")


_LEDGER_URL = re.compile(r"^(?:https?://)?(?:www\.)?toyhou\.se/(\d+)\.", re.I)


def _ledger_id(url: str | None) -> str | None:
    m = _LEDGER_URL.match((url or "").strip())
    return m.group(1) if m else None


def transfer_warnings(conn, character_url: str, buyer: str) -> list[str]:
    """Сверка с журналом продаж перед передачей: оплачено ли, тот ли покупатель."""
    cid = character_id(character_url)
    buyer = _username(buyer).lower()
    if not cid:
        return []
    # В журнале ссылка лежит как её вписали: без https, с www, со старым слагом — сверяем по номеру перса.
    rows = [r for r in conn.execute("SELECT * FROM sales WHERE character_url LIKE ? AND status != 'cancelled' "
                                    "ORDER BY rowid DESC", (f"%toyhou.se/{cid}.%",)).fetchall()
            if _ledger_id(r["character_url"]) == cid]
    if not rows:
        return ["Этой продажи нет в журнале — после передачи могу записать (кто, за сколько)."]
    sale = rows[0]
    out = []
    if sale["buyer"] and sale["buyer"].lstrip("@").lower() != buyer:
        out.append(f"В журнале покупатель — {sale['buyer']}, а передаёшь {buyer}.")
    if sale["status"] == "won":
        out.append("Оплата по этой продаже ещё не отмечена — передавать до оплаты рискованно.")
    return out


# --- Предпросмотр ---

def _size(n: int) -> str:
    return f"{n / 1048576:.1f} МБ" if n >= 1048576 else f"{max(1, n // 1024)} КБ"


def _tail(plan: dict) -> list[str]:
    lines = []
    for w in plan.get("warnings") or []:
        lines.append(f"⚠ {w}")
    for n in plan.get("notes") or []:
        lines.append(f"• {n}")
    for p in plan.get("problems") or []:
        lines.append(f"✗ {p}")
    return lines


def preview_create(checked: dict, plan: dict) -> str:
    s = plan.get("summary") or {}
    t = (plan.get("targets") or [{}])[0]
    lines = ["Новый перс на Toyhouse: заполню форму создания в твоём Chrome, «Save» жмёшь сама — я ничего не сохраняю.",
             f"Имя: {s.get('name') or '—'}"]
    imgs = s.get("images") or []
    if imgs:
        names = ", ".join(f"{i['name']} ({_size(i['size'])})" for i in imgs)
        how = ("подставлю в поле файла и проверю; не выйдет — выберешь сама" if t.get("image_field")
               else "выберешь сама")
        lines.append(f"Картинка: {names} — {how}.")
    lines.append(f"Папка: {s.get('folder') or 'без папки'}")
    if s.get("world"):
        lines.append(f"Мир: {s['world']}")
    if s.get("tags"):
        lines.append(f"Теги: {', '.join(s['tags'])}")
    status = SALE_STATUSES.get(s.get("status") or "nfs", "not for sale")
    if s.get("status") != "nfs" and s.get("price") is not None:
        status += f", {_num(s['price'])} {(s.get('currency') or 'usd').upper()}"
    lines.append(f"Продажа: {status}")
    if s.get("designer"):
        lines.append(f"Дизайнер: {s['designer']}")
    if s.get("profile"):
        lines.append("Текст профиля (твой, как есть):")
        lines.append(s["profile"])
    return "\n".join(lines + _tail(plan))


def preview_transfer(plan: dict) -> str:
    s = plan.get("summary") or {}
    who = f"{s.get('name')} ({s.get('url')})" if s.get("name") else s.get("url")
    lines = [f"Передача перса {who} → {s.get('buyer')}.",
             "Впишу ник в форму передачи в твоём Chrome; «Transfer» нажимаешь сама, когда оплата точно пришла.",
             "После передачи скажи — отмечу продажу как «перс передан»."]
    return "\n".join(lines + _tail(plan))


def preview_listing(plan: dict) -> str:
    s = plan.get("summary") or {}
    status = SALE_STATUSES.get(s.get("status") or "nfs", "")
    if s.get("status") != "nfs" and s.get("price") is not None:
        status += f", {_num(s['price'])} {(s.get('currency') or 'usd').upper()}"
    lines = [f"Статус продажи {s.get('name') or s.get('url')}: {status}.",
             "Выберу это на странице редактирования в твоём Chrome; «Save» жмёшь сама."]
    return "\n".join(lines + _tail(plan))


def preview_move(plan: dict) -> str:
    s = plan.get("summary") or {}
    urls = s.get("urls") or []
    lines = [f"Перенос в папку {s.get('folder')}: {len(urls)} перс(а).",
             "У каждого открою страницу редактирования и выберу папку; «Save» на каждой вкладке жмёшь сама."]
    lines += [f"– {u}" for u in urls]
    return "\n".join(lines + _tail(plan))


# --- Скрипты страницы (выполняются в её Chrome; здесь только собираются) ---

def _prelude(kind: str, char_id: str, path: str = CREATE_PATH) -> str:
    """Общее начало: сверка адреса, вход, проверка «я человек», формы Toyhouse, события полей, стоп-отправка."""
    if kind not in KINDS:
        raise ValueError(f"kind: {', '.join(KINDS)}")
    if kind != "create" and not re.fullmatch(r"\d+", char_id or ""):
        raise ValueError("Нужен номер перса Toyhouse.")
    _check_path(path)
    return r"""
  var TH = 'https://toyhou.se', kind = """ + json.dumps(kind) + """, charId = """ + json.dumps(char_id or "") + """;
  var createPath = """ + json.dumps(path) + r""";
  function onPage() {
    if (location.origin !== TH) return false;
    if (kind === 'create') return location.pathname.replace(/\/$/, '') === createPath;
    var m = /^\/(\d+)\./.exec(location.pathname);
    return !!m && m[1] === charId;
  }
  var loggedOut = !!document.querySelector('a[href$="/~account/login"], a[href*="/~account/login?"]');
  function gate() {
    if (window._cf_chl_opt || document.getElementById('challenge-error-text') ||
        document.getElementById('challenge-form')) return 'challenge';
    if (location.origin === TH && location.pathname.indexOf('/~account/login') === 0) return 'needs_login';
    if (!onPage()) return 'wrong_page';
    return '';
  }
  // Только POST-формы, которые уходят на toyhou.se (и не формы входа/выхода).
  function thForms() {
    return Array.prototype.slice.call(document.querySelectorAll('form')).filter(function (f) {
      if ((f.getAttribute('method') || 'get').toLowerCase() !== 'post') return false;
      var a;
      try { a = new URL(f.getAttribute('action') || location.href, location.href); } catch (e) { return false; }
      return a.origin === TH && a.pathname.indexOf('/~account/') !== 0;
    });
  }
  function named(f, name) {
    return Array.prototype.slice.call(f.querySelectorAll('[name]')).filter(function (e) { return e.name === name; });
  }
  function fire(el) {
    ['input', 'change'].forEach(function (t) { el.dispatchEvent(new Event(t, {bubbles: true})); });
    if (window.jQuery) { try { window.jQuery(el).trigger('change'); } catch (e) {} }
  }
  // Пока заполняем, форма не может уйти сама (onchange с отправкой формы, автоотправка на jQuery):
  // отправку глушим и запоминаем попытку в window.__mcpThTried. Глушилка держится ещё HOLD мс после
  // заполнения (отправка с задержкой: таймер, промис, виджет), JXA перечитывает попытки до показа вкладки;
  // потом всё возвращается как было, «Save» остаётся за ней. Один общий щит на страницу, со счётчиком.
  var HOLD = """ + str(SUBMIT_HOLD_MS) + r""";
  function guard() {
    var G = window.__mcpThGuard;
    if (G) return G;
    var tried = window.__mcpThTried = window.__mcpThTried || [];
    var P = (typeof HTMLFormElement !== 'undefined') ? HTMLFormElement.prototype : null;
    var saved = P ? {s: P.submit, r: P.requestSubmit} : null;
    var jq = window.jQuery && window.jQuery.event, jqTrigger = jq ? jq.trigger : null;
    function stop(e) { tried.push('submit'); try { e.preventDefault(); e.stopImmediatePropagation(); } catch (x) {} }
    if (P) {
      P.submit = function () { tried.push('form.submit'); };
      if (saved.r) P.requestSubmit = function () { tried.push('requestSubmit'); };
    }
    if (jqTrigger) {
      jq.trigger = function (ev) {
        if (String((ev && ev.type) || ev).split('.')[0] === 'submit') { tried.push('jquery'); return; }
        return jqTrigger.apply(this, arguments);
      };
    }
    if (document.addEventListener) document.addEventListener('submit', stop, true);
    G = window.__mcpThGuard = {n: 0, tried: tried, restore: function () {
      if (P) { P.submit = saved.s; if (saved.r) P.requestSubmit = saved.r; }
      if (jqTrigger) jq.trigger = jqTrigger;
      if (document.removeEventListener) document.removeEventListener('submit', stop, true);
      window.__mcpThGuard = null;
    }};
    return G;
  }
  function noSubmit(work) {
    var G = guard(), before = G.tried.length;
    G.n++;
    function release() { if (--G.n <= 0) G.restore(); }
    try { return {value: work(), tried: G.tried.slice(before)}; }
    finally {
      if (typeof setTimeout === 'function' && HOLD > 0) setTimeout(release, HOLD); else release();
    }
  }
"""


def inspect_js(kind: str, char_id: str = "", path: str = CREATE_PATH) -> str:
    """Только чтение: формы Toyhouse на странице, их поля и варианты (значения скрытых полей не читаются)."""
    return r"""(function () {""" + _prelude(kind, char_id, path) + r"""
  var g = gate();
  if (g) return JSON.stringify({state: g});
  function clean(t) { return String(t || '').replace(/\s+/g, ' ').trim().slice(0, 80); }
  function labelOf(e) {
    var t = '';
    if (e.id) {
      var l = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(e.id) : e.id) + '"]');
      if (l) t = l.textContent;
    }
    if (!t && e.closest) { var l2 = e.closest('label'); if (l2) t = l2.textContent; }
    if (!t && e.closest) {
      var box = e.closest('.form-group, fieldset, .row');
      var l3 = box && box.querySelector('label, legend, .control-label');
      if (l3) t = l3.textContent;
    }
    return clean(t || e.getAttribute('placeholder'));
  }
  function widget(e) {
    var near = (e.className || '') + ' ' + ((e.nextElementSibling && e.nextElementSibling.className) || '');
    if (/select2/.test(near)) return 'select2';
    if (/selectize/.test(near)) return 'selectize';
    if (e.parentElement && e.parentElement.querySelector('.fr-element')) return 'froala';
    return '';
  }
  var forms = thForms().map(function (f) {
    var all = Array.prototype.slice.call(f.querySelectorAll('input, select, textarea')).filter(function (e) {
      return e.name && ['submit', 'button', 'image', 'reset'].indexOf((e.type || '').toLowerCase()) < 0;
    });
    var fields = all.slice(0, """ + str(INSPECT_FIELDS_MAX) + r""").map(function (e) {
      var d = {tag: e.tagName.toLowerCase(), type: (e.type || '').toLowerCase(), name: e.name, id: e.id || '',
               label: labelOf(e), multiple: !!e.multiple, required: !!e.required};
      if (/submit/i.test((e.getAttribute('onchange') || '') + ' ' + (e.getAttribute('oninput') || '') + ' ' +
                         (e.getAttribute('data-autosubmit') || '') + ' ' + (e.getAttribute('data-submit') || ''))) {
        d.autosubmit = true;
      }
      if (d.type === 'radio' || d.type === 'checkbox') d.value = e.value;
      if (d.type === 'file') d.accept = e.accept || '';
      if (d.tag === 'select') {
        d.options = Array.prototype.slice.call(e.options).slice(0, 400).map(function (o) {
          var p = o.parentElement;
          return {value: o.value, text: o.text, depth: o.getAttribute('data-depth') || '',
                  group: (p && p.tagName === 'OPTGROUP') ? p.label : ''};
        });
      }
      var w = widget(e);
      if (w) d.widget = w;
      return d;
    });
    var submits = Array.prototype.slice.call(f.querySelectorAll('button[type=submit], input[type=submit], button:not([type])'))
      .map(function (b) { return clean(b.textContent || b.value); });
    var action = new URL(f.getAttribute('action') || location.href, location.href);
    return {action: action.origin + action.pathname, method: 'post', submits: submits, fields: fields,
            truncated: all.length > fields.length};
  });
  var links = Array.prototype.slice.call(document.querySelectorAll('a[href]')).filter(function (a) {
    return a.href.indexOf(TH + '/') === 0 && /transfer|edit|create|folder|manage|world/i.test(a.getAttribute('href'));
  }).slice(0, 30).map(function (a) { return {href: a.href.split('#')[0], text: clean(a.textContent).slice(0, 40)}; });
  var folders = Array.prototype.slice.call(document.querySelectorAll('a.characters-folder')).slice(0, 100).map(function (a) {
    return {href: a.href, name: clean((a.querySelector('.characters-folder-name') || a).textContent)};
  });
  return JSON.stringify({state: forms.length ? 'inspected' : (loggedOut ? 'needs_login' : 'not_found'),
                         url: location.href.split('#')[0], logged_out: loggedOut, forms: forms, links: links,
                         folders_on_page: folders});
})()"""


def fill_js(kind: str, fields: list[dict], char_id: str = "", path: str = CREATE_PATH) -> str:
    """Вписать поля в ту единственную форму, где есть ВСЕ нужные имена. Ничего не нажимает и не отправляет.

    Если поле при заполнении пытается само отправить форму — отправка гасится, итог autosubmit (вкладка закрывается).
    """
    prelude = _prelude(kind, char_id, path)
    if not fields:
        raise ValueError("Нечего заполнять.")
    clean = [{"name": str(f["name"]), "kind": str(f["kind"]), "value": f["value"], "label": str(f.get("label") or "")}
             for f in fields]
    return r"""(function () {""" + prelude + """
  var fields = """ + json.dumps(clean, ensure_ascii=False) + r""";
  var g = gate();
  if (g) return JSON.stringify({state: g});
  var names = fields.map(function (f) { return f.name; });
  var full = [], best = null, bestHits = -1;
  thForms().forEach(function (f) {
    var hits = names.filter(function (n) { return named(f, n).length > 0; }).length;
    if (hits === names.length) full.push(f);
    if (hits > bestHits) { best = f; bestHits = hits; }
  });
  if (full.length !== 1) {
    if (!full.length && loggedOut) return JSON.stringify({state: 'needs_login'});
    var missing = (full.length > 1 || !best) ? names : names.filter(function (n) { return !named(best, n).length; });
    return JSON.stringify({state: 'not_found', missing: missing, ambiguous: full.length > 1});
  }
  var form = full[0];
  function res(f, ok, reason) {
    return {name: f.name, label: f.label, ok: !!ok, reason: ok ? null : (reason || 'поле не приняло значение')};
  }
  function textOf(s) {
    var doc = new DOMParser().parseFromString(s, 'text/html');
    return (doc.body.textContent || '').replace(/\s+/g, '');
  }
  function setField(f) {
    var els = named(form, f.name), el = els[0];
    if (f.kind === 'text') { el.value = String(f.value); fire(el); return res(f, el.value === String(f.value)); }
    if (f.kind === 'select') {
      var has = Array.prototype.some.call(el.options, function (o) { return o.value === f.value; });
      if (!has) return res(f, false, 'нет такого варианта');
      el.value = f.value; fire(el); return res(f, el.value === f.value);
    }
    if (f.kind === 'radio') {
      var r = els.filter(function (e) { return e.value === f.value; })[0];
      if (!r) return res(f, false, 'нет такого варианта');
      els.forEach(function (e) { e.checked = (e === r); });
      fire(r); return res(f, r.checked);
    }
    if (f.kind === 'tags') {
      var want = f.value.map(function (t) { return String(t); });
      if (el.tagName === 'SELECT') {
        want.forEach(function (t) {
          var o = Array.prototype.filter.call(el.options, function (x) {
            return x.text.trim().toLowerCase() === t.toLowerCase() || x.value.toLowerCase() === t.toLowerCase();
          })[0];
          if (!o) { o = new Option(t, t, true, true); el.appendChild(o); }
          o.selected = true;
        });
        fire(el);
        var got = Array.prototype.map.call(el.selectedOptions || [], function (o) { return o.text.trim().toLowerCase(); });
        return res(f, want.every(function (t) { return got.indexOf(t.toLowerCase()) >= 0; }));
      }
      var s = want.join(', ');
      el.value = s; fire(el); return res(f, el.value === s);
    }
    if (f.kind === 'html') {
      var inst = null;
      if (window.FroalaEditor && window.FroalaEditor.INSTANCES) {
        inst = window.FroalaEditor.INSTANCES.filter(function (e) { return e.$oel && e.$oel[0] === el; })[0] || null;
      }
      if (!inst && window.jQuery) { try { inst = window.jQuery(el).data('froala.editor') || null; } catch (e) {} }
      if (inst) {
        inst.html.set(f.value); inst.undo.saveStep(); inst.events.trigger('contentChanged');
        el.value = inst.html.get();
        return res(f, textOf(inst.html.get()) === textOf(f.value));
      }
      // Редактора не нашли: в видимое поле писать можно, а в спрятанное (его прячет редактор) — нельзя,
      // при «Save» редактор запишет туда своё пустое содержимое.
      if (el.getClientRects && !el.getClientRects().length) return res(f, false, 'редактор не принял текст — вставь сама');
      el.value = f.value; fire(el); return res(f, el.value === f.value);
    }
    return res(f, false, 'неизвестный тип поля');
  }
  var run = noSubmit(function () {
    return fields.map(function (f) {
      try { return setField(f); } catch (e) { return res(f, false, String(e).slice(0, 120)); }
    });
  });
  if (run.tried.length) return JSON.stringify({state: 'autosubmit', tried: run.tried});
  var results = run.value;
  window.__mcpThFilled = 1;
  try { form.scrollIntoView({block: 'center'}); } catch (e) {}
  var good = results.filter(function (r) { return r.ok; }).length;
  return JSON.stringify({state: good === results.length ? 'filled' : (good ? 'partial' : 'fill_failed'), results: results});
})()"""


def attach_js(kind: str, field_name: str, images: list[dict], char_id: str = "", path: str = CREATE_PATH) -> str:
    """Собрать файлы из кусков, которые JXA положил в window.__mcpThImg, и подставить в поле файла.

    Браузер разрешает это странице через DataTransfer; итог проверяется по числу, именам и размерам файлов.
    Не вышло — честно not_attached: картинку она выберет сама.
    """
    metas = [{"name": str(i["name"]), "type": str(i.get("type") or "image/png"), "size": int(i["size"])} for i in images]
    return r"""(function () {""" + _prelude(kind, char_id, path) + """
  var fieldName = """ + json.dumps(field_name) + """, metas = """ + json.dumps(metas, ensure_ascii=False) + r""";
  var g = gate();
  if (g) return JSON.stringify({state: g});
  var parts = window.__mcpThImg || [];
  window.__mcpThImg = null;
  var inputs = [];
  thForms().forEach(function (f) {
    named(f, fieldName).forEach(function (e) { if ((e.type || '').toLowerCase() === 'file') inputs.push(e); });
  });
  if (inputs.length !== 1) return JSON.stringify({state: 'not_attached', reason: 'поле файла не найдено'});
  var inp = inputs[0];
  try {
    var files = metas.map(function (m, i) {
      var bin = atob(parts[i] || ''), arr = new Uint8Array(bin.length);
      for (var k = 0; k < bin.length; k++) arr[k] = bin.charCodeAt(k);
      return new File([arr], m.name, {type: m.type});
    });
    if (files.some(function (f, i) { return f.size !== metas[i].size; })) {
      return JSON.stringify({state: 'not_attached', reason: 'файл дошёл не целиком'});
    }
    var use = inp.multiple ? files : files.slice(0, 1);
    var dt = new DataTransfer();
    use.forEach(function (f) { dt.items.add(f); });
    var run = noSubmit(function () { inp.files = dt.files; fire(inp); });
    if (run.tried.length) return JSON.stringify({state: 'not_attached', reason: 'форма пыталась отправиться сама'});
    var got = Array.prototype.slice.call(inp.files || []);
    var ok = got.length === use.length && got.every(function (f, i) { return f.size === use[i].size && f.name === use[i].name; });
    return JSON.stringify({state: ok ? 'attached' : 'not_attached', count: got.length, skipped: files.length - use.length});
  } catch (e) {
    return JSON.stringify({state: 'not_attached', reason: String(e).slice(0, 160)});
  }
})()"""


# JXA: один запуск osascript на одну страницу. Шаблон окна/вкладки — как her_chrome.REPLY_JXA.
# argv: url, регулярка адреса, скрипт страницы, режим (inspect | fill), секунд на загрузку,
# картинки JSON [{path, size}], скрипт подстановки файла, общий бюджет в секундах.
# Правила: адрес сверяется до открытия, после загрузки и перед каждой порцией картинки; кнопки не нажимаются;
# inspect закрывает вкладку и возвращает её прежнюю; fill оставляет заполненную вкладку открытой и показывает ей.
TH_FORM_JXA = r"""
ObjC.import('Foundation');
function run(argv) {
  const url = argv[0], urlRe = new RegExp(argv[1]), pageJs = argv[2], mode = argv[3];
  const loadS = Number(argv[4]), images = JSON.parse(argv[5] || '[]'), attachJs = argv[6] || '';
  const hardDeadline = Date.now() + Number(argv[7]) * 1000;
  const CHUNK = 400000;
  if (!urlRe.test(url)) return JSON.stringify({state: 'wrong_page', error: 'url'});
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
  const made = chrome.Tab({url: url});
  win.tabs.push(made);
  let id = null;
  try { id = made.id(); } catch (e) {}
  if (id === null) { const ids = win.tabs.id(); id = ids[ids.length - 1]; }
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
  if (mode !== 'fill' && beforeId !== null) activate(beforeId);
  const cf = "(window._cf_chl_opt || document.getElementById('challenge-error-text') || " +
    "document.getElementById('challenge-form'))";
  const probe = cf + " ? 'challenge' : ((document.readyState === 'complete' && " +
    "document.querySelector('form, .navbar, nav, footer, #main')) ? 'ready' : 'wait')";
  let lastError = null;
  const waitPage = () => {
    const deadline = Math.min(Date.now() + loadS * 1000, hardDeadline);
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
      if (st === 'ready') return st;
    }
    try { const tab = find(); if (tab && tab.execute({javascript: cf + " ? 1 : 0"}) === 1) return 'challenge'; } catch (e) {}
    return 'timeout';
  };
  const here = () => { const t = find(); return t ? String(t.url() || '') : ''; };
  const attach = () => {
    try {
      find().execute({javascript: "window.__mcpThImg = []; 1"});
      for (let i = 0; i < images.length; i++) {
        const data = $.NSData.dataWithContentsOfFile(images[i].path);
        if (!data || data.isNil()) return {state: 'not_attached', reason: 'файл не прочитался'};
        if (Number(data.length) !== Number(images[i].size)) return {state: 'not_attached', reason: 'файл изменился'};
        const b64 = ObjC.unwrap(data.base64EncodedStringWithOptions(0));
        find().execute({javascript: "window.__mcpThImg[" + i + "] = ''; 1"});
        for (let p = 0; p < b64.length; p += CHUNK) {
          if (Date.now() > hardDeadline) return {state: 'not_attached', reason: 'не успела'};
          if (!urlRe.test(here())) return {state: 'not_attached', reason: 'вкладка ушла со страницы'};
          find().execute({javascript: "window.__mcpThImg[" + i + "] += " + JSON.stringify(b64.slice(p, p + CHUNK)) + "; 1"});
        }
      }
      if (!urlRe.test(here())) return {state: 'not_attached', reason: 'вкладка ушла со страницы'};
      return JSON.parse(find().execute({javascript: attachJs}));
    } catch (e) { return {state: 'not_attached', reason: String(e).slice(0, 160)}; }
  };
  let result = {state: 'timeout'}, keep = false;
  try {
    const st = waitPage();
    if (st !== 'ready') {
      if (st === 'challenge') { keep = true; activate(id); chrome.activate(); }
      result = {state: st, error: lastError};
      return JSON.stringify(result);
    }
    const cur = here();
    if (/^https:\/\/toyhou\.se\/~account\/login/.test(cur)) { result = {state: 'needs_login'}; return JSON.stringify(result); }
    if (!urlRe.test(cur)) { result = {state: 'wrong_page', url: cur.slice(0, 200)}; return JSON.stringify(result); }
    result = JSON.parse(find().execute({javascript: pageJs}));
    if (mode === 'fill' && (result.state === 'filled' || result.state === 'partial')) {
      if (images.length && attachJs) result.image = attach();
      // Отправка с задержкой: ждём, пока держится глушилка, и смотрим, не пыталась ли форма уйти сама.
      delay(__HOLD_S__);
      let late = [];
      try { late = JSON.parse(find().execute({javascript: "JSON.stringify(window.__mcpThTried || [])"}) || '[]'); }
      catch (e) { late = ['unreadable']; }
      if (late.length) {
        result = {state: 'autosubmit', tried: late};
        return JSON.stringify(result);
      }
      keep = true;
      activate(id); chrome.activate();
    }
    return JSON.stringify(result);
  } catch (e) {
    result = {state: keep ? 'partial' : 'error', error: String(e)};
    return JSON.stringify(result);
  } finally {
    if (!keep && result.state !== 'tab_closed') {
      try { const tab = find(); if (tab) tab.close(); } catch (e) {}
      if (beforeId !== null) activate(beforeId);
    }
  }
}
""".replace("__HOLD_S__", str(SUBMIT_HOLD_MS / 1000 + 0.5))

MESSAGES = {
    "filled": "Форма заполнена в твоём Chrome. Проверь и нажми «{button}» сама — я ничего не сохраняю.",
    "partial": "Форма заполнена не целиком (ниже — что не вписалось). Допиши и нажми «{button}» сама.",
    "fill_failed": "Ни одно поле не приняло значение — ничего не сохранено. Похоже, форма поменялась: нужен новый осмотр.",
    "not_found": "Нужной формы на странице нет (или Toyhouse её поменял) — ничего не заполняла. "
                 "Нужен новый осмотр формы (th_character:inspect).",
    "autosubmit": "Форма пыталась отправиться сама, пока я заполняла, — я это остановила и закрыла вкладку, "
                  "ничего не сохранено. Эту форму заполни сама.",
    "needs_login": "В Chrome ты не вошла на Toyhouse: войди и попроси ещё раз. Ничего не заполняла.",
    "challenge": "Toyhouse просит подтвердить, что ты человек: вкладка открыта — нажми галочку и попроси ещё раз.",
    "wrong_page": "Вкладка открылась не на той странице Toyhouse — ничего не заполняла.",
    "timeout": "Страница Toyhouse не загрузилась вовремя — ничего не заполняла.",
    "image_changed": "Картинка поменялась после черновика — ничего не заполняла. Подготовь черновик заново.",
    "inspected": "Форма осмотрена (только чтение).",
    "bad_result": "Chrome вернул непонятный ответ — ничего не заполняла.",
}
INSPECT_NOT_FOUND = ("На этой странице нужной формы не нашла (только смотрела, ничего не трогала). "
                     "Открой настоящую форму в Chrome и дай мне её точный адрес — осмотрю её.")
# Коротко — для нескольких вкладок сразу (перенос по папкам).
TAB_STATES = {"not_found": "формы нет", "fill_failed": "поля не приняли значения", "needs_login": "нужен вход",
              "autosubmit": "форма пыталась отправиться сама — вкладку закрыла", "challenge": "проверка «я человек»",
              "wrong_page": "не та страница", "timeout": "не загрузилась", "bad_result": "непонятный ответ Chrome",
              "partial": "заполнена не целиком"}
IMAGE_MESSAGES = {
    "attached": "Картинку подставила в поле файла — проверь, что превью появилось.",
    "not_attached": "Картинку подставить не вышло: осталось выбрать картинку и нажать Save.",
}


def _budget(load_s: int, images: list[dict]) -> int:
    total = sum(int(i.get("size") or 0) for i in images)
    return load_s * 2 + 15 + SUBMIT_HOLD_MS // 1000 + 1 + int(total / 1048576 * 4)


def _call(target: dict, mode: str, page_js: str, attach: str, images: list[dict], runner, load_s: int) -> dict:
    if not re.match(target["url_re"], target["url"]):
        raise ToyhouseError("Адрес не совпал с ожидаемой страницей Toyhouse — не открываю.")
    if not her_chrome.chrome_installed():
        raise her_chrome.ChromeSetupError(her_chrome.NOT_INSTALLED_HINT)
    budget = _budget(load_s, images)
    jxa_images = json.dumps([{"path": i["path"], "size": i["size"]} for i in images])
    try:
        done = (runner or subprocess.run)(
            ["osascript", "-l", "JavaScript", "-e", TH_FORM_JXA, target["url"], target["url_re"], page_js, mode,
             str(load_s), jxa_images, attach, str(budget)],
            capture_output=True, text=True, timeout=her_chrome.LAUNCH_WAIT_S + budget + her_chrome.ERROR_EXTRA_S,
            check=False)
    except subprocess.TimeoutExpired as e:
        raise ToyhouseError("Chrome не ответил вовремя — ничего не сохранялось (вкладка могла остаться открытой).") from e
    except OSError as e:
        raise ToyhouseError(f"не получилось запустить osascript: {e}") from e
    if done.returncode != 0:
        raise her_chrome.classify_error(done.stderr or done.stdout)
    try:
        answer = json.loads(done.stdout.strip() or "{}")
    except json.JSONDecodeError as e:
        raise ToyhouseError(f"Chrome вернул что-то странное: {done.stdout[:200]}") from e
    if not isinstance(answer, dict):
        return {"state": "bad_result"}
    if answer.get("state") == "tab_closed":
        raise ToyhouseError("Вкладку Toyhouse закрыли раньше, чем страница загрузилась. Ничего не заполнено.")
    if answer.get("state") == "error":
        raise her_chrome.classify_error(answer.get("error") or "")
    return answer


def _inspect_target(kind: str, url: str) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind: {', '.join(KINDS)}")
    if kind == "create":
        path = create_path(url) if (url or "").strip() else CREATE_PATH
        if not path:
            raise ValueError("Для осмотра формы создания нужна страница Toyhouse вида https://toyhou.se/~characters/create")
        return _target("create", TH + path, "", [], path=path)
    cid = character_id(url)
    if not cid:
        raise ValueError("Для осмотра формы нужна ссылка на любого её перса: https://toyhou.se/12345.имя")
    base = character_base(url)
    url = url.strip()
    return _target(kind, base + DEFAULT_SUFFIX[kind] if url.rstrip("/") == base else url, cid, [])


def run_inspect(conn, kind: str, url: str = "", runner=None, load_s: int = 30,
                lock_wait_s: float = her_chrome.LOCK_WAIT_S, now: float | None = None) -> dict:
    """Разовый осмотр формы в её Chrome (только чтение, вкладка закрывается). Удачный — сохраняется в th_forms.

    kind: create | edit | transfer; для edit/transfer url — любой её перс (или точный адрес формы этого перса),
    для create — пусто (обычный адрес) или точный адрес формы создания на toyhou.se/~….
    Возвращает {"state", "message", "form": parse_inspect(...)}.
    """
    target = _inspect_target(kind, url)
    with her_chrome.chrome_turn(lock_wait_s):
        answer = _call(target, "inspect", inspect_js(kind, target["char_id"], target["path"] or CREATE_PATH), "",
                       [], runner, load_s)
    form = parse_inspect(answer, kind)
    state = form.get("state") or "bad_result"
    if state == "inspected":
        save_form(conn, kind, form, now)
    return {"state": state, "message": _inspect_message(form), "form": form}


def _links(form: dict, n: int = 5) -> str:
    rows = [f"{x.get('href')}" + (f" ({x['text']})" if x.get("text") else "") for x in form.get("links") or []
            if x.get("href")][:n]
    return ("Ссылки на странице: " + "; ".join(rows) + ".") if rows else ""


def _inspect_message(form: dict) -> str:
    state = form.get("state") or "bad_result"
    if state == "not_found":
        return " ".join(x for x in (INSPECT_NOT_FOUND, _links(form)) if x)
    if state != "inspected":
        return MESSAGES.get(state, MESSAGES["bad_result"]).replace("ничего не заполняла", "ничего не трогала")
    found = ", ".join(form["roles"]) or "—"
    parts = [f"{MESSAGES['inspected']} Нашла поля: {found}."]
    if form.get("missing"):
        parts.append(f"Нет: {', '.join(form['missing'])}.")
    if form.get("folders"):
        parts.append(f"Папок: {len(form['folders'])}.")
    if form.get("autosubmit"):
        parts.append(f"Сами отправляют форму (их не заполняю): {', '.join(form['autosubmit'])}.")
    if form.get("truncated"):
        parts.append(f"Полей больше {INSPECT_FIELDS_MAX} — видела не все; если чего-то нет, скажи.")
    if form.get("missing") and form.get("links"):
        parts.append(_links(form))
    return " ".join(p for p in parts if p)


def _images_unchanged(images: list[dict]) -> bool:
    from .posting import fingerprint

    for img in images:
        try:
            if fingerprint(Path(img["path"]))["sha256"] != img["sha256"]:
                return False
        except (OSError, KeyError):
            return False
    return True


_DONE = ("filled", "partial")


def run_plan(plan: dict, runner=None, load_s: int = 30, lock_wait_s: float = her_chrome.LOCK_WAIT_S) -> dict:
    """Заполнить формы по плану в её Chrome (вкладки остаются ей, кнопки жмёт она).

    Возвращает {"state", "message", "results": [{"url", "state", "fields": [...], "image": {...}}]}.
    state: filled | partial | fill_failed | not_found | autosubmit | needs_login | challenge | wrong_page | timeout
    | image_changed. Несколько вкладок и хоть одна заполнена — partial (если не все). Ошибка Chrome до первой
    заполненной вкладки — исключение (ToyhouseError / her_chrome.ChromeSetupError); Chrome занят — BrowserBusy.
    """
    if not plan.get("ok"):
        raise ValueError("В плане есть проблемы — заполнять нельзя: " + "; ".join(plan.get("problems") or []))
    targets = plan["targets"]
    images = [i for t in targets for i in t.get("images") or []]
    if images and not _images_unchanged(images):
        return {"state": "image_changed", "message": MESSAGES["image_changed"], "results": []}
    results: list[dict] = []
    with her_chrome.chrome_turn(lock_wait_s):
        for t in targets:
            path = t.get("path") or CREATE_PATH
            page = fill_js(t["kind"], t["fields"], t["char_id"], path)
            att = attach_js(t["kind"], t["image_field"], t["images"], t["char_id"], path) if t.get("image_field") else ""
            try:
                answer = _call(t, "fill", page, att, t["images"] if att else [], runner, load_s)
            except ToyhouseError as e:
                if not any(r["state"] in _DONE for r in results):
                    raise
                results.append({"url": t["url"], "state": "error", "fields": [], "missing": [], "image": None,
                                "error": str(e)[:200]})
                break
            results.append({"url": t["url"], "state": answer.get("state") or "bad_result",
                            "fields": answer.get("results") or [], "missing": answer.get("missing") or [],
                            "image": answer.get("image")})
            if results[-1]["state"] in ("needs_login", "challenge"):
                break
    states = [r["state"] for r in results]
    if len(results) == len(targets) and all(s == "filled" for s in states):
        state = "filled"
    elif any(s in _DONE for s in states):
        state = "partial"
    else:
        state = next((s for s in states if s != "filled"), "bad_result")
    return {"state": state, "message": _fill_message(plan, results, state), "results": results}


def _fill_message(plan: dict, results: list[dict], state: str) -> str:
    button = BUTTON.get(plan["targets"][0]["kind"], "Save")
    n = len(plan["targets"])
    done = sum(r["state"] in _DONE for r in results)
    if n > 1 and done:
        parts = [f"Заполнено вкладок: {done} из {n} — проверь каждую и нажми «{button}» сама; я ничего не сохраняю."]
    else:
        parts = [MESSAGES.get(state, MESSAGES["bad_result"]).format(button=button)]
        if n > 1:
            parts.append(f"Заполнено вкладок: 0 из {n}.")
    if n > 1:
        for r in results:
            if r["state"] != "filled":
                why = r.get("error") if r["state"] == "error" else TAB_STATES.get(r["state"], r["state"])
                parts.append(f"{r['url']}: {why}.")
        for t in plan["targets"][len(results):]:
            parts.append(f"{t['url']}: не открывала.")
    for r in results:
        bad = [f"{f.get('label') or f.get('name')} ({f.get('reason')})" for f in r["fields"] if not f.get("ok")]
        if bad:
            parts.append("Не вписалось: " + ", ".join(bad) + ".")
        if r["missing"]:
            parts.append("Не нашла поля: " + ", ".join(r["missing"]) + ".")
        if r["state"] in _DONE and plan["targets"][0].get("image_field"):
            img = (r.get("image") or {}).get("state") or "not_attached"
            parts.append(IMAGE_MESSAGES.get(img, IMAGE_MESSAGES["not_attached"]))
    return " ".join(parts)
