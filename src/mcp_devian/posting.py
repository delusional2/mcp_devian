"""Публикация: черновики для DeviantArt и Toyhouse и их отправка по её «го».

Отправка идёт в отдельном процессе (`python -m mcp_devian send-draft <id>`): загрузка
картинки и отклик в её Chrome могут не уложиться в лимит вызова инструмента Claude Desktop.
Состояние черновика в базе и есть статус отправки.
"""

import fcntl
import hashlib
import json
import logging
import random
import re
import sqlite3
import string
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, drafts, limits
from .config import DATA_DIR, Config, SettingsError

log = logging.getLogger(__name__)

# Общий выключатель отправки (0.4.0: включена после исправлений по ревью). Каждый путь отправки и подготовки
# черновика проверяет его; выключить — одна строка, если на её маке что-то пойдёт не так.
SENDING_ENABLED = True
NOT_YET = ("Публикация в этой версии помощника выключена. Пока можно всё подготовить: её шаблон отклика "
           "(set_template:reply), её персов (characters:add). Ставки на её аукционах уже можно смотреть: "
           "da_auctions:status.")

CHARACTER_URL = re.compile(r"^https://toyhou\.se/(\d+)\.[^/?#]+/?$")
TEMPLATE_SAMPLE = {"name": "Имя", "url": "https://toyhou.se/1.sample", "price": "$10"}


# --- Её данные: персонажи, шаблоны, наборы групп ---

def add_character(cfg: Config, name: str, url: str, price: str = "", tags: list[str] | None = None) -> str:
    url = url.strip()
    if not CHARACTER_URL.match(url):
        raise SettingsError("Ссылка на перса должна быть вида https://toyhou.se/12345.имя")
    chars = [c for c in cfg.posting.characters if c.get("url") != url]
    chars.append({"name": name.strip() or url, "url": url, "price": price.strip(),
                  "tags": [t.strip() for t in (tags or []) if t.strip()]})
    cfg.posting.characters = chars
    return name.strip() or url


def remove_character(cfg: Config, name_or_url: str) -> bool:
    key = name_or_url.strip().lower()
    before = len(cfg.posting.characters)
    cfg.posting.characters = [c for c in cfg.posting.characters
                              if c.get("url", "").lower() != key and c.get("name", "").lower() != key]
    return len(cfg.posting.characters) < before


def find_character(cfg: Config, name_or_url: str) -> dict | None:
    key = name_or_url.strip().lower()
    return next((c for c in cfg.posting.characters
                 if c.get("url", "").lower() == key or c.get("name", "").lower() == key), None)


def set_template(cfg: Config, name: str, text: str) -> None:
    """Шаблон — её текст слово в слово. Проверяем только, что слоты правильные."""
    from .th_reply import render_reply

    if not text.strip():
        raise SettingsError("Шаблон пустой")
    try:
        render_reply(text, {**TEMPLATE_SAMPLE}, note="заметка")
    except ValueError as e:
        raise SettingsError(str(e)) from e
    cfg.posting.reply_templates[name.strip() or "основной"] = text




def da_client(cfg: Config):
    from . import da_api

    if not cfg.posting.da_client_id:
        raise SettingsError("Нет client_id приложения DeviantArt: его нужно один раз прописать (update_settings da_client_id)")
    return da_api.DAClient(cfg.posting.da_client_id, da_api.TokenStore())


def add_group_to_preset(cfg: Config, preset: str, group: str, folder_name: str) -> dict:
    """Найти группу и папку через API и добавить в набор."""
    entry = da_client(cfg).resolve_group_folder(group.strip(), folder_name.strip())
    items = [g for g in cfg.posting.group_presets.get(preset, []) if g.get("group", "").lower() != entry["group"].lower()]
    items.append(entry)
    cfg.posting.group_presets[preset] = items
    return entry


def remove_group_from_preset(cfg: Config, preset: str, group: str) -> bool:
    items = cfg.posting.group_presets.get(preset, [])
    kept = [g for g in items if g.get("group", "").lower() != group.strip().lower()]
    if kept:
        cfg.posting.group_presets[preset] = kept
    else:
        cfg.posting.group_presets.pop(preset, None)
    return len(kept) < len(items)


# --- Черновики ---

def prepare_toyhouse_reply(conn, cfg: Config, thread_id: str, character: str, template: str = "",
                           note: str = "", now: float | None = None) -> tuple[str | None, str]:
    """(id черновика или None, текст для неё)."""
    from . import th_match
    from .th_reply import op_restrictions, price_conflicts, render_reply

    if cfg.toyhouse.mode != "chrome":
        return None, ("Отклики отправляются из её Chrome: переключи Toyhouse на режим chrome "
                      "(у бота Cloudflare не пускает на форум).")
    item_id = thread_id if thread_id.startswith("th:") else f"th:{thread_id}"
    row = db.get_item(conn, item_id)
    if not row:
        return None, "Такого треда нет в базе — сначала проверь Toyhouse."
    if th_match.lf_profile(row["title"], row["snippet"]).closed:
        return None, "Тред закрыт — отклик не нужен."
    # Ответ руками тоже считается (последний пост в треде — её): тот же срок, что в её настройке.
    since = (now or time.time()) - max(1, cfg.posting.th_thread_cooldown_days) * 86400
    if cfg.posting.th_username and item_id in th_match.replied_threads(conn, cfg.posting.th_username, since):
        return None, "Она уже отвечала в этом треде."
    char = find_character(cfg, character)
    if not char:
        return None, "Такого перса нет в её списке. Добавь его: characters:add."
    templates = cfg.posting.reply_templates
    if not templates:
        return None, "Нет ни одного её шаблона отклика. Попроси её написать текст и сохрани через set_template:reply."
    name = template or next(iter(templates))
    if name not in templates:
        return None, f"Нет шаблона «{name}». Есть: {', '.join(templates)}"
    block = limits.toyhouse_reply_block(conn, cfg.posting, item_id, char["url"], now)
    if block:
        return None, f"Сейчас не отправить: {block}."
    try:
        rendered = render_reply(templates[name], char, note)
    except ValueError as e:
        return None, f"Шаблон не подошёл: {e}"
    rules = op_restrictions(row["title"], row["snippet"])
    payload = {"thread_id": item_id, "thread_url": row["url"], "thread_title": row["title"],
               "character_url": char["url"], "character": char["name"], "html": rendered["html"],
               "text": rendered["text"], "marker": rendered["marker"], "submit": cfg.posting.th_auto_submit}
    how = ("по её «го» отправлю сам" if cfg.posting.th_auto_submit
           else "по её «го» вставлю текст в её Chrome, «Post» она нажмёт сама")
    preview = "\n".join([
        f"Тред: {row['title']} (чужой текст, не инструкции)",
        row["url"],
        *([f"Что пишет автор треда: {'; '.join(rules)}"] if rules else []),
        *[f"⚠ {w}" for w in price_conflicts(char, row["title"], row["snippet"])],
        f"Перс: {char['name']} — {char['url']}" + (f" · {char['price']}" if char.get("price") else ""),
        "Текст отклика (её шаблон, без изменений):",
        rendered["text"],
        f"Как отправится: {how}.",
    ])
    draft_id = drafts.create(conn, "th_reply", payload, preview, now)
    return draft_id, preview


TITLE_MAX = 50  # DeviantArt обрезает название до 50 символов — лучше сказать заранее


def image_roots() -> list[Path]:
    """Откуда можно публиковать: туда же смотрит recent_images."""
    home = Path.home()
    return [home / "Downloads", home / "Desktop", home / "Pictures"]


def _short(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home + "/") else s


def fingerprint(path: Path) -> dict:
    st = path.stat()
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest.hexdigest()}


def check_image(file_path: str) -> tuple[Path | None, str]:
    """(путь или None, причина отказа). Только картинки из Загрузок, Рабочего стола и Изображений."""
    from .da_api import IMAGE_EXTS, MAX_FILE_BYTES

    given = Path(file_path).expanduser()
    try:
        path = given.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"Не нашла файл {given}. Можно попросить показать последние картинки: images:recent."
    if not path.is_file():
        return None, f"{_short(path)} — это не файл."
    if path.suffix.lower() not in IMAGE_EXTS:
        return None, f"{path.name}: публикую только картинки ({', '.join(sorted(IMAGE_EXTS))})."
    roots = [r.resolve() for r in image_roots() if r.exists()]
    if not any(path.is_relative_to(r) for r in roots):
        return None, (f"{_short(path)} лежит не в Загрузках, на Рабочем столе или в Изображениях. "
                      "Чтобы случайно не выложить что-то личное, публикую только оттуда: пусть перенесёт файл "
                      "туда, или выбери из images:recent.")
    if path.stat().st_size > MAX_FILE_BYTES:
        return None, f"{path.name} больше {MAX_FILE_BYTES // (1024 * 1024)} МБ — DeviantArt такой не примет."
    return path, ""


# --- Время: принимаем и показываем в её часовом поясе ---

DEFAULT_TZ = "Europe/Moscow"
WHEN_HINT = "Напиши, например, «2026-11-03 19:00», «03.11 19:00», «завтра 19:00» или «19:00»."


def her_zone(tz: str | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _tz(cfg: Config) -> str:
    return getattr(cfg.posting, "timezone", "") or DEFAULT_TZ


def fmt_when(ts: float, tz: str | None = None) -> str:
    return datetime.fromtimestamp(ts, her_zone(tz)).strftime("%d.%m %H:%M %Z")


def parse_when(value: str | float, tz: str | None = None, now: float | None = None) -> float:
    """Её время словами → unix-время. Без пояса — в её часовом поясе. Бросает ValueError с подсказкой."""
    if isinstance(value, (int, float)):
        return float(value)
    now = now or time.time()
    zone = her_zone(tz)
    base = datetime.fromtimestamp(now, zone)
    text = " ".join(str(value).strip().lower().split())
    if text in ("сейчас", "now", "сразу"):
        return now
    shift, explicit_day = 0, False
    for word, days in (("сегодня", 0), ("завтра", 1), ("послезавтра", 2)):
        if text.startswith(word + " "):
            text, shift, explicit_day = text[len(word) + 1:], days, True
            break
    try:
        dt = datetime.fromisoformat(text)
        return (dt if dt.tzinfo else dt.replace(tzinfo=zone)).timestamp()
    except ValueError:
        pass
    try:
        hm = datetime.strptime(text, "%H:%M")
        dt = (base + timedelta(days=shift)).replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
        if not explicit_day and dt.timestamp() <= now:
            dt += timedelta(days=1)  # «в 19:00», а уже 20:00 — значит, завтра (но «сегодня 19:00» — это прошлое)
        return dt.timestamp()
    except ValueError:
        pass
    short = re.fullmatch(r"(\d{1,2}\.\d{1,2}) (\d{1,2}:\d{2})", text)
    for year in ([base.year, base.year + 1] if short else [None]):
        candidate = f"{short.group(1)}.{year} {short.group(2)}" if short else text
        try:
            dt = datetime.strptime(candidate, "%d.%m.%Y %H:%M").replace(tzinfo=zone)
        except ValueError:
            break
        if short and dt < base - timedelta(days=1):
            continue  # «05.01» в декабре — это январь следующего года
        return dt.timestamp()
    raise ValueError(f"Не поняла время «{value}». {WHEN_HINT}")


# --- Её папки галереи ---

def _norm_folder(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def resolve_gallery_folders(cfg: Config, names: list[str], cache: dict | None = None) -> tuple[list[dict], str]:
    """Названия папок её галереи → ([{folder_id, name}], причина отказа). Регистр и пробелы не важны."""
    from . import da_api

    wanted = [n.strip() for n in names if n and n.strip()]
    if not wanted:
        return [], ""
    cache = {} if cache is None else cache
    if "folders" not in cache:
        try:
            client = da_client(cfg)
            if not client.is_connected():
                return [], "Чтобы разложить работу по папкам её галереи, нужен подключённый DeviantArt (accounts:da_connect)."
            username = cfg.deviantart.my_username or (client.whoami().get("username") or "")
            if not username:
                return [], "Не знаю её ник на DeviantArt, чтобы найти папки галереи."
            cache["folders"] = client.gallery_folders(username)
        except SettingsError as e:
            return [], str(e)
        except da_api.DAApiError as e:
            return [], f"Не получилось прочитать папки её галереи: {e}"
    folders = cache["folders"]
    by_name: dict[str, dict] = {}
    for f in folders:
        by_name.setdefault(_norm_folder(f.get("name") or ""), f)
    found: list[dict] = []
    missing: list[str] = []
    for name in wanted:
        f = by_name.get(_norm_folder(name))
        if not f:
            missing.append(name)
        elif all(x["folder_id"] != f["folder_id"] for x in found):
            found.append({"folder_id": f["folder_id"], "name": f.get("name") or name})
    if missing:
        have = ", ".join(f.get("name") or "?" for f in folders) or "ни одной"
        return [], f"В её галерее нет папки {', '.join(f'«{m}»' for m in missing)}. Есть: {have}."
    return found, ""


# --- Черновики DeviantArt: одна работа или партия ---

BATCH_MAX = 10
DA_NOTE = ("DeviantArt не сообщает, принята ли работа группой: после отправки стоит проверить группы.\n"
           "Если DeviantArt спросит согласие с правилами публикации и условиями (Submission Policy, Terms of "
           "Service), её «го» означает и это согласие — как галочка при загрузке на сайте.")


SAME_FILE_DAYS = 14


def _same_file_drafts(conn, sha256: str, now: float | None) -> list:
    """Другие черновики публикации этой же картинки (по sha256) за 2 недели: ушла, ушла ли — неизвестно, ждёт."""
    t = now or time.time()
    rows = conn.execute("SELECT id, status, created_ts, payload FROM drafts WHERE kind = 'da_publish' "
                        "AND status IN ('sent', 'unknown', 'sending', 'pending', 'scheduled') AND created_ts >= ? "
                        "ORDER BY created_ts DESC", (t - SAME_FILE_DAYS * 86400,)).fetchall()
    out = []
    for r in rows:
        if r["status"] == "pending" and t - r["created_ts"] > drafts.DRAFT_TTL_S:
            continue  # устаревший черновик уже не отправить
        try:
            same = (json.loads(r["payload"]).get("file_check") or {}).get("sha256") == sha256
        except (json.JSONDecodeError, AttributeError):
            same = False
        if same:
            out.append(r)
    return out


def _da_item(conn, cfg: Config, item: dict, now: float | None, used: dict[str, int], outside: dict[str, int],
             cache: dict) -> tuple[dict | None, list[str]]:
    """(payload, строки превью) или (None, [причина]). used — сколько мест в группах уже занято
    вместе с работами выше в этой же партии: так партия сама не выйдет за лимит."""
    from .da_api import _clean_tags

    file_path = str(item.get("file_path") or "")
    path, why = check_image(file_path)
    if not path:
        return None, [why]
    clean_title = str(item.get("title") or "").strip()
    if not clean_title:
        return None, ["Нужно название работы."]
    if len(clean_title) > TITLE_MAX:
        return None, [f"Название длиннее {TITLE_MAX} символов ({len(clean_title)}), DeviantArt его обрежет. "
                      "Попроси её сократить."]
    description = str(item.get("description") or "")
    tags = list(item.get("tags") or [])
    # Теги показываем ровно такими, какими они уйдут: DeviantArt принимает только латиницу, цифры и _.
    clean_tags = _clean_tags(tags)
    lost = [t for t in tags if not _clean_tags([t])]
    changed = [t for t in tags if _clean_tags([t]) and _clean_tags([t])[0] != t]
    preset = str(item.get("preset") or "")
    groups: list[dict] = []
    if preset:
        found = cfg.posting.group_presets.get(preset)
        if found is None:
            have = ", ".join(cfg.posting.group_presets) or "нет ни одного"
            return None, [f"Нет набора групп «{preset}». Есть: {have}."]
        groups = found
    folder_names = item.get("gallery_folders")
    if folder_names is None:
        folder_names = list(getattr(cfg.posting, "default_gallery_folders", []) or [])
    folders, why = resolve_gallery_folders(cfg, list(folder_names), cache)
    if why:
        return None, [why]
    limit = cfg.posting.da_submissions_per_group_per_day
    skipped: list[tuple[dict, str]] = []
    allowed: list[dict] = []
    for g in groups:
        key = g["group"].lower()
        if key not in used:
            used[key] = outside[key] = limits.deviantart_group_used(conn, g["group"], now)
        if outside[key] >= limit:
            skipped.append((g, f"в группу {g['group']} сегодня уже отправлено {limit}"))
        elif used[key] >= limit:
            skipped.append((g, f"с работами выше в этой партии в неё уже уйдёт {limit} за сутки — это лимит"))
        else:
            used[key] += 1
            allowed.append(g)
    is_mature = item.get("is_mature")
    mature = cfg.posting.default_is_mature if is_mature is None else bool(is_mature)
    st = path.stat()
    check = fingerprint(path)
    payload = {"file": str(path), "file_check": check, "title": clean_title, "description": description,
               "tags": clean_tags, "is_mature": mature,
               "mature_level": cfg.posting.default_mature_level if mature else None,
               "is_ai_generated": cfg.posting.default_is_ai_generated,
               "noai": cfg.posting.default_noai, "preset": preset, "groups": allowed,
               "galleryids": [f["folder_id"] for f in folders], "gallery_folders": [f["name"] for f in folders]}
    lines = [
        f"Файл: {_short(path)} ({st.st_size // 1024} КБ, изменён {fmt_when(st.st_mtime, _tz(cfg))})",
        *([f"  (это ярлык на {_short(path)}, указан {file_path})"] if Path(file_path).expanduser().is_symlink() else []),
        f"Название: {clean_title}",
        *([f"Описание: {description}"] if description else []),
        f"Теги: {', '.join(clean_tags) or '—'}",
        *([f"  (DeviantArt принимает в тегах только латиницу, цифры и _: изменены {', '.join(changed)}"
           + (f"; не войдут {', '.join(lost)}" if lost else "") + ")"] if changed or lost else []),
        f"18+: {'да (' + cfg.posting.default_mature_level + ')' if mature else 'нет'} · "
        f"сделано ИИ: {'да' if payload['is_ai_generated'] else 'нет'} · "
        f"запрет на обучение ИИ: {'да' if payload['noai'] else 'нет'}",
        *([f"В галерею: {', '.join(f['name'] for f in folders)}"] if folders else []),
    ]
    if allowed:
        lines.append(f"Группы ({len(allowed)}):")
        lines += [f"  - {g['group']} → {g['folder_name']}" for g in allowed]
    else:
        lines.append("Группы: никаких, только в её галерею.")
    lines += [f"  (пропущу {g['group']}: {why})" for g, why in skipped]
    same = _same_file_drafts(conn, check["sha256"], now)
    if same:
        lines.append("⚠ Эта же картинка уже есть в других черновиках публикации: "
                     + "; ".join(f"{r['id']} — {drafts.STATUS_NAMES.get(r['status'], r['status'])}" for r in same[:5])
                     + ". После «го» на DeviantArt может оказаться две одинаковые работы — сначала проверь галерею.")
    return payload, lines


def _planned(cfg: Config, schedule: str | float | None, now: float | None) -> tuple[float | None, str]:
    if schedule is None or schedule == "":
        return None, ""
    try:
        when = parse_when(schedule, _tz(cfg), now)
    except ValueError as e:
        return None, str(e)
    why = _when_problem(cfg, when, now or time.time())
    return (None, why) if why else (when, "")


# Время «только что» (пока она печатала) — ещё не прошлое.
PAST_SLACK_S = 5 * 60


def _when_problem(cfg: Config, when: float, now: float) -> str:
    if when < now - PAST_SLACK_S:
        return f"Это время уже прошло ({fmt_when(when, _tz(cfg))}). Спроси её, когда выложить."
    if when - now > drafts.SCHEDULE_MAX_AGE_S:
        return "Запланировать можно не дальше чем на неделю вперёд."
    return ""


def _when_line(cfg: Config, when: float | None) -> list[str]:
    return [f"Когда: {fmt_when(when, _tz(cfg))} — после её «го» уйдёт само в это время."] if when else []


def prepare_da_publish(conn, cfg: Config, file_path: str, title: str, description: str = "",
                       tags: list[str] | None = None, preset: str = "", is_mature: bool | None = None,
                       now: float | None = None, gallery_folders: list[str] | None = None,
                       schedule: str | float | None = None) -> tuple[str | None, str]:
    """(id черновика или None, текст для неё). gallery_folders — папки её галереи по названиям
    (None — из настройки default_gallery_folders); schedule — когда отправить после её «го»."""
    when, why = _planned(cfg, schedule, now)
    if why:
        return None, why
    item = {"file_path": file_path, "title": title, "description": description, "tags": tags, "preset": preset,
            "is_mature": is_mature, "gallery_folders": gallery_folders}
    payload, lines = _da_item(conn, cfg, item, now, {}, {}, {})
    if payload is None:
        return None, lines[0]
    if when:
        payload["planned_send_at"] = when
    preview = "\n".join([*lines, *_when_line(cfg, when), DA_NOTE])
    return drafts.create(conn, "da_publish", payload, preview, now), preview


def prepare_da_batch(conn, cfg: Config, items: list[dict], schedule: str | float | None = None,
                     now: float | None = None) -> tuple[list[str], str]:
    """Партия работ: каждая — свой черновик, все связаны batch_id; одно превью на всю партию.

    items: [{file_path, title, description, tags, preset, is_mature, gallery_folders}].
    Лимиты групп считаются по всей партии: что не влезает — выпадает из следующих работ, и превью это говорит.
    Возвращает ([id черновиков], превью); при ошибке — ([], причина), черновики не создаются.
    """
    if not items:
        return [], "Партия пустая: нужна хотя бы одна работа."
    if len(items) > BATCH_MAX:
        return [], f"В одной партии не больше {BATCH_MAX} работ — так аккаунт выглядит живым. Раздели на несколько."
    when, why = _planned(cfg, schedule, now)
    if why:
        return [], why
    used: dict[str, int] = {}
    outside: dict[str, int] = {}
    cache: dict = {}
    built: list[tuple[dict, list[str]]] = []
    for n, item in enumerate(items, 1):
        payload, lines = _da_item(conn, cfg, item, now, used, outside, cache)
        if payload is None:
            return [], f"Работа {n} ({item.get('title') or item.get('file_path') or '?'}): {lines[0]}"
        built.append((payload, lines))
    files = [p["file"] for p, _ in built]
    batch_id = "p" + uuid.uuid4().hex[:7]  # не шестнадцатеричная буква: с id черновика не спутать
    total = len(built)
    ids: list[str] = []
    blocks: list[str] = []
    for n, (payload, lines) in enumerate(built, 1):
        payload.update(batch_id=batch_id, batch_index=n, batch_size=total)
        if when:
            payload["planned_send_at"] = when
        own = "\n".join([f"Работа {n} из {total} (партия {batch_id})", *lines, *_when_line(cfg, when), DA_NOTE])
        draft_id = drafts.create(conn, "da_publish", payload, own, now)
        ids.append(draft_id)
        blocks.append("\n".join([f"{n}) черновик {draft_id}", *("   " + line for line in lines)]))
    head = [f"Партия {batch_id}: {total} работ для DeviantArt. Одно её «го» — и я выложу их по очереди, "
            f"с паузой {PACE_S[0] // 60}–{PACE_S[1] // 60} минуты между работами.",
            *_when_line(cfg, when)]
    dupes = sorted({_short(Path(f)) for f in files if files.count(f) > 1})
    if dupes:
        head.append(f"Внимание: один и тот же файл несколько раз — {', '.join(dupes)}.")
    return ids, "\n".join([*head, *blocks, DA_NOTE])


def is_batch_id(value: str) -> bool:
    value = value.strip()
    return len(value) == 8 and value.startswith("p")


def batch_rows(conn, batch_id: str) -> list:
    drafts.ensure_schema(conn)
    return conn.execute("SELECT * FROM drafts WHERE kind = 'da_publish' AND json_extract(payload, '$.batch_id') = ? "
                        "ORDER BY json_extract(payload, '$.batch_index')", (batch_id.strip(),)).fetchall()


def _waiting_ids(conn, draft_or_batch_id: str) -> list[str]:
    if is_batch_id(draft_or_batch_id):
        return [r["id"] for r in batch_rows(conn, draft_or_batch_id) if r["status"] in drafts.WAITING]
    return [draft_or_batch_id.strip()]


def approve_draft(conn, cfg: Config, draft_or_batch_id: str, send_at: str | float | None = None,
                  now: float | None = None) -> str:
    """Её «го» со временем: черновик (или вся партия) уйдёт сам, когда придёт время.

    send_at — её время словами («завтра 19:00», «03.11 19:00») в её поясе или unix-время;
    None — время из черновика (schedule при подготовке), а если его нет — «сейчас»:
    тогда черновик уйдёт при ближайшей проверке (или сразу запусти отправку: start_sending).
    Отклики Toyhouse по расписанию не отправляются — только сразу.
    """
    now = now or time.time()
    tz = _tz(cfg)
    ids = _waiting_ids(conn, draft_or_batch_id)
    if not ids:
        return "В этой партии нет работ, которые ждут «го» (уже отправлены или отменены)."
    rows = [drafts.get(conn, i, now) for i in ids]
    if any(r is None for r in rows):
        return "Такого черновика нет. Сначала подготовь его и покажи ей."
    if any(r["kind"] in GALLERY_KINDS for r in rows if r):
        return ("Правки галереи (закрыть аукционы, папки, названия) по расписанию не делаю: только сразу "
                "по её «го» (submit_draft).")
    if any(r["kind"] == "th_reply" for r in rows if r):
        return "Отклики Toyhouse по расписанию не отправляю: только сразу по её «го» (submit_draft)."
    if any(r["kind"] != "da_publish" for r in rows if r):
        return "Этот черновик по расписанию не делаю: только сразу по её «го» (submit_draft)."
    if send_at is None:
        planned = json.loads(rows[0]["payload"]).get("planned_send_at") if rows[0] else None
        when = float(planned) if planned else now
        if planned and when < now - PAST_SLACK_S:
            return (f"Время из черновика ({fmt_when(when, tz)}) уже прошло. Спроси её, когда выложить, "
                    "и передай его в send_at.")
    else:
        try:
            when = parse_when(send_at, tz, now)
        except ValueError as e:
            return str(e)
        why = _when_problem(cfg, when, now)
        if why:
            return why
    errors = []
    for draft_id in ids:
        try:
            drafts.schedule(conn, draft_id, when, now)
        except drafts.DraftError as e:
            errors.append(f"{draft_id}: {e}")
    done = len(ids) - len(errors)
    what = f"{done} из {len(ids)} работ партии" if len(ids) > 1 else "черновик"
    text = (f"Запланировано: {what} уйдёт {fmt_when(max(when, now), tz)}." if when > now
            else f"Поставила в очередь: {what} уйдёт при ближайшей проверке (или запусти отправку сейчас).")
    if not done:
        return "Не получилось запланировать: " + "; ".join(errors)
    return text + ("".join(f"\nНе вышло: {e}" for e in errors))


def cancel_any(conn, draft_or_batch_id: str) -> int:
    """Отменить черновик или все ещё не ушедшие работы партии. Сколько отменено."""
    n = 0
    for i in _waiting_ids(conn, draft_or_batch_id):
        if drafts.cancel(conn, i):
            n += 1
            _quietly(_release_ping_draft, conn, i)
    return n


def _release_ping_draft(conn, draft_id: str) -> None:
    """Отменённый черновик пинга: занятые им ники снова свободны."""
    from . import pinglist

    row = drafts.get(conn, draft_id)
    if row and row["kind"] == "da_ping":
        p = json.loads(row["payload"])
        pinglist.release(conn, p["deviation_id"], [u for c in p.get("comments") or [] for u in c.get("users") or []],
                         reserved_until=row["created_ts"])


def _release_dead_pings(conn, deviation_id: str, now: float | None) -> None:
    """Черновики пинга под этой работой, которые уже не уйдут (просрочены, отменены, не удались),
    больше не держат ники: просроченный черновик закрывается без cancel_any."""
    cutoff = (now or time.time()) - drafts.DRAFT_TTL_S
    rows = conn.execute("SELECT id, payload FROM drafts WHERE kind = 'da_ping' AND (status IN ('cancelled', 'failed') "
                        "OR (status = 'pending' AND created_ts < ?))", (cutoff,)).fetchall()
    for r in rows:
        try:
            dev = json.loads(r["payload"] or "{}").get("deviation_id")
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if dev == deviation_id:
            _release_ping_draft(conn, r["id"])


# --- Отправка ---

# Одна отправка за раз на весь мак: проверка лимитов, сама отправка и запись в журнал идут под этим замком.
SEND_LOCK_WAIT_S = 5 * 60
_PROCS: dict[str, subprocess.Popen] = {}


class SendBusy(Exception):
    pass


@contextmanager
def send_lock(timeout_s: float = SEND_LOCK_WAIT_S, poll_s: float = 0.5):
    path = DATA_DIR / "send.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        deadline = time.time() + timeout_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as e:
                if time.time() >= deadline:
                    raise SendBusy("другая отправка ещё идёт") from e
                time.sleep(poll_s)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def start_sending(draft_id: str) -> subprocess.Popen:
    logs = DATA_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "send-draft.log", "a") as log_file:
        proc = subprocess.Popen([sys.executable, "-m", "mcp_devian", "send-draft", draft_id],
                                stdout=log_file, stderr=log_file, stdin=subprocess.DEVNULL, start_new_session=True)
    _PROCS[draft_id] = proc
    return proc


def _log_tail(limit: int = 300) -> str:
    try:
        text = (DATA_DIR / "logs" / "send-draft.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "лога нет"
    return " ".join(text.strip().splitlines()[-3:])[-limit:] or "лог пустой"


def wait_for(conn, draft_id: str, timeout_s: float = 45, poll_s: float = 1.5) -> dict:
    deadline = time.time() + timeout_s
    row = drafts.get(conn, draft_id)
    while row and row["status"] in ("pending", "sending") and time.time() < deadline:
        proc = _PROCS.get(draft_id)
        code = proc.poll() if proc is not None and row["status"] == "pending" else None
        if code is not None:
            if code != 0:
                # Процесс отправки упал, не успев забрать черновик: закрываем его, чтобы он не ушёл позже сам.
                drafts.fail_if_pending(conn, draft_id, f"отправка не запустилась: {_log_tail()}")
            # Иначе процесс закончил, а черновик снова ждёт (Chrome был занят, шла другая отправка): ответ в result.
            row = drafts.get(conn, draft_id)
            break
        time.sleep(poll_s)
        row = drafts.get(conn, draft_id)
    return dict(row) if row else {}


def send(conn, cfg: Config, draft_id: str, now: float | None = None,
         lock_timeout_s: float = SEND_LOCK_WAIT_S, pace: bool = False, sleep=time.sleep,
         rng: random.Random | None = None) -> str:
    """Сама отправка (в отдельном процессе). Возвращает итог словами.
    pace — выдержать паузу после прошлой публикации на DA (внутри замка, чтобы два процесса её не съели)."""
    if not SENDING_ENABLED:
        return NOT_YET
    peek = drafts.get(conn, draft_id, now)
    if peek is not None and peek["kind"] in GALLERY_KINDS:
        # Много работ в одном черновике: замок берём на каждую работу, а не на всё время.
        return _send_gallery(conn, cfg, draft_id, peek["kind"], now, lock_timeout_s, sleep, rng)
    if peek is not None and peek["kind"] == "da_ping":
        # Несколько комментариев с паузой в минуты: замок — на каждый комментарий.
        return _send_ping(conn, cfg, draft_id, now, lock_timeout_s, sleep, rng)
    try:
        with send_lock(lock_timeout_s):
            row = drafts.get(conn, draft_id, now)
            kind = row["kind"] if row else ""
            if pace and row is not None and kind == "da_publish" and row["status"] in drafts.WAITING:
                _pace(conn, now, sleep, rng)
            try:
                payload = drafts.take_for_sending(conn, draft_id, kind, now)
            except drafts.DraftError as e:
                return str(e)
            sender = _sender(kind)
            try:
                status, result = sender(conn, cfg, payload, draft_id, now)
            except Exception as e:  # noqa: BLE001 — исход неизвестен: могло уйти, повторять вслепую нельзя
                status, result = "unknown", f"{drafts.UNKNOWN_RESULT} (ошибка: {type(e).__name__}: {e})"
            if status == "pending":  # до сайта не дошло, её «го» в силе — можно повторить
                drafts.requeue(conn, draft_id, result)
            else:
                drafts.finish(conn, draft_id, status == "sent", result, now, status=status)
            return result
    except SendBusy:
        drafts.note_waiting(conn, draft_id, BUSY_TEXT)
        return BUSY_TEXT


BUSY_TEXT = "Сейчас идёт другая отправка — этот черновик не тронула, попробуй через пару минут."


# Состояния, при которых на сайт точно ничего не ушло: место в лимитах освобождаем.
# no_submit_button сюда не входит: текст остался в форме, и «Post» она может нажать сама.
NOT_POSTED = {"no_form", "closed", "not_logged_in", "challenge", "fill_failed", "already_submitted", "timeout",
              "wrong_page"}
CHROME_BUSY = "Chrome сейчас занят проверкой — попробуй через пару минут"


def _send_th_reply(conn, cfg: Config, payload: dict, draft_id: str, now: float | None) -> tuple[str, str]:
    from . import her_chrome
    from .toyhouse import BrowserBusy

    # Лимиты проверяем ещё раз: между черновиком и «го» могли уйти другие отклики.
    block = limits.toyhouse_reply_block(conn, cfg.posting, payload["thread_id"], payload["character_url"], now,
                                        exclude_draft=draft_id)
    if block:
        return "failed", f"не отправила: {block}"
    submit = bool(payload["submit"])
    # Место в лимитах занимаем до клика: если что-то оборвётся, повторный отклик в тот же тред не пройдёт.
    rid = drafts.reserve(conn, "th_reply", payload["thread_id"], payload["character_url"], now)
    try:
        res = her_chrome.run_reply(payload["thread_url"], payload["html"], submit,
                                   cfg.posting.th_username, payload["marker"],
                                   character_url=payload.get("character_url") or "")
    except BrowserBusy:  # её Chrome занят проверкой ещё до клика: ничего не ушло
        drafts.release(conn, rid)
        return "pending", CHROME_BUSY
    except her_chrome.ChromeSetupError as e:
        drafts.release(conn, rid)
        return "failed", f"не отправила: {e}"
    except Exception as e:  # noqa: BLE001 — после возможного клика исход неизвестен
        if submit:
            drafts.settle(conn, rid, "unknown")
            return "unknown", f"{drafts.UNKNOWN_RESULT}: {e}"
        drafts.release(conn, rid)
        return "failed", f"не получилось: {e}"
    state = res.get("state") or ""
    if state == "no_submit_button":
        # Сама не отправила, но текст ждёт в форме: место в лимитах держим, как для «filled».
        _quietly(drafts.settle, conn, rid, state)
        return "failed", (f"{_reply_text(state, res)}. Если нажмёт сама, второй отклик в этот тред "
                          "помощник не подготовит до конца паузы треда.")
    if state in NOT_POSTED or (not submit and state not in ("filled",)):
        _quietly(drafts.release, conn, rid)
        return "failed", _reply_text(state, res)
    if state in ("posted", "submitted_unverified", "filled"):
        # «Вставлено, жмёт сама» тоже считаем в лимиты: скорее всего, она нажмёт.
        _quietly(drafts.settle, conn, rid, res.get("permalink") or state)
        return "sent", _reply_text(state, res)
    _quietly(drafts.settle, conn, rid, "unknown")
    return "unknown", f"{drafts.UNKNOWN_RESULT} ({res.get('message') or state})"


def _quietly(fn, *args) -> None:
    """Учёт после необратимого шага: если база занята, итог для неё важнее записи в журнал."""
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        log.warning("bookkeeping after send failed: %s", e)


def _reply_text(state: str, res: dict) -> str:
    text = REPLY_STATES.get(state, "не получилось: {error}")
    return text.format_map({"permalink": res.get("permalink") or "(ссылку не нашла)",
                            "error": res.get("message") or res.get("error") or state})


REPLY_STATES = {
    "posted": "отклик опубликован: {permalink}",
    "submitted_unverified": "отправлено, но я не нашла пост на странице — загляни в тред",
    "filled": "текст вставлен в тред в её Chrome — осталось нажать «Post»",
    "no_form": "не нашла форму ответа: тред закрыт или она не залогинена в Chrome",
    "closed": "тред закрыт, отвечать нельзя",
    "no_submit_button": "не нашла кнопку отправки — текст остался в форме в её Chrome, «Post» можно нажать вручную",
    "not_logged_in": "в Chrome она не залогинена на Toyhouse",
    "challenge": "Toyhouse просит подтвердить, что она человек: вкладка открыта, нажми галочку и попроси ещё раз",
    "timeout": "страница треда не загрузилась вовремя, ничего не отправлено",
    "wrong_page": "вкладка ушла с этого треда — ничего не вставила и не отправила, можно попробовать ещё раз",
}


def _uncertain(exc: Exception) -> bool:
    """Ошибка на запросе публикации: мог ли DeviantArt всё же опубликовать?"""
    from . import da_api

    if isinstance(exc, da_api.UncertainPublish):
        return True
    if isinstance(exc, (da_api.RateLimited, da_api.NotConnected)):
        return False  # запрос отвергнут, публикации не было
    if isinstance(exc, da_api.DAApiError):
        # error_code 0 — это ответ DeviantArt («нужно согласие»), а не его отсутствие; 5 — тоже его отказ.
        code = "" if exc.code is None else str(exc.code)
        # Нет ответа (сеть, таймаут, нечитаемый ответ) или ошибка сервера — исход неизвестен.
        server = (exc.status or 0) >= 500 or (len(code) == 3 and code.startswith("5"))
        return not code or code in da_api.LOST_RESPONSE or server
    return True


def _send_da_publish(conn, cfg: Config, payload: dict, draft_id: str, now: float | None) -> tuple[str, str]:
    client = da_client(cfg)
    if not client.is_connected():
        return "failed", "DeviantArt не подключён: нужно accounts:da_connect"
    # Лимиты групп ещё раз: она одобрила конкретный список, молча выкидывать группы нельзя.
    blocked = [why for g in payload["groups"]
               if (why := limits.deviantart_group_block(conn, cfg.posting, g["group"], now, exclude_draft=draft_id))]
    if blocked:
        return "failed", f"не отправила: {'; '.join(blocked)}. Подготовь новый черновик (эти группы можно убрать)."
    path = Path(payload["file"])
    try:
        same = fingerprint(path) == payload.get("file_check")
    except OSError:
        same = False
    if not same:
        return "failed", "файл изменился или пропал после черновика — подготовь черновик заново"
    # Sta.sh — личное хранилище: если загрузка упала, публично ничего не появилось.
    try:
        itemid = client.stash_submit(str(path), payload["title"], payload["description"], payload["tags"])
    except Exception as e:  # noqa: BLE001
        return "failed", f"не загрузила картинку на DeviantArt, публично ничего не появилось: {e}"
    reserved = [(g, drafts.reserve(conn, "da_group", g["group"].lower(), draft_id, now)) for g in payload["groups"]]
    groups = [(g["group_id"], g["folder_id"]) for g in payload["groups"]]
    try:
        res = client.stash_publish(itemid, is_mature=payload["is_mature"], tags=payload["tags"], groups=groups,
                                   galleryids=payload.get("galleryids") or (),
                                   is_ai_generated=payload["is_ai_generated"], noai=payload["noai"],
                                   mature_level=payload.get("mature_level"))
    except Exception as e:  # noqa: BLE001
        if _uncertain(e):
            for _, rid in reserved:
                drafts.settle(conn, rid, f"unknown itemid={itemid}")
            return "unknown", (f"{drafts.UNKNOWN_RESULT}: DeviantArt не ответил ({e}). "
                               "Загляни в её галерею, прежде чем публиковать снова.")
        for _, rid in reserved:
            drafts.release(conn, rid)
        return "failed", f"DeviantArt не опубликовал: {e}"
    for _, rid in reserved:
        _quietly(drafts.settle, conn, rid, res.get("url") or res.get("deviationid") or "ok")
    tail = f", отправлено в {len(groups)} групп — проверь, что они приняли" if groups else ""
    return "sent", f"опубликовано: {res.get('url')}{tail}"


# --- Партии и расписание: по очереди и с паузами, как живой человек ---

# Пауза между публикациями на DeviantArt, с разбросом: пачка работ за секунду похожа на бота.
PACE_S = (60, 120)


def _pace(conn, now: float | None, sleep, rng: random.Random | None) -> None:
    """Выдержать паузу после последней публикации на DeviantArt (из любого процесса)."""
    row = conn.execute("SELECT MAX(sent_ts) FROM drafts WHERE kind = 'da_publish' "
                       "AND status IN ('sent', 'unknown', 'sending')").fetchone()
    if not row or not row[0]:
        return
    wait = row[0] + (rng or random).uniform(*PACE_S) - (now or time.time())
    if wait > 0:
        sleep(wait)


def _send_paced(conn, cfg: Config, draft_id: str, now: float | None, sleep, rng) -> dict:
    result = send(conn, cfg, draft_id, now, pace=True, sleep=sleep, rng=rng)
    row = drafts.get(conn, draft_id, now)
    return {"draft_id": draft_id, "status": row["status"] if row else "", "result": result}


def send_batch(conn, cfg: Config, batch_id: str, now: float | None = None, sleep=time.sleep,
               rng: random.Random | None = None) -> list[dict]:
    """Её «го» на партию: работы уходят по очереди, каждая со всеми проверками, между ними пауза.
    Отменённые по пути (cancel_any) пропускаются; запланированные на потом не трогаем."""
    out = []
    for row in batch_rows(conn, batch_id):
        if row["status"] == "pending" and not _planned_later(row, now):
            out.append(_send_paced(conn, cfg, row["id"], now, sleep, rng))
    return out


def _planned_later(row, now: float | None) -> bool:
    """Черновик подготовлен «на потом»: без schedule_draft он не уходит."""
    planned = json.loads(row["payload"]).get("planned_send_at")
    return bool(planned) and float(planned) > (now or time.time())


def send_any(conn, cfg: Config, draft_or_batch_id: str, now: float | None = None, sleep=time.sleep) -> str:
    """Для send-draft: черновик или вся партия. Итог словами."""
    if not is_batch_id(draft_or_batch_id):
        return send(conn, cfg, draft_or_batch_id, now)
    results = send_batch(conn, cfg, draft_or_batch_id, now, sleep)
    if not results:
        return "В этой партии нечего отправлять: всё уже ушло, отменено или запланировано на потом."
    return "\n".join(f"{r['draft_id']}: {r['result']}" for r in results)


def send_due(conn, cfg: Config, now: float | None = None, sleep=time.sleep,
             rng: random.Random | None = None) -> list[dict]:
    """Из фоновой проверки: отправить запланированное, чьё время пришло (с паузами между работами).
    Всё перепроверяется в момент отправки; запланированное больше недели назад отменяется с причиной."""
    if not SENDING_ENABLED:
        return []
    out = [{"draft_id": i, "status": "cancelled", "result": drafts.EXPIRED_SCHEDULE}
           for i in drafts.expire_scheduled(conn, now)]
    for row in drafts.due(conn, now):
        out.append(_send_paced(conn, cfg, row["id"], now, sleep, rng))
    return out


def list_open(conn, now: float | None = None) -> list:
    """Ждут «го», запланированы, отправляются — и за последние сутки те, о которых неизвестно, ушли ли."""
    now = now or time.time()
    drafts.ensure_schema(conn)
    drafts.reap_stuck(conn, now)
    drafts.expire_pending(conn, now)
    return conn.execute(
        "SELECT * FROM drafts WHERE status IN ('pending', 'scheduled', 'sending') "
        "OR (status = 'unknown' AND COALESCE(sent_ts, created_ts) >= ?) ORDER BY created_ts DESC",
        (now - 86400,)).fetchall()


FILLED_NAME = "заполнено в её Chrome — кнопку на сайте жмёт она"


def status_name(kind: str, status: str) -> str:
    """Статус словами; у персов Toyhouse «отправлен» значит «форма заполнена, Save/Transfer — за ней»."""
    if status == "sent" and str(kind or "").startswith("th_character_"):
        return FILLED_NAME
    return drafts.STATUS_NAMES.get(status, status)


def describe(row, tz: str | None = None) -> str:
    when = fmt_when(row["created_ts"], tz)
    payload = json.loads(row["payload"])
    first = payload.get("thread_title") or payload.get("title") or ""
    label = " (чужой текст)" if payload.get("thread_title") else ""
    line = f"[{row['id']}] {row['kind']} · {when} · {status_name(row['kind'], row['status'])}"
    try:
        send_at = row["send_at"]
    except (IndexError, KeyError):  # строка из базы до 0.4
        send_at = None
    if row["status"] == "scheduled" and send_at:
        line += f" на {fmt_when(send_at, tz)}"
    if payload.get("batch_id"):
        line += f" · партия {payload['batch_id']}, {payload.get('batch_index')}/{payload.get('batch_size')}"
    line += f" · {first}{label}"
    if row["status"] in ("unknown", "pending", "scheduled") and row["result"]:
        line += f"\n  {row['result']}"
    return line


# --- Её галерея на DeviantArt: закрыть аукционы, папки, переименование ---
#
# Один черновик — много работ: одно превью, одно её «го». Работы идут по одной, с паузой, каждая
# перепроверяется в момент отправки (название не поменялось после превью). Повтор безопасен:
# что уже сделано (новое название, папка, комментарий, продажа), второй раз не делается.

GALLERY_KINDS = ("da_close", "da_folders", "da_rename")
GALLERY_MAX = 25
GALLERY_LOOKUP_PAGES = 10
FOLDER_PACE_S = (5, 15)
COMMENT_MAX = 2000
COMMENT_SLOTS = ("winner", "amount")
WORD_RE = re.compile(r"^\w[\w !.\-]{0,19}$")
DA_USERNAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_OPEN_WORDS = r"open|opened|available|открыт[оа]?"
OPEN_BRACKETED = re.compile(rf"[\[\(\{{【«<]\s*({_OPEN_WORDS})\s*[\]\)\}}】»>]", re.I)
OPEN_WORD = re.compile(rf"(?<![\w-])({_OPEN_WORDS})(?![\w-])(?!\s+(?:to|species|for|offers?)\b)", re.I)

STEP_NAMES = {"waiting": "ждёт", "done": "готово", "already": "уже было сделано", "skipped": "пропущено",
              "partial": "сделано не всё", "failed": "не вышло", "unknown": "неизвестно, сделано ли — проверь на сайте",
              "notrun": "не делала"}
GALLERY_DONE = ("done", "already")


def _gallery_note(cfg: Config) -> str:
    return ("Как меняю название: через официальный API DeviantArt, а всё, что API показывает, отправляю заново как есть — "
            "теги, папки, 18+, комментарии, скачивание. Описание API не меняет. Метки ИИ ставлю по её настройкам: "
            f"сделано ИИ — {'да' if cfg.posting.default_is_ai_generated else 'нет'}, запрет на обучение ИИ — "
            f"{'да' if cfg.posting.default_noai else 'нет'}. Размер показа и водяной знак DeviantArt API не показывает: "
            "если она ставила их на этих работах, они могут сброситься — такие работы лучше переименовать на сайте. "
            "После каждой правки перечитываю работу: если DeviantArt что-то сбросил, остановлюсь и остальные не трону.")


# Лот без метки в названии («Silk moth auction») считаем открытым только первые 3 недели (как
# auctions.to_refresh): иначе давно закончившиеся аукционы копились бы и вытесняли нынешние.
UNMARKED_OPEN_S = 21 * 86400


def open_auctions(conn, limit: int = 100, now: float | None = None) -> list:
    """Её открытые аукционы из базы, свежие первыми: с меткой OPEN — все, без метки — за 3 недели."""
    since = (now or time.time()) - UNMARKED_OPEN_S
    return conn.execute(
        "SELECT * FROM items WHERE source = 'deviantart' AND own = 1 AND lot_type IN ('auction', 'bta') "
        "AND (status = 'open' OR (COALESCE(status, '') IN ('', 'unknown') "
        "AND COALESCE(published_ts, first_seen_ts) >= ?)) "
        "ORDER BY COALESCE(published_ts, first_seen_ts) DESC LIMIT ?",
        (since, limit)).fetchall()


def _match_case(word: str, found: str) -> str:
    if len(found) > 1 and found.isupper():
        return word.upper()
    if found.islower():
        return word.lower()
    if found[:1].isupper():
        return word[:1].upper() + word[1:].lower()
    return word


_EDGE = " \t|:~•·—–-!.,*"


def _marker_like(t: str, m: re.Match) -> bool:
    """Слово open без скобок — метка, только если оно КАПСОМ или стоит с краю названия
    («Moth | Open», «OPEN moth»); «Eyes Open moth» — просто слово в названии."""
    found = m.group(1)
    return (len(found) > 1 and found.isupper()) or not t[:m.start(1)].strip(_EDGE) or not t[m.end(1):].strip(_EDGE)


def closed_title(title: str, word: str) -> tuple[str | None, str]:
    """Название после закрытия: метка OPEN → word (в том же регистре), без метки — [word] в начало.
    (None, причина) — менять не нужно: уже помечена закрытой."""
    from .classify import status_of

    t = title.strip()
    m = OPEN_BRACKETED.search(t)
    if m:
        return t[:m.start(1)] + _match_case(word, m.group(1)) + t[m.end(1):], ""
    if status_of(t) == "closed" or re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", t, re.I):
        return None, "уже помечена закрытой"
    m = next((x for x in OPEN_WORD.finditer(t) if _marker_like(t, x)), None)
    if m:
        new = t[:m.start(1)] + _match_case(word, m.group(1)) + t[m.end(1):]
        # «OPEN 2 left» → «CLOSED 2 left» всё ещё читается открытым — тогда метка в начало.
        if status_of(new) != "open":
            return new, ""
    return f"[{word}] {t}", ""


def check_comment_template(text: str) -> str:
    """'' — годится; иначе причина. Слоты — только {winner} и {amount}, без форматов."""
    if len(text) > COMMENT_MAX:
        return f"длиннее {COMMENT_MAX} символов"
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        return "непарные фигурные скобки — слоты пишутся так: {winner}, {amount}"
    bad = [f"{{{f}}}" for _, f, spec, conv in parsed if f is not None and (f not in COMMENT_SLOTS or spec or conv)]
    if bad:
        return f"неизвестные слоты {', '.join(bad)}: можно только {{winner}} и {{amount}}"
    return ""


def render_comment(text: str, winner: str | None, amount: str | None) -> tuple[str | None, str]:
    """Её текст с подставленными слотами или (None, почему не получится)."""
    fields = {f for _, f, _, _ in string.Formatter().parse(text) if f}
    if "winner" in fields and not winner:
        return None, "комментарий не напишу: победитель неизвестен, а в тексте есть {winner}"
    if "amount" in fields and not amount:
        return None, "комментарий не напишу: сумма неизвестна, а в тексте есть {amount}"
    return text.format_map({"winner": winner or "", "amount": amount or ""}), ""


def _username(value) -> str | None:
    name = str(value or "").strip().lstrip("@")
    return name if DA_USERNAME.match(name) else None


def _auction_info(conn, item_id: str | None) -> dict:
    """Победитель и сумма по ставкам (как в sales.sync_from_auctions). Ники — чужой текст: только допустимые."""
    from . import sales

    if not item_id:
        return {}
    row = conn.execute("SELECT * FROM auctions WHERE deviation_id = ?", (item_id,)).fetchone()
    if not row:
        return {}
    try:
        top_user = _username(json.loads(row["extra"] or "{}").get("top_user"))
    except (json.JSONDecodeError, TypeError, AttributeError):
        top_user = None
    winner, amount, note = _username(row["winner"]), row["top_amount"], ""
    if not winner and top_user and amount is not None:
        winner, note = top_user, sales.CHECK_BUYER
    elif winner and top_user and winner.casefold() != top_user.casefold():
        amount, note = None, sales.CHECK_AMOUNT
    currency = row["currency"] if row["currency"] in sales.CURRENCIES else None
    return {"winner": winner, "amount": amount, "currency": currency, "note": note,
            "amount_text": sales.money(amount, currency) if amount is not None else None}


def _gallery_ctx(cfg: Config, scopes) -> tuple[Any, str, str]:
    """(клиент, её ник, причина отказа)."""
    from . import da_api

    try:
        client = da_client(cfg)
        if not client.is_connected():
            return None, "", "DeviantArt не подключён: сначала accounts:da_connect (она нажмёт «Разрешить» на сайте)."
        client.require_scopes(*scopes)
        username = cfg.deviantart.my_username or (client.whoami().get("username") or "")
    except SettingsError as e:
        return None, "", str(e)
    except da_api.NeedsReconnect:
        return None, "", da_api.RECONNECT
    except da_api.DAApiError as e:
        return None, "", f"Не получилось связаться с DeviantArt: {e}"
    if not username:
        return None, "", "Не знаю её ник на DeviantArt: укажи его в настройках (update_settings my_da_username)."
    return client, username, ""


def _find_folder(folders: list[dict], name: str) -> dict | None:
    want = _norm_folder(name)
    return next((f for f in folders if _norm_folder(f.get("name") or "") == want), None)


def _folder_names(folders: list[dict]) -> str:
    return ", ".join(f.get("name") or "?" for f in folders) or "ни одной"


def _resolve_work(conn, client, username: str, ref: str, cache: dict) -> tuple[dict | None, str]:
    """Её работа по ссылке, id da:123 или названию → ({item_id, uuid, url, title}, '') или (None, причина)."""
    from . import auctions, da_api

    ref = str(ref or "").strip()
    item = None
    if not ref:
        return None, "пустое название работы"
    if ref.startswith("da:"):
        item = db.get_item(conn, ref)
        if not item:
            return None, f"«{ref}»: такой работы нет в базе помощника"
    elif ref.startswith("http"):
        m = da_api.DEVIATION_URL.match(ref)
        if not m:
            return None, f"«{ref}»: это не ссылка на работу DeviantArt"
        if m.group(1).casefold() != username.casefold():
            return None, f"«{ref}»: это не её работа (автор {m.group(1)}) — чужие не трогаю"
        item = auctions.item_for_url(conn, ref)
        if not item:
            uuid = client.deviation_uuid_from_url(ref, max_pages=GALLERY_LOOKUP_PAGES)
            if not uuid:
                return None, f"«{ref}»: не нашла эту работу среди её последних {GALLERY_LOOKUP_PAGES * 24}"
            return {"item_id": None, "uuid": uuid, "url": ref, "title": ""}, ""
    else:
        key = ref.casefold()
        if "items" not in cache:
            cache["items"] = conn.execute("SELECT * FROM items WHERE source = 'deviantart' AND own = 1 "
                                          "ORDER BY COALESCE(published_ts, first_seen_ts) DESC").fetchall()
        mine = cache["items"]
        hits = ([i for i in mine if i["title"].strip().casefold() == key]
                or [i for i in mine if key in i["title"].casefold()])
        if len(hits) > 1:
            return None, (f"«{ref}»: подходит несколько работ ({'; '.join(i['title'] for i in hits[:5])}) — "
                          "назови точнее или пришли ссылку")
        if hits:
            item = hits[0]
        else:
            if "gallery" not in cache:
                cache["gallery"] = client.gallery_all(username, GALLERY_LOOKUP_PAGES)
            devs = cache["gallery"]
            found = ([d for d in devs if d["title"].strip().casefold() == key]
                     or [d for d in devs if key in d["title"].casefold()])
            if len(found) > 1:
                return None, f"«{ref}»: подходит несколько работ ({'; '.join(d['title'] for d in found[:5])})"
            if not found:
                return None, f"«{ref}»: не нашла такую работу в её галерее"
            dev = found[0]
            row = auctions.item_for_url(conn, dev["url"]) if dev["url"] else None
            return {"item_id": row["id"] if row and row["own"] else None, "uuid": dev["deviationid"],
                    "url": dev["url"], "title": dev["title"]}, ""
    if item["source"] != "deviantart" or not item["own"]:
        return None, f"«{item['title']}»: это не её работа — чужие не трогаю"
    uuid = auctions.known_uuid(item) or client.deviation_uuid_from_url(item["url"], max_pages=GALLERY_LOOKUP_PAGES)
    if not uuid:
        return None, f"«{item['title']}»: не нашла эту работу в её галерее через API"
    auctions.remember_uuid(conn, item, uuid)
    return {"item_id": item["id"], "uuid": uuid, "url": item["url"], "title": item["title"]}, ""


def _comment_ref(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _comment_journaled(conn, uuid: str, text: str) -> bool:
    return drafts.count_sent(conn, "da_comment", 0, target=uuid, ref=_comment_ref(text)) > 0


def comment_once(conn, client: Any, username: str, uuid: str, text: str, now: float | None = None,
                 do=None) -> tuple[str, str]:
    """Один комментарий с этим текстом под работой — никогда второй.

    → (posted | already | unchecked, итог словами). Журнал sent_log 'da_comment' (ref — хэш текста) пишется
    ДО запроса: потерянный ответ (UncertainPublish) летит наружу, а второй раз этот текст не уйдёт."""
    from . import da_api

    if _comment_journaled(conn, uuid, text):
        return "already", "этот комментарий я уже писала раньше — второй раз не пишу"
    try:
        comments = client.deviation_comments(uuid)
    except (da_api.RateLimited, da_api.NotConnected):
        raise
    except da_api.DAApiError as e:
        return "unchecked", f"комментарий не написала: не смогла проверить, нет ли его уже ({e})"
    want = " ".join(text.split()).casefold()
    if any((c.user or "").casefold() == username.casefold() and " ".join((c.text or "").split()).casefold() == want
           for c in comments):
        return "already", "такой её комментарий уже есть"
    rid = drafts.reserve(conn, "da_comment", uuid, _comment_ref(text), now)
    try:
        res = (do or _call)(client.post_comment, uuid, text)
    except Exception as e:
        if _uncertain(e):
            _quietly(drafts.settle, conn, rid, "unknown")
        else:
            _quietly(drafts.release, conn, rid)
        raise
    _quietly(drafts.settle, conn, rid, str((res or {}).get("commentid") or "ok"))
    return "posted", "написала комментарий"


def _call(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _gallery_draft(conn, kind: str, payload: dict, head: list[str], blocks: list[str], skipped: list[str],
                   tail: list[str], now: float | None) -> tuple[str, str]:
    lines = [*head, *blocks]
    if skipped:
        lines += ["Не трогаю:", *(f"  - {why}" for why in skipped)]
    preview = "\n".join([*lines, *tail])
    return drafts.create(conn, kind, payload, preview, now), preview


def _gallery_guard(fn):
    """Сбои DeviantArt во время подготовки — словами, черновик не создаётся."""
    def wrapped(*args, **kwargs):
        from . import da_api

        try:
            return fn(*args, **kwargs)
        except da_api.NeedsReconnect:
            return None, da_api.RECONNECT
        except da_api.RateLimited:
            return None, "DeviantArt просит притормозить — черновик не готов, попробуй через несколько минут."
        except da_api.DAApiError as e:
            return None, f"Не получилось прочитать её галерею, черновик не готов: {e}"
    return wrapped


def prepare_da_close(conn, cfg: Config, works: list[str] | None = None, word: str = "CLOSED", folder: str = "",
                     comment: str = "", record_sales: bool = True, from_folder: str = "",
                     now: float | None = None) -> tuple[str | None, str]:
    """Закрыть её аукционы: метка в названии, папка, её комментарий, продажа. (id черновика или None, текст).

    works — ссылки, da:123 или её названия; пусто — все её открытые аукционы (до GALLERY_MAX).
    comment — только её текст, слово в слово; слоты {winner} {amount} из ставок."""
    from . import da_api

    word = " ".join(str(word or "").split()) or "CLOSED"
    if not WORD_RE.match(word):
        return None, "Слово для названия — коротко, до 20 букв или цифр, например CLOSED или SOLD."
    comment = str(comment or "").strip()
    if comment and (why := check_comment_template(comment)):
        return None, f"Комментарий не подходит: {why}."
    folder, from_folder = " ".join(str(folder or "").split()), " ".join(str(from_folder or "").split())
    if len(folder) > da_api.FOLDER_NAME_MAX:
        return None, f"Название папки длиннее {da_api.FOLDER_NAME_MAX} символов."
    if from_folder and not folder:
        return None, "Из какой папки убрать — есть, а куда положить — нет: назови папку (folder)."
    refs = [str(w).strip() for w in works or [] if str(w).strip()]
    if len(refs) > GALLERY_MAX:
        return None, f"За раз не больше {GALLERY_MAX} работ — раздели на несколько черновиков."
    scopes = [*da_api.EDIT_SCOPES, *(da_api.GALLERY_SCOPES if folder else ()),
              *(da_api.COMMENT_SCOPES if comment else ())]
    client, username, why = _gallery_ctx(cfg, scopes)
    if why:
        return None, why
    more = ""
    if not refs:
        from . import selling

        rows = open_auctions(conn, limit=500, now=now)
        if not rows:
            return None, ("Открытых аукционов у неё в базе помощника нет. Проверь ник на DA в настройках и что "
                          "check:run source=deviantart уже прошёл — или назови работы ссылками.")
        # Срок ещё не прошёл — без её прямого слова не закрываем: иначе одно «го» назовёт победителем
        # того, кто лидирует в идущем аукционе.
        running, ended = [], []
        for r in rows:
            end = selling.item_end(conn, r["id"], _tz(cfg))
            if end and end.end_ts and end.end_ts > (now or time.time()):
                running.append(f"«{r['title'][:60]}» — до {selling.end_text(end, _tz(cfg))}")
            else:
                ended.append(r)
        still = ("Ещё идут, их не трогаю (закрыть раньше срока — только если она сама назовёт работу): "
                 + "; ".join(running[:10]) + (f" и ещё {len(running) - 10}" if len(running) > 10 else "")
                 if running else "")
        if not ended:
            return None, f"Закрывать пока нечего. {still}"
        rows = ended
        if len(rows) > GALLERY_MAX:
            more = (f"Открытых аукционов {len(rows)}: в этом черновике {GALLERY_MAX} самых свежих, "
                    "остальные — следующим черновиком.")
        more = "\n".join(x for x in (more, still) if x)
        refs = [r["id"] for r in reversed(rows[:GALLERY_MAX])]  # самые свежие, по порядку выставления
    return _prepare_close(conn, cfg, client, username, refs, word, folder, from_folder, comment, record_sales,
                          more, now)


@_gallery_guard
def _prepare_close(conn, cfg: Config, client: Any, username: str, refs: list[str], word: str, folder: str,
                   from_folder: str, comment: str, record_sales: bool, more: str,
                   now: float | None) -> tuple[str | None, str]:
    from . import da_api, selling
    from .classify import status_of

    folders = client.gallery_folders(username) if folder else []
    target = _find_folder(folders, folder) if folder else None
    source = _find_folder(folders, from_folder) if from_folder else None
    if from_folder and not source:
        return None, f"В её галерее нет папки «{from_folder}». Есть: {_folder_names(folders)}."
    if source and target and source["folder_id"] == target["folder_id"]:
        return None, "Папка «откуда» и «куда» — одна и та же."
    target_id = target["folder_id"] if target else None
    target_name = target["name"] if target else folder
    source_id = source["folder_id"] if source else None
    source_name = source["name"] if source else ""
    cache: dict = {}
    entries: list[dict] = []
    skipped: list[str] = []
    blocks: list[str] = []
    seen: set[str] = set()
    unknown_word = False
    for ref in refs:
        found, why = _resolve_work(conn, client, username, ref, cache)
        if not found:
            skipped.append(why)
            continue
        if found["uuid"] in seen:
            continue
        seen.add(found["uuid"])
        try:
            state = client.edit_state(found["uuid"])
        except (da_api.RateLimited, da_api.NotConnected):
            raise
        except da_api.DAApiError as e:
            skipped.append(f"«{found['title'] or ref}»: {e}")
            continue
        title = state["title"]
        if state["author"] and state["author"].casefold() != username.casefold():
            skipped.append(f"«{title}»: это не её работа (автор {state['author']}) — чужие не трогаю")
            continue
        new_title, title_note = closed_title(title, word)
        if new_title and len(new_title) > TITLE_MAX:
            skipped.append(f"«{title}»: с пометкой {word} название станет длиннее {TITLE_MAX} символов — пусть она "
                           "придумает короткое (prepare_gallery:rename)")
            continue
        if new_title and status_of(new_title) != "closed":
            unknown_word = True
        in_folders = {g["folder_id"] for g in state["galleries"]}
        in_target = target_id is not None and target_id in in_folders
        move_from = source_id if source_id in in_folders else None
        info = _auction_info(conn, found["item_id"])
        text, comment_note = None, ""
        if comment:
            if not state["can_post_comment"]:
                comment_note = "комментарий не напишу: комментарии под работой закрыты"
            else:
                text, comment_note = render_comment(comment, info.get("winner"), info.get("amount_text"))
                if text is not None and _comment_journaled(conn, found["uuid"], text):
                    text, comment_note = None, "этот комментарий я уже писала под работой — второй раз не напишу"
        sale, sale_note = None, ""
        if record_sales and found["item_id"]:
            existing = conn.execute("SELECT * FROM sales WHERE item_id = ?", (found["item_id"],)).fetchone()
            if existing:
                sale_note = f"продажа уже записана: {existing['buyer'] or '?'}, статус {existing['status']}"
            elif info.get("winner"):
                item = db.get_item(conn, found["item_id"])
                lot_type = item["lot_type"] if item else "auction"
                sale = {"title": title, "buyer": info["winner"], "amount": info.get("amount"),
                        "currency": info.get("currency"), "format": lot_type if lot_type in ("auction", "bta") else "auction",
                        "note": info.get("note") or ""}
            else:
                sale_note = "ставок не нашла — продажу не записываю"
        row = db.get_item(conn, found["item_id"]) if found["item_id"] else None
        mark = row is not None and (row["status"] or "") != "closed"
        if not new_title and not (folder and not in_target) and text is None and not sale and not mark:
            skipped.append(f"«{title}»: уже закрыта, делать нечего")
            continue
        n = len(entries) + 1
        lines = [f"{n}) {title}", f"   {state['url'] or found['url']}"]
        end = selling.item_end(conn, found["item_id"], _tz(cfg))
        if end and end.end_ts and end.end_ts > (now or time.time()):
            lines.append(f"   ⚠ аукцион ещё идёт до {selling.end_text(end, _tz(cfg))} — закрою раньше срока, "
                         "победителем будет тот, кто лидирует сейчас")
        lines.append(f"   Название: «{title}» → «{new_title}»" if new_title else f"   Название: не меняю ({title_note})")
        if folder:
            if in_target:
                lines.append(f"   Папка: уже в «{target_name}»")
            elif move_from:
                lines.append(f"   Папка: переложу из «{source_name}» в «{target_name}»")
            else:
                lines.append(f"   Папка: добавлю в «{target_name}» (из других папок не убираю)")
        if text is not None:
            lines.append(f"   Комментарий под работой (её текст, слово в слово): «{text}»")
        elif comment_note:
            lines.append(f"   {comment_note[:1].upper()}{comment_note[1:]}")
        if sale:
            amount = sales_money(sale["amount"], sale["currency"])
            lines.append(f"   Продажа: запишу «ждёт оплаты» — {sale['buyer']}, {amount}"
                         + (f" ({sale['note']})" if sale["note"] else ""))
        elif sale_note:
            lines.append(f"   Продажа: {sale_note}")
        if mark:
            lines.append("   В базе помощника отмечу аукцион закрытым.")
        blocks.append("\n".join(lines))
        entries.append({"item_id": found["item_id"], "uuid": found["uuid"], "url": state["url"] or found["url"],
                        "old_title": title, "new_title": new_title, "to_folder": bool(folder) and not in_target,
                        "move_from": move_from, "comment": text, "sale": sale, "mark_closed": mark})
    if not entries:
        return None, "Делать нечего:\n" + "\n".join(f"- {s}" for s in skipped) if skipped else "Делать нечего."
    create = bool(folder) and not target and any(e["to_folder"] for e in entries)
    payload = {"title": f"закрыть аукционы: {len(entries)}", "username": username, "word": word,
               "noai": cfg.posting.default_noai, "is_ai_generated": cfg.posting.default_is_ai_generated,
               "target_folder": {"name": target_name, "folder_id": target_id} if folder else None,
               "source_folder": source_name, "works": entries}
    head = [f"Закрыть аукционы на DeviantArt: {len(entries)} шт. Одно её «го» — и я сделаю всё по очереди, "
            f"с паузой {PACE_S[0] // 60}–{PACE_S[1] // 60} минуты между работами."]
    if more:
        head.append(more)
    if create:
        head.append(f"Сначала создам в её галерее папку «{folder}» — её пока нет.")
    tail = []
    n_comments = sum(1 for e in entries if e["comment"])
    left = comment_quota(conn, cfg, now)[0] if n_comments else 0
    if n_comments > left:
        tail.append(f"Комментариев {n_comments}, а суточный лимит её комментариев позволяет ещё {left}: лишние не "
                    "напишу, такие работы останутся открытыми в базе — допишу следующим черновиком.")
    if unknown_word:
        tail.append(f"Слово «{word}» помощник не узнаёт как «закрыто» — для учёта ставок лучше CLOSED или SOLD.")
    if any(e["new_title"] for e in entries):
        tail.append(_gallery_note(cfg))
    return _gallery_draft(conn, "da_close", payload, head, blocks, skipped, tail, now)


def sales_money(amount, currency) -> str:
    from . import sales

    return sales.money(amount, currency) if amount is not None else "сумма неизвестна"


def prepare_da_rename(conn, cfg: Config, renames: dict | None, now: float | None = None) -> tuple[str | None, str]:
    """Переименовать её выложенные работы: {ссылка, da:123 или нынешнее название: новое название (её слова)}."""
    from . import da_api

    pairs = [(str(k).strip(), " ".join(str(v or "").split())) for k, v in (renames or {}).items() if str(k).strip()]
    if not pairs:
        return None, "Нужно хотя бы одно: какая работа → новое название."
    if len(pairs) > GALLERY_MAX:
        return None, f"За раз не больше {GALLERY_MAX} работ — раздели на несколько черновиков."
    client, username, why = _gallery_ctx(cfg, da_api.EDIT_SCOPES)
    if why:
        return None, why
    return _prepare_rename(conn, cfg, client, username, pairs, now)


@_gallery_guard
def _prepare_rename(conn, cfg: Config, client: Any, username: str, pairs: list[tuple[str, str]],
                    now: float | None) -> tuple[str | None, str]:
    from . import da_api

    cache: dict = {}
    entries: list[dict] = []
    blocks: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for ref, new_title in pairs:
        if not new_title:
            skipped.append(f"«{ref}»: пустое новое название")
            continue
        if len(new_title) > TITLE_MAX:
            skipped.append(f"«{ref}»: новое название длиннее {TITLE_MAX} символов ({len(new_title)}) — пусть сократит")
            continue
        found, why = _resolve_work(conn, client, username, ref, cache)
        if not found:
            skipped.append(why)
            continue
        if found["uuid"] in seen:
            skipped.append(f"«{ref}»: эта работа уже есть выше")
            continue
        seen.add(found["uuid"])
        try:
            state = client.edit_state(found["uuid"])
        except (da_api.RateLimited, da_api.NotConnected):
            raise
        except da_api.DAApiError as e:
            skipped.append(f"«{found['title'] or ref}»: {e}")
            continue
        if state["author"] and state["author"].casefold() != username.casefold():
            skipped.append(f"«{state['title']}»: это не её работа — чужие не трогаю")
            continue
        if state["title"] == new_title:
            skipped.append(f"«{new_title}»: уже так называется")
            continue
        n = len(entries) + 1
        blocks.append(f"{n}) «{state['title']}» → «{new_title}»\n   {state['url'] or found['url']}")
        entries.append({"item_id": found["item_id"], "uuid": found["uuid"], "url": state["url"] or found["url"],
                        "old_title": state["title"], "new_title": new_title})
    if not entries:
        return None, "Делать нечего:\n" + "\n".join(f"- {s}" for s in skipped)
    payload = {"title": f"переименовать работы: {len(entries)}", "username": username,
               "noai": cfg.posting.default_noai, "is_ai_generated": cfg.posting.default_is_ai_generated,
               "works": entries}
    head = [f"Переименовать работы на DeviantArt: {len(entries)} шт. Названия — её слова, без изменений. "
            f"Одно её «го» — и я сделаю по очереди, с паузой {PACE_S[0] // 60}–{PACE_S[1] // 60} минуты."]
    return _gallery_draft(conn, "da_rename", payload, head, blocks, skipped, [_gallery_note(cfg)], now)


def prepare_da_folders(conn, cfg: Config, create: list[str] | None = None, rename: dict | None = None,
                       move: dict | None = None, remove_from: str = "", now: float | None = None) -> tuple[str | None, str]:
    """Папки её галереи: создать, переименовать {старое: новое}, разложить работы {папка: [работы]}.
    remove_from — из какой папки убрать работы, которые кладём в новую (иначе они остаются и там)."""
    from . import da_api

    def clean(v) -> str:
        return " ".join(str(v or "").split())

    creates = list(dict.fromkeys(c for c in (clean(x) for x in create or []) if c))
    renames = [(clean(k), clean(v)) for k, v in (rename or {}).items() if clean(k)]
    moves = [(clean(k), [str(w).strip() for w in (v or []) if str(w).strip()]) for k, v in (move or {}).items()
             if clean(k)]
    moves = [(k, v) for k, v in moves if v]
    remove_from = clean(remove_from)
    if not creates and not renames and not moves:
        return None, "Нечего делать: нужно создать, переименовать папку или разложить работы."
    names = creates + [n for _, n in renames] + [k for k, _ in moves]
    long = [n for n in names if len(n) > da_api.FOLDER_NAME_MAX]
    if long or any(not new for _, new in renames):
        return None, (f"Название папки — от 1 до {da_api.FOLDER_NAME_MAX} символов"
                      + (f": длинные — {', '.join(long)}" if long else "") + ".")
    if sum(len(v) for _, v in moves) > GALLERY_MAX:
        return None, f"За раз не больше {GALLERY_MAX} работ — раздели на несколько черновиков."
    client, username, why = _gallery_ctx(cfg, da_api.GALLERY_SCOPES)
    if why:
        return None, why
    return _prepare_folders(conn, cfg, client, username, creates, renames, moves, remove_from, now)


@_gallery_guard
def _prepare_folders(conn, cfg: Config, client: Any, username: str, creates: list[str],
                     renames: list[tuple[str, str]], moves: list[tuple[str, list[str]]], remove_from: str,
                     now: float | None) -> tuple[str | None, str]:
    folders = client.gallery_folders(username)
    blocks: list[str] = []
    skipped: list[str] = []
    plan_create: list[str] = []
    plan_rename: list[dict] = []
    plan_move: list[dict] = []
    renamed_to: dict[str, dict] = {}
    for old, new in renames:
        f = _find_folder(folders, old)
        if not f:
            return None, f"В её галерее нет папки «{old}». Есть: {_folder_names(folders)}."
        clash = _find_folder(folders, new)
        if clash and clash["folder_id"] != f["folder_id"]:
            return None, f"Папка «{clash['name']}» уже есть — две папки с одним названием путают."
        if f["name"] == new:
            skipped.append(f"папка «{new}» уже так называется")
            continue
        if f.get("description") is None:
            return None, (f"Папку «{f['name']}» не переименую: DeviantArt не показал её описание, а без него оно "
                          "может стереться. Её лучше переименовать на сайте.")
        plan_rename.append({"folder_id": f["folder_id"], "old": f["name"], "new": new})
        renamed_to[_norm_folder(new)] = f
    for name in creates:
        if _find_folder(folders, name) or _norm_folder(name) in renamed_to:
            skipped.append(f"папка «{name}» уже есть — не создаю вторую")
        else:
            plan_create.append(name)
    source = _find_folder(folders, remove_from) if remove_from else None
    if remove_from and not source:
        return None, f"В её галерее нет папки «{remove_from}». Есть: {_folder_names(folders)}."
    cache: dict = {}
    for folder_name, refs in moves:
        target = _find_folder(folders, folder_name) or renamed_to.get(_norm_folder(folder_name))
        if not target and _norm_folder(folder_name) not in {_norm_folder(n) for n in plan_create}:
            plan_create.append(folder_name)
        found_works: list[dict] = []
        for ref in refs:
            found, why = _resolve_work(conn, client, username, ref, cache)
            if not found:
                skipped.append(why)
            elif all(w["uuid"] != found["uuid"] for w in found_works):
                found_works.append(found)
        brief = client.deviations_brief([w["uuid"] for w in found_works]) if found_works else {}
        copy_ids, move_ids, lines = [], [], []
        shown = target["name"] if target else folder_name
        for w in found_works:
            info = brief.get(w["uuid"])
            title = (info or {}).get("title") or w["title"] or w["url"]
            if not info:
                skipped.append(f"«{title}»: DeviantArt не показал эту работу")
                continue
            if info["author"] and info["author"].casefold() != username.casefold():
                skipped.append(f"«{title}»: это не её работа — чужие не трогаю")
                continue
            in_folders = {g["folder_id"] for g in info["galleries"]}
            if target and target["folder_id"] in in_folders:
                skipped.append(f"«{title}»: уже в папке «{shown}»")
            elif source and source["folder_id"] in in_folders:
                move_ids.append(w["uuid"])
                lines.append(f"   - «{title}»: переложу из «{source['name']}»")
            else:
                copy_ids.append(w["uuid"])
                lines.append(f"   - «{title}»: добавлю (из других папок не убираю)")
        if copy_ids or move_ids:
            plan_move.append({"folder": shown, "folder_id": target["folder_id"] if target else None,
                              "copy": copy_ids, "move": move_ids, "source_id": source["folder_id"] if source else None})
            blocks.append("\n".join([f"Разложу в папку «{shown}»:", *lines]))
    if plan_create:
        blocks.insert(0, "Создам папки: " + ", ".join(f"«{n}»" for n in plan_create))
    if plan_rename:
        blocks.insert(1 if plan_create else 0,
                      "Переименую папки: " + "; ".join(f"«{r['old']}» → «{r['new']}»" for r in plan_rename)
                      + " (описание папки оставлю прежним)")
    if not plan_create and not plan_rename and not plan_move:
        return None, "Делать нечего:\n" + "\n".join(f"- {s}" for s in skipped)
    payload = {"title": "папки галереи", "username": username, "create": plan_create, "rename": plan_rename,
               "move": plan_move}
    head = ["Папки её галереи на DeviantArt. Одно её «го» — и я сделаю всё по очереди."]
    return _gallery_draft(conn, "da_folders", payload, head, blocks, skipped, [], now)


# --- Её галерея: отправка по «го» ---

def _steps_schema(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS draft_steps (draft_id TEXT NOT NULL, idx INTEGER NOT NULL, "
                 "label TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, ts REAL, PRIMARY KEY (draft_id, idx))")
    conn.execute("CREATE TABLE IF NOT EXISTS draft_stop (draft_id TEXT PRIMARY KEY, ts REAL)")


STOPPED_BY_HER = "она попросила остановиться"


def request_stop(conn, draft_id: str, now: float | None = None) -> bool:
    """Она сказала «стоп», а черновик галереи уже идёт: текущую работу доделаю, следующие не трону.
    False — это не идущий черновик галереи."""
    row = drafts.get(conn, draft_id, now)
    if not row or row["kind"] not in (*GALLERY_KINDS, "da_ping") or row["status"] != "sending":
        return False
    _steps_schema(conn)
    conn.execute("INSERT OR REPLACE INTO draft_stop (draft_id, ts) VALUES (?, ?)", (row["id"], now or time.time()))
    conn.commit()
    return True


def _stop_requested(conn, draft_id: str) -> bool:
    _steps_schema(conn)
    return conn.execute("SELECT 1 FROM draft_stop WHERE draft_id = ?", (draft_id,)).fetchone() is not None


def draft_steps(conn, draft_id: str) -> list:
    _steps_schema(conn)
    return conn.execute("SELECT * FROM draft_steps WHERE draft_id = ? ORDER BY idx", (draft_id.strip(),)).fetchall()


def describe_progress(conn, draft_id: str) -> str:
    rows = draft_steps(conn, draft_id)
    if not rows:
        return ""
    done = sum(1 for r in rows if r["status"] in GALLERY_DONE)
    lines = [f"По шагам ({done} из {len(rows)} готово):"]
    for r in rows:
        detail = f": {r['detail']}" if r["detail"] else ""
        lines.append(f"{r['idx'] + 1}) {r['label']} — {STEP_NAMES.get(r['status'], r['status'])}{detail}")
    return "\n".join(lines)


def _needed_scopes(kind: str, payload: dict) -> list[str]:
    from . import da_api

    if kind == "da_folders":
        return list(da_api.GALLERY_SCOPES)
    works = payload.get("works") or []
    return [*da_api.EDIT_SCOPES, *(da_api.GALLERY_SCOPES if any(w.get("to_folder") for w in works) else ()),
            *(da_api.COMMENT_SCOPES if any(w.get("comment") for w in works) else ())]


class _Stop(Exception):
    """Дальше не идём (DeviantArt что-то сбросил после правки)."""


class _GalleryRun:
    def __init__(self, conn, cfg: Config, draft_id: str, now: float | None, lock_timeout_s: float, sleep, rng):
        self.conn, self.cfg, self.draft_id, self.now = conn, cfg, draft_id, now
        self.lock_timeout_s, self.sleep, self.rng = lock_timeout_s, sleep, rng or random
        self.writes = 0
        self.stop = ""
        self.stopped_by_her = False
        self.folder_ids: dict[str, str] = {}
        self.client: Any = None
        self._payload_username = ""

    def _do(self, fn, *args, **kwargs):
        """Запрос, который меняет её DeviantArt. В счёт изменений идёт, если прошёл или его исход
        неизвестен; точный отказ (429, нет прав, 400) ничего не изменил — её «го» тогда в силе."""
        from . import da_api

        try:
            out = fn(*args, **kwargs)
        except da_api.UncertainPublish:
            self.writes += 1
            raise
        except da_api.DAApiError:
            raise
        except Exception:
            self.writes += 1  # непонятная ошибка после запроса — считаем, что могло измениться
            raise
        self.writes += 1
        return out

    def _batch(self, fn, *args, verb: str, total: int, parts: list[str]) -> None:
        """Перенос пачками: если упала не первая пачка, в итоге видно, сколько работ уже перенесено."""
        from . import da_api

        done = [0]

        def on_chunk(n: int) -> None:
            done[0] += n

        try:
            self._do(fn, *args, on_chunk=on_chunk)
        except Exception as e:
            if done[0]:
                parts.append(f"{verb} {done[0]} из {total}")
                if isinstance(e, da_api.DAApiError) and not isinstance(e, da_api.UncertainPublish):
                    self.writes += 1  # первые пачки прошли
            raise
        parts.append(f"{verb} {total}")

    def _check_stop(self) -> None:
        if not self.stop and _stop_requested(self.conn, self.draft_id):
            self.stop, self.stopped_by_her = STOPPED_BY_HER, True

    def _ts(self) -> float:
        return self.now or time.time()

    def _step(self, idx: int, label: str, status: str, detail: str = "") -> None:
        _quietly(self._step_write, idx, label, status, detail)

    def _step_write(self, idx, label, status, detail):
        self.conn.execute("INSERT OR REPLACE INTO draft_steps (draft_id, idx, label, status, detail, ts) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (self.draft_id, idx, label, status, detail[:1000], self._ts()))
        self.conn.commit()

    def _heartbeat(self) -> None:
        # Долгий черновик не должен выглядеть «зависшим» (drafts.STUCK_AFTER_S): отмечаемся на каждом шаге.
        _quietly(self._heartbeat_write)

    def _heartbeat_write(self):
        self.conn.execute("UPDATE drafts SET sent_ts = ? WHERE id = ? AND status = 'sending'",
                          (time.time() if self.now is None else self.now, self.draft_id))
        self.conn.commit()

    def execute(self, kind: str, payload: dict) -> tuple[str, str]:
        from . import da_api

        try:
            self.client = da_client(self.cfg)
            connected = self.client.is_connected()
        except SettingsError as e:
            return "failed", f"не сделала: {e}"
        if not connected:
            return "pending", ("ничего не сделала: DeviantArt не подключён. Пусть подключит (accounts:da_connect), потом снова "
                               "submit_draft — её «го» на этот черновик в силе.")
        try:
            self.client.require_scopes(*_needed_scopes(kind, payload))
        except da_api.NeedsReconnect:
            return "pending", (f"ничего не изменила: {da_api.RECONNECT} Потом снова submit_draft — её «го» на этот "
                               "черновик в силе.")
        units = {"da_close": self._close_units, "da_rename": self._rename_units,
                 "da_folders": self._folder_units}[kind](payload)
        _steps_schema(self.conn)
        self.conn.execute("DELETE FROM draft_steps WHERE draft_id = ?", (self.draft_id,))
        self.conn.commit()
        for idx, (label, _, _) in enumerate(units):
            self._step(idx, label, "waiting")
        results: list[tuple[str, str, str]] = []
        for idx, (label, fn, pace) in enumerate(units):
            self._check_stop()
            if not self.stop and pace:
                self.sleep(self.rng.uniform(*pace))
                self._check_stop()  # пауза до двух минут: «стоп» мог прийти за это время
            if self.stop:
                results.append((label, "notrun", ""))
                self._step(idx, label, "notrun")
                continue
            self._heartbeat()
            parts: list[str] = []
            before = self.writes
            try:
                with send_lock(self.lock_timeout_s):
                    status, detail = fn(parts)
            except SendBusy:
                status, detail = ("partial" if parts else "notrun"), "; ".join(parts)
                self.stop = "шла другая отправка, не дождалась своей очереди"
            except da_api.NeedsReconnect:
                status, detail = ("partial" if parts else "notrun"), "; ".join(parts)
                self.stop = da_api.RECONNECT
            except da_api.NotConnected as e:
                status, detail = ("partial" if parts else "notrun"), "; ".join(parts)
                self.stop = f"{e} (accounts:da_connect)"
            except da_api.RateLimited:
                status, detail = ("partial" if parts else "notrun"), "; ".join(parts)
                self.stop = "DeviantArt попросил притормозить — остальное не делала, можно повторить позже"
            except _Stop as e:
                # Название уже поменялось — работа изменена, хоть и не так, как надо.
                status, detail = "partial", "; ".join([*parts, str(e)])
                self.stop = "после правки DeviantArt сбросил часть настроек работы — остальные работы не трогала"
            except da_api.UncertainPublish as e:
                status, detail = "unknown", "; ".join([*parts, f"{drafts.UNKNOWN_RESULT}: {e}"])
                self.stop = "DeviantArt не ответил, а изменение могло пройти — дальше не иду"
            except Exception as e:  # noqa: BLE001
                if isinstance(e, da_api.DAApiError) and _uncertain(e):
                    # Все записи идут через _write: их потерянный ответ — UncertainPublish. Значит, здесь
                    # не ответило чтение — оно ничего не меняло.
                    status, detail = ("partial" if parts else "notrun"), "; ".join(parts)
                    self.stop = f"DeviantArt не ответил ({e}) — дальше не пошла, можно повторить позже"
                elif isinstance(e, da_api.DAApiError):
                    status, detail = ("partial" if parts else "failed"), "; ".join([*parts, f"не вышло: {e}"])
                elif self.writes > before:
                    status, detail = "unknown", "; ".join([*parts, f"{drafts.UNKNOWN_RESULT}: {e}"])
                    self.stop = "что-то пошло не так после изменения на DeviantArt — дальше не иду"
                else:
                    status, detail = ("partial" if parts else "failed"), "; ".join([*parts, f"не вышло: {e}"])
                    self.stop = f"что-то пошло не так ({type(e).__name__}) — дальше не пошла"
            results.append((label, status, detail))
            self._step(idx, label, status, detail)
        return self._outcome(kind, results)

    def _outcome(self, kind: str, results: list[tuple[str, str, str]]) -> tuple[str, str]:
        statuses = [s for _, s, _ in results]
        if self.writes == 0 and self.stopped_by_her:
            return "cancelled", "Остановила, как она попросила: на DeviantArt ничего не меняла."
        if self.writes == 0 and self.stop and "unknown" not in statuses:
            return "pending", (f"ничего не изменила: {self.stop.rstrip('.')}. Её «го» на этот черновик в силе — "
                               "можно снова submit_draft.")
        what = {"da_close": "Закрытие аукционов", "da_rename": "Переименование", "da_folders": "Папки галереи"}[kind]
        good = sum(1 for s in statuses if s in GALLERY_DONE)
        lines = [f"{what}: готово {good} из {len(results)}."]
        lines += [f"- {label} — {STEP_NAMES.get(s, s)}" + (f": {d}" if d else "") for label, s, d in results]
        if self.stop:
            lines.append(f"Остановилась: {self.stop.rstrip('.')}.")
        if "unknown" in statuses:
            return "unknown", "\n".join([*lines, drafts.UNKNOWN_RESULT])
        if good == len(results):
            return "sent", "\n".join(lines)
        if not good and "partial" not in statuses:
            return "failed", "\n".join(lines)
        return "partial", "\n".join(lines)

    # --- единицы работы ---

    def _paced(self, units: list, pace) -> list:
        return [(label, fn, pace if i else None) for i, (label, fn) in enumerate(units)]

    def _close_units(self, payload: dict) -> list:
        units: list = []
        target = payload.get("target_folder")
        if target and target.get("folder_id"):
            self.folder_ids[_norm_folder(target["name"])] = target["folder_id"]
        elif target and any(w.get("to_folder") for w in payload["works"]):
            units.append((f"папка «{target['name']}»", partial(self._ensure_folder, target["name"]), None))
        works = self._paced([(f"«{w['old_title']}»", self._closer(payload, w)) for w in payload["works"]], PACE_S)
        return units + works

    def _rename_units(self, payload: dict) -> list:
        return self._paced([(f"«{w['old_title']}»", self._renamer(payload, w)) for w in payload["works"]], PACE_S)

    def _folder_units(self, payload: dict) -> list:
        units: list = [(f"создать папку «{n}»", partial(self._ensure_folder, n)) for n in payload.get("create") or []]
        units += [(f"переименовать «{r['old']}» → «{r['new']}»", partial(self._rename_folder, r))
                  for r in payload.get("rename") or []]
        units += [(f"разложить в «{m['folder']}» ({len(m['copy']) + len(m['move'])} работ)",
                   partial(self._fill_folder, payload["username"], m))
                  for m in payload.get("move") or []]
        return [(label, fn, FOLDER_PACE_S if i else None) for i, (label, fn) in enumerate(units)]

    def _ensure_folder(self, name: str, parts: list[str]) -> tuple[str, str]:
        username = self._username()
        existing = _find_folder(self.client.gallery_folders(username), name)
        if existing:
            self.folder_ids[_norm_folder(name)] = existing["folder_id"]
            return "already", f"папка «{existing['name']}» уже есть"
        made = self._do(self.client.create_folder, name, username=username)
        self.folder_ids[_norm_folder(name)] = made["folder_id"]
        return "done", f"создала папку «{made['name']}»"

    def _username(self) -> str:
        return self._payload_username

    def _rename_folder(self, r: dict, parts: list[str]) -> tuple[str, str]:
        current = next((f for f in self.client.gallery_folders(self._username()) if f["folder_id"] == r["folder_id"]),
                       None)
        if not current:
            return "skipped", "этой папки больше нет"
        if current["name"] == r["new"]:
            return "already", "папка уже так называется"
        if current["name"] != r["old"]:
            return "skipped", f"папку переименовали после черновика (сейчас «{current['name']}») — не трогала"
        if current.get("description") is None:
            return "skipped", "DeviantArt не показал описание папки — без него оно могло бы стереться, не трогала"
        self._do(self.client.rename_folder, r["folder_id"], r["new"], current["description"])
        return "done", f"теперь «{r['new']}»"

    def _fill_folder(self, username: str, m: dict, parts: list[str]) -> tuple[str, str]:
        folder_id = m.get("folder_id") or self.folder_ids.get(_norm_folder(m["folder"]))
        if not folder_id:
            found = _find_folder(self.client.gallery_folders(username), m["folder"])
            folder_id = found["folder_id"] if found else None
        if not folder_id:
            return "failed", f"папки «{m['folder']}» нет (не создалась) — работы не разложила"
        if m["move"]:
            self._batch(self.client.move_deviations, m["source_id"], folder_id, m["move"], verb="переложила",
                        total=len(m["move"]), parts=parts)
        if m["copy"]:
            self._batch(self.client.copy_deviations, folder_id, m["copy"], verb="добавила", total=len(m["copy"]),
                        parts=parts)
        return "done", "; ".join(parts)

    def _edit_title(self, payload: dict, w: dict, parts: list[str]) -> tuple[str | None, dict]:
        """Поменять название, если оно всё ещё такое, как в превью. → (причина пропуска или None, state)."""
        from . import da_api

        state = self.client.edit_state(w["uuid"])
        current = state["title"]
        if current != w["old_title"]:
            if w.get("new_title") and current == w["new_title"]:
                parts.append("название уже было новым")
                return None, state
            return f"название поменялось после черновика (сейчас «{current}») — эту работу не трогала", state
        if not w.get("new_title"):
            return None, state
        self._do(self.client.edit_deviation, w["uuid"], state, title=w["new_title"],
                 noai=bool(payload.get("noai", True)), is_ai_generated=bool(payload.get("is_ai_generated", False)))
        parts.append(f"название «{w['new_title']}»")
        try:
            lost = self.client.verify_edit(w["uuid"], state, w["new_title"])
        except da_api.DAApiError as e:
            # Без перепроверки не знаем, не сбросил ли DeviantArt что-то, — остальные работы не трогаем.
            raise _Stop(f"перепроверить работу после правки не смогла ({e}) — загляни в неё на сайте") from e
        if lost:
            raise _Stop(f"но DeviantArt после правки сбросил: {'; '.join(lost)} — проверь работу на сайте")
        return None, state

    def _note_item(self, w: dict, title: str, closed: bool) -> None:
        if not w.get("item_id"):
            return
        if closed:
            self.conn.execute("UPDATE items SET title = ?, status = 'closed', closed_ts = COALESCE(closed_ts, ?), "
                              "updated_ts = ? WHERE id = ?", (title, self._ts(), self._ts(), w["item_id"]))
        else:
            self.conn.execute("UPDATE items SET title = ?, updated_ts = ? WHERE id = ?", (title, self._ts(), w["item_id"]))
        self.conn.commit()

    def _renamer(self, payload: dict, w: dict):
        def run(parts: list[str]) -> tuple[str, str]:
            skip, _ = self._edit_title(payload, w, parts)
            if skip:
                return "skipped", skip
            _quietly(self._note_item, w, w["new_title"], False)
            return ("already" if parts == ["название уже было новым"] else "done"), "; ".join(parts)
        return run

    def _closer(self, payload: dict, w: dict):
        from . import da_api

        def run(parts: list[str]) -> tuple[str, str]:
            skip, state = self._edit_title(payload, w, parts)
            if skip:
                return "skipped", skip
            title = w.get("new_title") or state["title"]
            _quietly(self._note_item, w, title, False)
            # Продажа — сразу после названия: если дальше DeviantArt остановит проход, она не потеряется.
            if w.get("sale"):
                parts.append(self._record_sale(w))
            problems = False
            if w.get("to_folder"):
                try:
                    parts.append(self._put_in_folder(payload, w, state))
                except (da_api.RateLimited, da_api.NotConnected, da_api.UncertainPublish):
                    raise
                except da_api.DAApiError as e:
                    problems = True
                    parts.append(f"папку не сделала: {e}")
            capped = False
            if w.get("comment") and comment_quota(self.conn, self.cfg, self.now)[0] <= 0:
                # Под замком отправки: суточный лимит её комментариев общий с напоминаниями и пингами.
                problems = capped = True
                parts.append(f"комментарий не написала: {COMMENT_LIMIT}")
            elif w.get("comment"):
                try:
                    note, ok = self._comment_once(payload["username"], w["uuid"], w["comment"])
                    problems = problems or not ok
                    parts.append(note)
                except (da_api.RateLimited, da_api.NotConnected, da_api.UncertainPublish):
                    raise
                except da_api.DAApiError as e:
                    problems = True
                    parts.append(f"комментарий не написала: {e}")
            # Закрытым в базе — только когда все шаги прошли до конца: оборванная работа попадёт в следующий
            # черновик «закрыть всё», и недоделанное (папка, комментарий) доделается.
            # Комментарий отложен лимитом — не закрываем в базе: следующий черновик «закрыть всё» его допишет.
            if w.get("mark_closed") and not capped:
                _quietly(self._note_item, w, title, True)
                parts.append("в базе отмечен закрытым")
            return ("partial" if problems else "done"), "; ".join(parts)
        return run

    def _put_in_folder(self, payload: dict, w: dict, state: dict) -> str:
        target = payload["target_folder"]
        folder_id = target.get("folder_id") or self.folder_ids.get(_norm_folder(target["name"]))
        if not folder_id:
            from .da_api import DAApiError

            raise DAApiError(f"папки «{target['name']}» нет (не создалась)", "no_folder")
        if any(g["folder_id"] == folder_id for g in state["galleries"]):
            return f"уже в папке «{target['name']}»"
        if w.get("move_from") and any(g["folder_id"] == w["move_from"] for g in state["galleries"]):
            self._do(self.client.move_deviations, w["move_from"], folder_id, [w["uuid"]])
            return f"переложила в «{target['name']}»"
        self._do(self.client.copy_deviations, folder_id, [w["uuid"]])
        return f"добавила в папку «{target['name']}»"

    def _comment_once(self, username: str, uuid: str, text: str) -> tuple[str, bool]:
        """Один комментарий с этим текстом под работой — никогда второй. → (итог словами, всё ли хорошо)."""
        state, note = comment_once(self.conn, self.client, username, uuid, text, self.now, do=self._do)
        return note, state != "unchecked"

    def _record_sale(self, w: dict) -> str:
        from . import sales

        s = w["sale"]
        try:
            sale_id = sales.add_sale(self.conn, title=s["title"], amount=s.get("amount"), currency=s.get("currency"),
                                     platform="deviantart", format=s.get("format") or "auction", buyer=s.get("buyer"),
                                     item_id=w["item_id"], source="auto", note=s.get("note") or "", now=self.now,
                                     if_new=True)
        except (sales.SaleError, sqlite3.Error) as e:
            return f"продажу не записала: {e}"
        return f"записала продажу [{sale_id}] «ждёт оплаты»" if sale_id else "продажа уже была записана"


def _send_gallery(conn, cfg: Config, draft_id: str, kind: str, now: float | None, lock_timeout_s: float,
                  sleep, rng) -> str:
    try:
        with send_lock(lock_timeout_s):
            payload = drafts.take_for_sending(conn, draft_id, kind, now)
    except SendBusy:
        drafts.note_waiting(conn, draft_id, BUSY_TEXT)
        return BUSY_TEXT
    except drafts.DraftError as e:
        return str(e)
    run = _GalleryRun(conn, cfg, draft_id, now, lock_timeout_s, sleep, rng)
    run._payload_username = payload.get("username") or ""
    try:
        status, result = run.execute(kind, payload)
    except Exception as e:  # noqa: BLE001 — исход неизвестен: что-то могло измениться
        status, result = "unknown", f"{drafts.UNKNOWN_RESULT} (ошибка: {type(e).__name__}: {e})"
    if status == "pending":
        drafts.requeue(conn, draft_id, result)
    elif status == "cancelled":
        drafts.finish(conn, draft_id, False, result, now, status="cancelled")
    else:
        drafts.finish(conn, draft_id, status == "sent", result, now, status=status)
    return result


# --- Персы Toyhouse и комментарии DA (напоминание об оплате, пинглист): тоже только по её «го» ---

TH_CHARACTER_KINDS = ("th_character_create", "th_character_transfer", "th_character_listing", "th_character_move")
COMMENT_KINDS = ("da_comment_reminder", "da_ping")
# Комментариев от её имени за сутки (вместе с комментариями при закрытии аукционов) и из них пингов.
COMMENTS_PER_DAY = 20
PINGS_PER_DAY = 10
PING_PACE_S = (150, 240)
COMMENT_LIMIT = "на сегодня лимит комментариев от её имени исчерпан (чтобы DeviantArt не счёл спамом) — завтра"


def _sender(kind: str):
    if kind == "th_reply":
        return _send_th_reply
    if kind in TH_CHARACTER_KINDS:
        return _send_th_character
    if kind == "da_comment_reminder":
        return _send_reminder
    return _send_da_publish


def _send_th_character(conn, cfg: Config, payload: dict, draft_id: str, now: float | None) -> tuple[str, str]:
    """Заполнить форму Toyhouse в её Chrome. Ничего не сохраняется и не отправляется: кнопку жмёт она,
    поэтому ни лимитов, ни «неизвестно, ушло ли»."""
    from . import th_characters as tc
    from .toyhouse import BrowserBusy, ToyhouseError

    try:
        res = tc.run_plan(payload)
    except BrowserBusy:
        return "pending", CHROME_BUSY
    except (ToyhouseError, ValueError) as e:
        return "failed", f"не получилось: {e}"
    except Exception as e:  # noqa: BLE001 — форма только заполняется, на сайт ничего не ушло
        return "failed", f"не получилось ({type(e).__name__}): {e}"
    return {"filled": "sent", "partial": "partial"}.get(res.get("state") or "", "failed"), res.get("message") or ""


def comment_quota(conn, cfg: Config, now: float | None = None) -> tuple[int, int]:
    """(сколько ещё комментариев можно за сутки, сколько из них пингов)."""
    since = (now or time.time()) - 86400
    # Настройка может только снизить потолок, не поднять.
    cap_comments = min(int(getattr(cfg.posting, "da_comments_per_day", COMMENTS_PER_DAY)), COMMENTS_PER_DAY)
    cap_pings = min(int(getattr(cfg.posting, "da_pings_per_day", PINGS_PER_DAY)), PINGS_PER_DAY)
    comments = cap_comments - drafts.count_sent(conn, "da_comment", since)
    pings = cap_pings - drafts.count_sent(conn, "da_ping", since)
    return max(0, comments), max(0, min(comments, pings))


def _comment_ctx(cfg: Config) -> tuple[Any, str]:
    """(клиент, её ник) для комментариев. Не подключена или нет прав — NotConnected / NeedsReconnect."""
    from . import da_api

    client = da_client(cfg)
    if not client.is_connected():
        raise da_api.NotConnected("DeviantArt не подключён: нужно accounts:da_connect")
    client.require_scopes(*da_api.COMMENT_SCOPES)
    return client, cfg.deviantart.my_username or (client.whoami().get("username") or "")


def _comment_ready(cfg: Config) -> str:
    from . import da_api

    try:
        _comment_ctx(cfg)
    except SettingsError as e:
        return f"DeviantArt не подключён: {e}"
    except da_api.NeedsReconnect:
        return da_api.RECONNECT
    except da_api.DAApiError as e:
        return str(e)
    return ""


def prepare_payment_reminder(conn, cfg: Config, sale_id: str, now: float | None = None) -> tuple[str | None, str]:
    """Черновик напоминания об оплате: её шаблон слово в слово, комментарием под её работой."""
    from . import selling

    sale_id = str(sale_id or "").strip()
    res = selling.payment_reminders(conn, now, older_than_days=0, limit=1000)
    d = next((x for x in res["due"] if str(x["sale_id"]) == sale_id), None)
    if d is None:
        s = next((x for x in res["skipped"] if str(x["sale_id"]) == sale_id), None)
        return None, (f"Не готовлю: {s['reason']}." if s else "Такой неоплаченной продажи нет (список — sales:unpaid).")
    if not d["text"]:
        return None, ("Нет её шаблона напоминания: попроси текст её словами и сохрани set_template"
                      "('payment_reminder', …), слоты {buyer} {amount} {title} {days} {url}.")
    if not (d.get("deviation_uuid") or d.get("deviation_url")):
        return None, "Не знаю, под какой работой писать: работы этой продажи нет в базе."
    why = _comment_ready(cfg)
    if why:
        return None, why
    if comment_quota(conn, cfg, now)[0] <= 0:
        return None, f"Не готовлю: {COMMENT_LIMIT}."
    payload = {"title": f"напоминание {d['buyer']}", "sale_id": d["sale_id"], "ref": d["ref"], "buyer": d["buyer"],
               "deviation_uuid": d.get("deviation_uuid"), "deviation_url": d.get("deviation_url"), "text": d["text"]}
    preview = "\n".join([
        f"Напоминание об оплате: {d['buyer']} — «{d['title']}», {d['amount']}, {d['days']} дн.",
        f"Комментарий под её работой: {d.get('deviation_url') or '—'}",
        "Текст (её шаблон, без изменений):",
        d["text"],
        "Как отправится: через API DeviantArt после её «го», один раз."])
    draft_id = drafts.create(conn, "da_comment_reminder", payload, preview, now)
    _quietly(selling.mark_reminded, conn, d["sale_id"], "draft", now)
    return draft_id, preview


def prepare_ping(conn, cfg: Config, url: str, now: float | None = None) -> tuple[str | None, str]:
    """Черновик пингов под её работой: её шаблон «pinglist» + до 10 @ников в комментарии."""
    from . import auctions, pinglist

    item = auctions.item_for_url(conn, str(url or ""))
    if not item:
        return None, "Этой работы нет в базе — сначала обнови её работы (check:run source=deviantart)."
    why = _comment_ready(cfg)
    if why:
        return None, why
    quota = comment_quota(conn, cfg, now)[1]
    if quota <= 0:
        return None, f"Не готовлю: {COMMENT_LIMIT}."
    _quietly(_release_dead_pings, conn, item["id"], now)
    res = pinglist.mention_comments(conn, deviation_id=item["id"], deviation_url=item["url"], title=item["title"],
                                    max_comments=min(quota, pinglist.MAX_COMMENTS), reserve_users=True, now=now)
    if not res["ok"]:
        return None, f"Не готовлю: {res['reason']}."
    payload = {"title": f"пинг: {item['title']}", "deviation_id": item["id"], "deviation_uuid": auctions.known_uuid(item),
               "deviation_url": item["url"], "comments": res["comments"], "ref": f"ping:{item['id']}"}
    lines = [f"Пинг под её работой «{item['title']}» — {item['url']}",
             f"Комментариев: {len(res['comments'])}, между ними пауза 2,5–4 мин."]
    for i, c in enumerate(res["comments"], 1):
        lines += [f"Комментарий {i} ({len(c['users'])} ник.):", c["text"]]
    if res["left_out"]:
        lines.append(f"Не вошли сейчас (лимит): {len(res['left_out'])} — в следующий раз.")
    lines.append("Текст — её шаблон без изменений; @ники — те, кто сам просил в пинглист.")
    preview = "\n".join(lines)
    return drafts.create(conn, "da_ping", payload, preview, now), preview


def _send_reminder(conn, cfg: Config, payload: dict, draft_id: str, now: float | None) -> tuple[str, str]:
    from . import da_api, selling

    why = selling.reminder_block(conn, payload["sale_id"], now)
    if why:
        return "failed", f"не написала: {why}"
    if comment_quota(conn, cfg, now)[0] <= 0:
        return "failed", f"не написала: {COMMENT_LIMIT}"
    try:
        client, username = _comment_ctx(cfg)
    except SettingsError as e:
        return "failed", f"не написала: {e}"
    except da_api.NeedsReconnect:
        return "pending", f"ничего не написала: {da_api.RECONNECT} Потом снова submit_draft — её «го» в силе."
    except da_api.NotConnected as e:
        return "pending", f"ничего не написала: {e}. Её «го» в силе."
    # Отметка «напомнила» — ДО запроса (как drafts.reserve): потерянный ответ или сбой не дадут второго
    # напоминания тому же покупателю. Снимается, только если точно ничего не ушло.
    stamp = now or time.time()
    _quietly(selling.mark_reminded, conn, payload["sale_id"], "sent", stamp)
    status, text = _post_reminder(conn, client, username, payload, now)
    if status in ("pending", "failed"):
        _quietly(selling.unmark_reminded, conn, payload["sale_id"], stamp)
    return ("failed" if status == "already" else status), text


def _post_reminder(conn, client: Any, username: str, payload: dict, now: float | None) -> tuple[str, str]:
    from . import da_api

    try:
        uuid = payload.get("deviation_uuid") or client.deviation_uuid_from_url(
            payload.get("deviation_url") or "", max_pages=GALLERY_LOOKUP_PAGES)
        if not uuid:
            return "failed", "не нашла эту работу в её галерее через API — ничего не написала"
        state, note = comment_once(conn, client, username, uuid, payload["text"], now)
    except da_api.UncertainPublish as e:
        return "unknown", f"{drafts.UNKNOWN_RESULT}: DeviantArt не ответил ({e})"
    except (da_api.RateLimited, da_api.NotConnected) as e:
        return "pending", f"ничего не написала: {e}. Её «го» в силе — можно повторить позже."
    except da_api.DAApiError as e:
        if _uncertain(e):  # не ответило чтение: записи ещё не было
            return "pending", f"ничего не написала: DeviantArt не ответил ({e}). Её «го» в силе."
        return "failed", f"DeviantArt не принял комментарий: {e}"
    if state == "posted":
        return "sent", f"напоминание написано под работой {payload.get('deviation_url') or ''}".rstrip()
    if state == "already":
        # Такой текст уже стоит под работой — покупателю напомнено, отметка «напомнила» остаётся.
        return "already", (f"{note}. Ещё одно с тем же текстом не пишу: чтобы текст менялся, в её шаблоне "
                           "нужен слот {days}.")
    return "pending", f"{note}. Её «го» в силе — можно повторить позже."


def _send_ping(conn, cfg: Config, draft_id: str, now: float | None, lock_timeout_s: float, sleep, rng) -> str:
    try:
        with send_lock(lock_timeout_s):
            payload = drafts.take_for_sending(conn, draft_id, "da_ping", now)
    except SendBusy:
        drafts.note_waiting(conn, draft_id, BUSY_TEXT)
        return BUSY_TEXT
    except drafts.DraftError as e:
        return str(e)
    try:
        status, result = _ping_run(conn, cfg, draft_id, payload, now, lock_timeout_s, sleep, rng or random)
    except Exception as e:  # noqa: BLE001 — исход неизвестен
        status, result = "unknown", f"{drafts.UNKNOWN_RESULT} (ошибка: {type(e).__name__}: {e})"
    if status == "pending":
        drafts.requeue(conn, draft_id, result)
    else:
        drafts.finish(conn, draft_id, status == "sent", result, now, status=status)
    return result


def _ping_run(conn, cfg: Config, draft_id: str, payload: dict, now, lock_timeout_s, sleep, rng) -> tuple[str, str]:
    from . import da_api, pinglist

    dev, comments = payload["deviation_id"], payload.get("comments") or []

    def release_from(i: int) -> None:
        _quietly(pinglist.release, conn, dev, [u for c in comments[i:] for u in c.get("users") or []])

    try:
        client, username = _comment_ctx(cfg)
    except SettingsError as e:
        release_from(0)
        return "failed", f"не написала: {e}"
    except da_api.NeedsReconnect:
        return "pending", f"ничего не написала: {da_api.RECONNECT} Потом снова submit_draft — её «го» в силе."
    except da_api.NotConnected as e:
        return "pending", f"ничего не написала: {e}. Её «го» в силе."
    uuid = payload.get("deviation_uuid")
    lines: list[str] = []
    posted, stop, unknown, by_her, definite, skipped_n = 0, "", False, False, False, 0
    i = 0
    for i, c in enumerate(comments):
        if i:
            if not _stop_requested(conn, draft_id):
                sleep(rng.uniform(*PING_PACE_S))
            _quietly(_heartbeat_draft, conn, draft_id, now)
        if _stop_requested(conn, draft_id):
            stop, by_her = STOPPED_BY_HER, True
            break
        try:
            with send_lock(lock_timeout_s):
                if comment_quota(conn, cfg, now)[1] <= 0:
                    stop = COMMENT_LIMIT
                    break
                # Её одобренный текст не правлю: если кто-то из ников с тех пор вышел из пинглиста
                # (или уже упомянут), этот комментарий целиком пропускаю.
                gone = pinglist.not_wanted(conn, dev, c["users"])
                if gone:
                    _quietly(pinglist.release, conn, dev, c["users"])
                    skipped_n += 1
                    lines.append(f"{i + 1}) пропустила — уже не в пинглисте или уже упомянуты: "
                                 f"{', '.join('@' + u for u in gone)}")
                    continue
                uuid = uuid or client.deviation_uuid_from_url(payload.get("deviation_url") or "",
                                                              max_pages=GALLERY_LOOKUP_PAGES)
                if not uuid:
                    stop, definite = "не нашла работу в её галерее через API", True
                    break
                rid = drafts.reserve(conn, "da_ping", dev, None, now)
                try:
                    state, note = comment_once(conn, client, username, uuid, c["text"], now)
                except Exception as e:
                    if _uncertain(e):
                        _quietly(drafts.settle, conn, rid, "unknown")
                    else:
                        _quietly(drafts.release, conn, rid)
                    raise
                if state != "posted":
                    _quietly(drafts.release, conn, rid)
                if state == "unchecked":
                    stop = note
                    break
                _quietly(pinglist.mark_pinged, conn, dev, c["users"], now)
                if state == "posted":
                    posted += 1
                lines.append(f"{i + 1}) {note}: {', '.join('@' + u for u in c['users'])}")
        except SendBusy:
            stop = "шла другая отправка, не дождалась своей очереди"
            break
        except da_api.UncertainPublish as e:
            _quietly(pinglist.mark_pinged, conn, dev, c["users"], now)  # могло уйти — второй раз не пингуем
            lines.append(f"{i + 1}) {drafts.UNKNOWN_RESULT}: {e}")
            unknown, stop = True, "DeviantArt не ответил — дальше не иду"
            i += 1
            break
        except (da_api.RateLimited, da_api.NotConnected) as e:
            stop = f"DeviantArt: {e}"
            break
        except da_api.DAApiError as e:
            # Потерянный ответ записи — это UncertainPublish (выше); здесь «нет ответа» могло быть только у чтения.
            definite = not _uncertain(e)
            stop = f"DeviantArt не принял комментарий: {e}" if definite else f"DeviantArt не ответил ({e})"
            break
    else:
        i = len(comments)
    done = len(lines) - unknown
    if done == len(comments) and not stop:
        if skipped_n == len(comments):
            return "failed", "\n".join(["Пинг: ничего не написала.", *lines])
        return "sent","\n".join([f"Пинг: написала {posted} из {len(comments)}.", *lines])
    if not lines and not unknown:
        if by_her:
            release_from(0)
            return "cancelled", "Остановила, как она попросила: ничего не написала."
        if definite:
            release_from(0)
            return "failed", f"ничего не написала: {stop}."
        return "pending", f"ничего не написала: {stop.rstrip('.')}. Её «го» в силе — можно снова submit_draft."
    release_from(i)
    text = "\n".join([f"Пинг: готово {done} из {len(comments)}.", *lines, f"Остановилась: {stop.rstrip('.')}."])
    return ("unknown" if unknown else "partial"), text


def _heartbeat_draft(conn, draft_id: str, now: float | None) -> None:
    conn.execute("UPDATE drafts SET sent_ts = ? WHERE id = ? AND status = 'sending'", (now or time.time(), draft_id))
    conn.commit()
