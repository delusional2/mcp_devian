"""Настройки. Лежат в settings.json в папке данных, заполняет их Claude по её ответам в чате."""

import contextlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("MCP_DEVIAN_DATA", "~/Library/Application Support/mcp_devian")).expanduser()

ALL_LOT_TYPES = ["auction", "ota", "bta", "flatsale", "raffle", "adopt", "ych", "commission"]
DEFAULT_LOT_TYPES = ["auction", "ota", "bta", "flatsale", "raffle", "adopt"]
# chrome — Toyhouse читается в её Google Chrome по просьбе в чате;
# bot — автоматический парсер в своём профиле Chrome, работает и в фоне.
MODES = ("chrome", "bot")
LEGACY_MODES = {"claude": "chrome"}
MIN_POLL_MINUTES = 15
MAX_PAGES_PER_BOARD = 20
# Потолки её комментариев на DA за сутки: настройкой их можно только снизить.
DA_COMMENTS_MAX = 20
DA_PINGS_MAX = 10
# Покупатели и продвижение (0.5): жёсткие пределы. Руками исправленный файл их не поднимет.
OUTREACH_PER_DAY_MAX = 5          # черновиков личных сообщений «тёплым» людям за сутки
OUTREACH_COOLDOWN_DAYS = (14, 90)  # одному человеку — не чаще
OUTREACH_WINDOW_DAYS = (30, 180)  # кого считаем «недавно интересовался»
EARLY_ACCESS_HOURS_MAX = 48
PAYMENT_WINDOW_HOURS = (12, 168)
X_HASHTAGS_MAX = 4                # больше — X считает это манипуляцией («excessive hashtags»)
PAYMENT_METHODS = ("boosty", "points", "other")
DA_EXTRA_SCOPES = ("user.manage", "message")
BOOSTY_URL = re.compile(r"^https://boosty\.to/[A-Za-z0-9_.-]+/?$")
X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
X_TAG = re.compile(r"^[A-Za-z0-9_]{2,40}$")
RT_METHODS = ("tag", "hashtag", "reply", "quote", "other")
WATERMARK_POSITIONS = ("tiled", "center", "bottom-right", "bottom-left", "top-right", "top-left")
BOARD_SLUG = re.compile(r"^(\d+)\.[a-z0-9][a-z0-9\-]*$", re.I)


@dataclass
class Filters:
    keywords: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: ["ych", "commission", "base", "f2u"])
    lot_types: list[str] = field(default_factory=lambda: list(DEFAULT_LOT_TYPES))
    max_usd: float = 0
    max_points: float = 0


@dataclass
class DAGroup:
    url: str
    label: str = ""
    # False — папка только для статистики рынка, находки из неё не присылаем.
    notify: bool = True


@dataclass
class DeviantArt:
    poll_minutes: int = 30
    groups: list[DAGroup] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    # Её ник: её галерея нужна для прогноза и обзора её продаж.
    my_username: str = ""
    # История рынка (backfill): сколько дней назад, сколько групп и запросов API за один проход.
    backfill_days: int = 75
    backfill_groups: int = 6
    backfill_api_per_run: int = 40
    # Палитра превью: сколько картинок в час можно скачать (0 — не скачивать).
    palette_downloads_per_hour: int = 30
    # Похожие художники: следить ли и кого (ники DA).
    similar_watch: bool = True
    similar_artists: list[str] = field(default_factory=list)


@dataclass
class Toyhouse:
    mode: str = "bot"
    poll_minutes: int = 45
    # Номер доски (13) или «id.slug» («13.adoption-center») — второй вариант даёт проверенный адрес.
    boards: list[int | str] = field(default_factory=lambda: [13, 5866])
    # Сколько страниц доски читаем всегда…
    pages_per_board: int = 2
    # …и до скольких можно дочитать, если на доске много нового с прошлой проверки (те же паузы).
    max_pages_per_board: int = 6
    active_days: int = 5
    lf_keywords: list[str] = field(default_factory=list)
    read_first_posts: int = 5
    headless: bool = False
    # Фоново читать LF-треды Toyhouse (ищет, кому подходят её персы). По умолчанию выключено — включает она.
    background_lf: bool = False


@dataclass
class Notify:
    macos: bool = True
    telegram: bool = False
    # Она продаёт, а не покупает: чужие лоты DA — только данные для цен, без уведомлений «можно купить».
    market_lots: bool = False
    # Недельный разбор продаж: день недели (0 = понедельник … 6 = воскресенье) и час по её времени.
    weekly_report: bool = True
    weekly_report_weekday: int = 6
    weekly_report_hour: int = 19


@dataclass
class Posting:
    # Шаблоны откликов — только её текст, слово в слово. Слоты: {name} {url} {price} {note}.
    reply_templates: dict[str, str] = field(default_factory=dict)
    # Её персонажи на продажу: {"name", "url", "price", "tags": [...]}.
    characters: list[dict] = field(default_factory=list)
    # Наборы групп DA: имя → [{"group", "folder_id", "folder_name"}].
    group_presets: dict[str, list[dict]] = field(default_factory=dict)
    th_replies_per_day: int = 8
    th_min_gap_minutes: int = 3
    th_same_character_per_day: int = 2
    th_thread_cooldown_days: int = 7
    da_submissions_per_group_per_day: int = 3
    # Её ник на Toyhouse: по нему проверяем, что отклик появился в треде.
    th_username: str = ""
    # True — по «го» отклик отправляется сам; False — текст вставляется, «Post» она жмёт сама.
    th_auto_submit: bool = False
    # client_id приложения DeviantArt (публичный, секрета нет).
    da_client_id: str = ""
    # Её работы по умолчанию: не 18+, не ИИ, запрет на обучение ИИ.
    default_is_mature: bool = False
    default_is_ai_generated: bool = False
    default_noai: bool = True
    # Уровень 18+ для DeviantArt, если работа помечена 18+: moderate или strict.
    default_mature_level: str = "moderate"
    # Её папки галереи DeviantArt, куда класть новые работы по умолчанию (например «Adopts — Open»).
    default_gallery_folders: list[str] = field(default_factory=list)
    # Её часовой пояс: расписание публикаций и «лучшее время».
    timezone: str = "Europe/Moscow"
    # Водяной знак превью (prepare_deviantart_publish(watermark=True)): подпись (пусто — её ник DA),
    # PNG-логотип, положение и непрозрачность в процентах.
    watermark_text: str = ""
    watermark_logo: str = ""
    watermark_position: str = "tiled"
    watermark_opacity: float = 35
    # Её комментарии на DA за сутки (закрытие аукционов, напоминания, пинги) и сколько из них пингов.
    # Можно только уменьшить: больше — риск, что DeviantArt сочтёт это спамом.
    da_comments_per_day: int = DA_COMMENTS_MAX
    da_pings_per_day: int = DA_PINGS_MAX
    # Оплата. Boosty — её страница на boosty.to; иностранцы платят картой методом «не для карт РФ» (3-D Secure).
    boosty_url: str = ""
    payment_methods: list[str] = field(default_factory=list)   # boosty | points | other
    payment_window_hours: int = 48                             # сколько ждать оплату (12–168)
    holds_policy: str = ""                                     # её правило про холды, её словами
    payment_plans: bool = False                                # предлагать оплату частями (50/50)
    tos_url: str = ""                                          # ссылка на её TOS
    # X (Twitter): её ник, хэштеги (не больше 4) и RT-аккаунты, которые она сама записала.
    x_handle: str = ""
    x_hashtags: list[str] = field(default_factory=lambda: ["adoptables", "adoptsopen"])
    x_rt_accounts: list[dict] = field(default_factory=list)    # {"handle", "method", "tag_ok"}
    # Тёплые покупатели: черновиков в сутки, пауза на человека, окно «недавно интересовался».
    outreach_per_day: int = OUTREACH_PER_DAY_MAX
    outreach_cooldown_days: int = OUTREACH_COOLDOWN_DAYS[0]
    outreach_window_days: int = OUTREACH_WINDOW_DAYS[1]
    # Ранний доступ для пинглиста: за сколько часов до всех (0 — выключено).
    pinglist_early_access_hours: int = 0
    # Дополнительные права DeviantArt (по её «Разрешить»): user.manage — статусы и журналы, message — входящие.
    da_extra_scopes: list[str] = field(default_factory=list)
    # YCH и коммишены: часов рисования в неделю (0 — загрузку не проверяем), предоплата %, через сколько дней
    # напомнить ей самой (предоплата не пришла, срок близко, остаток после WIP, пора отдать работу).
    commission_weekly_hours: int = 0
    commission_deposit_pct: int = 50
    commission_deposit_days: int = 3
    commission_deadline_warn_days: int = 3
    commission_final_days: int = 5
    commission_deliver_days: int = 2
    # Фон: напоминания по её слотам (только ей, клиентам ничего не уходит; каждое — один раз) и
    # раз в неделю — где она по цели месяца (выключено, пока не попросит).
    commission_reminders: bool = True
    goal_nudges: bool = False


COMMISHES_DEFAULT_CATEGORIES: tuple[str, ...] = ("adopt", "furry", "human")


@dataclass
class Commishes:
    # Её ник на ych.commishes.com: без него помощник туда в фоне не ходит совсем.
    username: str = ""
    # Категории для небольшой выборки рынка (лента категории, 1 запрос на категорию, кэш 30 мин). Адопты — в
    # Adoptables, а YCH — в лентах по виду персонажа (Furry, Human): без них цен YCH почти не будет.
    categories: list[str] = field(default_factory=lambda: list(COMMISHES_DEFAULT_CATEGORIES))
    # Фон: её аукционы (ставки, итоги, продажи) и выборка рынка раз в 6 ч — только если ник указан.
    background: bool = True
    market: bool = True


COMMISHES_CATEGORIES = ("furry", "brony", "human", "adopt", "crafts")
COMMISHES_CATEGORIES_MAX = 3
COMMISHES_USERNAME = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9_.-]{1,40}$")  # не «..»


@dataclass
class Config:
    filters: Filters = field(default_factory=Filters)
    deviantart: DeviantArt = field(default_factory=DeviantArt)
    toyhouse: Toyhouse = field(default_factory=Toyhouse)
    notify: Notify = field(default_factory=Notify)
    posting: Posting = field(default_factory=Posting)
    commishes: Commishes = field(default_factory=Commishes)
    configured: bool = False
    configured_at: float = 0
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    unreadable: bool = False


SECTIONS = {"filters": Filters, "deviantart": DeviantArt, "toyhouse": Toyhouse, "notify": Notify,
            "posting": Posting, "commishes": Commishes}


def settings_path() -> Path:
    env = os.environ.get("MCP_DEVIAN_SETTINGS")
    return Path(env).expanduser() if env else DATA_DIR / "settings.json"


def _pick(cls, raw: dict, section: str, warnings: list[str]):
    fields = cls.__dataclass_fields__
    warnings.extend(f"{section}.{k} — неизвестный ключ" for k in raw if k not in fields)
    return cls(**{k: v for k, v in raw.items() if k in fields})


def _section(raw: dict, name: str, warnings: list[str]) -> dict:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        warnings.append(f"{name} — не раздел настроек, беру значения по умолчанию")
        return {}
    return value


def load_config(path: Path | None = None) -> Config:
    path = path or settings_path()
    if not path.exists():
        return Config(path=path)
    warnings: list[str] = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("ожидался объект")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # Не перезаписываем её настройки умолчаниями: сохраняем копию и не даём сохранять поверх.
        backup = path.with_name(f"{path.stem}.broken.json")
        if not backup.exists():
            with contextlib.suppress(OSError):
                shutil.copy2(path, backup)
        return Config(path=path, unreadable=True,
                      warnings=[f"settings.json повреждён ({e}); копия: {backup.name}. "
                                "Чтобы начать заново, пройди настройку ещё раз (finish_setup=True)."])

    da_raw = dict(_section(raw, "deviantart", warnings))
    groups = [DAGroup(url=g) if isinstance(g, str) else _pick(DAGroup, g, "deviantart.groups", warnings)
              for g in da_raw.pop("groups", None) or [] if isinstance(g, (str, dict))]
    deviantart = _pick(DeviantArt, da_raw, "deviantart", warnings)
    deviantart.groups = groups
    cfg = Config(
        filters=_pick(Filters, _section(raw, "filters", warnings), "filters", warnings),
        deviantart=deviantart,
        toyhouse=_pick(Toyhouse, _section(raw, "toyhouse", warnings), "toyhouse", warnings),
        notify=_pick(Notify, _section(raw, "notify", warnings), "notify", warnings),
        posting=_pick(Posting, _section(raw, "posting", warnings), "posting", warnings),
        commishes=_pick(Commishes, _section(raw, "commishes", warnings), "commishes", warnings),
        configured=bool(raw.get("configured")),
        configured_at=float(raw.get("configured_at") or 0),
        path=path,
        warnings=warnings,
    )
    _check_boards(cfg.toyhouse, warnings)
    cfg.toyhouse.mode = LEGACY_MODES.get(cfg.toyhouse.mode, cfg.toyhouse.mode)
    if cfg.toyhouse.mode not in MODES:
        warnings.append(f"toyhouse.mode = {cfg.toyhouse.mode!r}, беру bot")
        cfg.toyhouse.mode = "bot"
    # Пояс и время отчёта читает каждая фоновая проверка: исправленный руками файл не должен её ронять.
    if not valid_timezone(cfg.posting.timezone):
        warnings.append(f"posting.timezone = {cfg.posting.timezone!r} — не часовой пояс, беру {DEFAULT_TIMEZONE}")
        cfg.posting.timezone = DEFAULT_TIMEZONE
    for key, top in (("weekly_report_weekday", 6), ("weekly_report_hour", 23)):
        value, default = getattr(cfg.notify, key), getattr(Notify, key)
        number = _whole(value, 0, top)
        if number is None:
            warnings.append(f"notify.{key} = {value!r} — нужно целое от 0 до {top}, беру {default}")
            number = default
        setattr(cfg.notify, key, number)
    _check_posting(cfg.posting, warnings)
    _check_deviantart(cfg.deviantart, warnings)
    _bool_or_default(cfg.toyhouse, "toyhouse", "background_lf", warnings)
    _check_commishes(cfg.commishes, warnings)
    return cfg


def _check_commishes(c: "Commishes", warnings: list[str]) -> None:
    """Руками исправленный файл: кривой ник — не ходим на сайт; лишние категории — не больше трёх."""
    c.username = _lenient(parse_commishes_username, c.username, "", "commishes.username", warnings)
    dropped: list[str] = []
    cats = _lenient(lambda v: parse_commishes_categories(v, strict=False, dropped=dropped), c.categories,
                    list(COMMISHES_DEFAULT_CATEGORIES), "commishes.categories", warnings)
    _warn_dropped("commishes.categories", dropped, warnings)
    c.categories = cats
    for key in ("background", "market"):
        _bool_or_default(c, "commishes", key, warnings)


def _check_posting(p: "Posting", warnings: list[str]) -> None:
    """Руками исправленный файл не должен поднять потолок комментариев или сломать превью."""
    for key, top in (("da_comments_per_day", DA_COMMENTS_MAX), ("da_pings_per_day", DA_PINGS_MAX)):
        number = _whole(getattr(p, key), 0, top)
        if number is None:
            warnings.append(f"posting.{key} = {getattr(p, key)!r} — нужно целое от 0 до {top}, беру {top}")
            number = top
        setattr(p, key, number)
    if p.watermark_position not in WATERMARK_POSITIONS:
        warnings.append(f"posting.watermark_position = {p.watermark_position!r}, беру tiled")
        p.watermark_position = "tiled"
    try:
        opacity = float(p.watermark_opacity)
    except (TypeError, ValueError):
        opacity = -1
    if not 5 <= opacity <= 100:
        warnings.append(f"posting.watermark_opacity = {p.watermark_opacity!r} — нужно 5–100, беру 35")
        opacity = 35
    p.watermark_opacity = opacity
    for key in ("watermark_text", "watermark_logo"):
        if not isinstance(getattr(p, key), str):
            warnings.append(f"posting.{key} — не строка, беру пусто")
            setattr(p, key, "")
    _check_sales_promo(p, warnings)


# Числовые пределы новых настроек: (раздел, ключ, мин, макс). Значение вне предела прижимаем к краю,
# не к умолчанию: руками исправленный файл не поднимет потолок (outreach 9 → 5) и не опустит паузу (3 → 14).
RANGES: dict[str, tuple[str, int, int]] = {
    "payment_window_hours": ("posting", *PAYMENT_WINDOW_HOURS),
    "outreach_per_day": ("posting", 0, OUTREACH_PER_DAY_MAX),
    "outreach_cooldown_days": ("posting", *OUTREACH_COOLDOWN_DAYS),
    "outreach_window_days": ("posting", *OUTREACH_WINDOW_DAYS),
    "pinglist_early_access_hours": ("posting", 0, EARLY_ACCESS_HOURS_MAX),
    "commission_weekly_hours": ("posting", 0, 168),
    "commission_deposit_pct": ("posting", 0, 100),
    "commission_deposit_days": ("posting", 1, 60),
    "commission_deadline_warn_days": ("posting", 1, 60),
    "commission_final_days": ("posting", 1, 60),
    "commission_deliver_days": ("posting", 1, 60),
    "backfill_days": ("deviantart", 60, 90),
    "backfill_groups": ("deviantart", 1, 10),
    "backfill_api_per_run": ("deviantart", 1, 60),
    "palette_downloads_per_hour": ("deviantart", 0, 60),
}


def _clamped(value, low: int, high: int) -> int | None:
    """Целое, прижатое к [low, high]; None — не число."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):  # NaN, Infinity, 1e400 — json их пропускает
        return None
    return int(min(max(round(number), low), high))


def _check_ranges(section, name: str, warnings: list[str]) -> None:
    for key, (sec, low, high) in RANGES.items():
        if sec != name:
            continue
        value = getattr(section, key)
        number = _clamped(value, low, high)
        if number is None:
            number = getattr(type(section), key)
            warnings.append(f"{name}.{key} = {value!r} — нужно целое {low}–{high}, беру {number}")
        elif number != value:
            warnings.append(f"{name}.{key} = {value!r} — можно {low}–{high}, беру {number}")
        setattr(section, key, number)


def _bool_or_default(section, name: str, key: str, warnings: list[str]) -> None:
    value = getattr(section, key)
    if not isinstance(value, bool):
        default = getattr(type(section), key)
        warnings.append(f"{name}.{key} = {value!r} — нужно true/false, беру {str(default).lower()}")
        setattr(section, key, default)


def _lenient(parse, value, default, label: str, warnings: list[str]):
    try:
        return parse(value)
    except SettingsError as e:
        warnings.append(f"{label}: {e}; беру {default!r}")
        return default


def _check_sales_promo(p: "Posting", warnings: list[str]) -> None:
    """Оплата, X, тёплые покупатели: всё из файла — через те же проверки, что и из чата, но мягко."""
    _check_ranges(p, "posting", warnings)
    _bool_or_default(p, "posting", "payment_plans", warnings)
    _bool_or_default(p, "posting", "commission_reminders", warnings)
    _bool_or_default(p, "posting", "goal_nudges", warnings)
    p.boosty_url = _lenient(parse_boosty_url, p.boosty_url, "", "posting.boosty_url", warnings)
    p.tos_url = _lenient(parse_url, p.tos_url, "", "posting.tos_url", warnings)
    p.holds_policy = _lenient(parse_policy, p.holds_policy, "", "posting.holds_policy", warnings)
    p.x_handle = _lenient(parse_x_handle, p.x_handle, "", "posting.x_handle", warnings)
    for key, allowed in (("payment_methods", PAYMENT_METHODS), ("da_extra_scopes", DA_EXTRA_SCOPES)):
        dropped: list[str] = []
        setattr(p, key, _lenient(lambda v, a=allowed, k=key, d=dropped: parse_choices(v, a, k, strict=False, dropped=d),
                                 getattr(p, key), [], f"posting.{key}", warnings))
        _warn_dropped(f"posting.{key}", dropped, warnings)
    dropped_tags: list[str] = []
    tags = _lenient(lambda v: parse_hashtags(v, strict=False, dropped=dropped_tags), p.x_hashtags,
                    list(Posting().x_hashtags), "posting.x_hashtags", warnings)
    _warn_dropped("posting.x_hashtags", dropped_tags, warnings)
    p.x_hashtags = tags
    dropped_rt: list[str] = []
    p.x_rt_accounts = _lenient(lambda v: parse_rt_accounts(v, strict=False, dropped=dropped_rt), p.x_rt_accounts, [],
                               "posting.x_rt_accounts", warnings)
    _warn_dropped("posting.x_rt_accounts", dropped_rt, warnings)
    _check_group_presets(p, warnings)


def _warn_dropped(label: str, dropped: list[str], warnings: list[str]) -> None:
    if dropped:
        warnings.append(f"{label}: не взяла {'; '.join(dropped[:5])}" + (f" и ещё {len(dropped) - 5}" if len(dropped) > 5
                                                                         else ""))


def _check_group_presets(p: "Posting", warnings: list[str]) -> None:
    """Наборы групп: у записи может быть limit {"count", "days"} — правило группы. Кривой limit убираем."""
    if not isinstance(p.group_presets, dict):
        warnings.append("posting.group_presets — не набор, беру пусто")
        p.group_presets = {}
        return
    for name, entries in list(p.group_presets.items()):
        if not isinstance(entries, list):
            warnings.append(f"posting.group_presets.{name} — не список, убираю")
            p.group_presets.pop(name)
            continue
        kept = []
        for e in entries:
            if not isinstance(e, dict):
                warnings.append(f"posting.group_presets.{name}: запись {e!r} — не группа, убираю")
                continue
            if "limit" in e:
                limit = valid_group_limit(e["limit"])
                if limit is None:
                    warnings.append(f"posting.group_presets.{name}: limit {e['limit']!r} у {e.get('group', '?')} — "
                                    "нужно {\"count\": 1–50, \"days\": 1–90}, убираю")
                    e = {k: v for k, v in e.items() if k != "limit"}
                else:
                    e = {**e, "limit": limit}
            kept.append(e)
        p.group_presets[name] = kept


def valid_group_limit(limit) -> dict | None:
    """Правило группы «не больше count работ за days дней» или None, если оно записано криво."""
    if not isinstance(limit, dict):
        return None
    count, days = _whole(limit.get("count"), 1, 50), _whole(limit.get("days"), 1, 90)
    if count is None or days is None:
        return None
    return {"count": count, "days": days}


def _check_deviantart(da: "DeviantArt", warnings: list[str]) -> None:
    _check_ranges(da, "deviantart", warnings)
    _bool_or_default(da, "deviantart", "similar_watch", warnings)
    da.similar_artists = _lenient(lambda v: parse_usernames(v, "similar_artists"), da.similar_artists, [],
                                  "deviantart.similar_artists", warnings)


DEFAULT_TIMEZONE = "Europe/Moscow"


def parse_board(value) -> int | str:
    """13, «13» → 13; «13.adoption-center» → как есть (в нижнем регистре). Иначе SettingsError."""
    if isinstance(value, bool):
        raise SettingsError("toyhouse_boards: номер доски или «id.slug»")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        if value < 1:
            raise SettingsError("toyhouse_boards: номер доски — целое больше 0")
        return value
    if isinstance(value, str):
        v = value.strip().rstrip("/").split("/~forums/")[-1]
        if v.isdigit() and int(v) > 0:
            return int(v)
        if BOARD_SLUG.match(v):
            return v.lower()
    raise SettingsError(f"toyhouse_boards: «{value}» — нужен номер доски (13) или «id.slug» (13.adoption-center)")


def unique_boards(boards: list[int | str]) -> list[int | str]:
    """13 и «13.adoption-center» — одна доска: оставляем одну запись (с адресом, если он есть),
    иначе каждая проверка читала бы её дважды и лишний раз стучалась за Cloudflare."""
    out: dict[int, int | str] = {}
    for b in boards:
        key = b if isinstance(b, int) else int(str(b).split(".", 1)[0])
        if key not in out or isinstance(out[key], int):
            out[key] = b
    return list(out.values())


def _check_boards(th: "Toyhouse", warnings: list[str]) -> None:
    boards = []
    for b in th.boards if isinstance(th.boards, list) else []:
        try:
            boards.append(parse_board(b))
        except SettingsError as e:
            warnings.append(str(e))
    boards = unique_boards(boards)
    th.boards = boards if boards or not th.boards else [13, 5866]
    for key, low in (("pages_per_board", 1), ("max_pages_per_board", 1)):
        number = _whole(getattr(th, key), low, MAX_PAGES_PER_BOARD)
        if number is None:
            default = getattr(Toyhouse, key)
            warnings.append(f"toyhouse.{key} = {getattr(th, key)!r} — нужно целое от {low} до "
                            f"{MAX_PAGES_PER_BOARD}, беру {default}")
            number = default
        setattr(th, key, number)
    th.max_pages_per_board = max(th.max_pages_per_board, th.pages_per_board)


def valid_timezone(name) -> bool:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not isinstance(name, str):
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _whole(value, low: int, high: int) -> int | None:
    """Целое в [low, high] из числа или строки «6»; иначе None. True/False — не числа."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def save_config(cfg: Config, path: Path | None = None, force: bool = False) -> Path:
    if cfg.unreadable and not force:
        raise SettingsError("settings.json повреждён: сохранять поверх не буду, пока она не пройдёт настройку заново")
    path = path or cfg.path or settings_path()
    data: dict[str, object] = {name: asdict(getattr(cfg, name)) for name in SECTIONS}
    data["configured"] = cfg.configured
    data["configured_at"] = cfg.configured_at
    path.parent.mkdir(parents=True, exist_ok=True)
    # Уникальный временный файл: параллельные записи не воруют его друг у друга.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)
    cfg.path, cfg.unreadable = path, False
    return path


# Claude Desktop шлёт параллельные вызовы, а MCPServer выполняет их в разных потоках:
# «прочитать → поменять → сохранить» должно идти целиком под одной блокировкой.
_EDIT_LOCK = threading.Lock()


@contextlib.contextmanager
def editing():
    with _EDIT_LOCK:
        yield load_config()


class SettingsError(ValueError):
    pass


def _words(value, name: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SettingsError(f"{name}: нужен список слов")
    seen, out = set(), []
    for v in (v.strip() for v in value):
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def _flag(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "да", "нет"):
        return value.strip().lower() in ("true", "да")
    raise SettingsError(f"{name}: true или false")


def _number(value, name: str, minimum: float = 0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise SettingsError(f"{name}: нужно число") from e
    if number < minimum:
        raise SettingsError(f"{name}: не меньше {minimum:g}")
    return number


def parse_boosty_url(value) -> str:
    """«boosty.to/name», «https://boosty.to/name/» → https://boosty.to/name. Пусто — сбросить."""
    if not isinstance(value, str):
        raise SettingsError("boosty_url: ссылка вида https://boosty.to/её_ник")
    url = value.strip()
    if not url:
        return ""
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if not url.startswith("https://"):
        url = "https://" + url
    url = url.replace("https://www.boosty.to/", "https://boosty.to/").split("?")[0].split("#")[0]
    if not BOOSTY_URL.match(url):
        raise SettingsError("boosty_url: нужна ссылка на её страницу, например https://boosty.to/her_name "
                            "(без постов и параметров)")
    return url.rstrip("/")


def parse_url(value) -> str:
    if not isinstance(value, str):
        raise SettingsError("tos_url: нужна ссылка https://…")
    url = value.strip()
    if url and (not re.match(r"^https?://[^\s/]+\.[^\s]+$", url) or len(url) > 300):
        raise SettingsError("tos_url: нужна ссылка https://… (до 300 символов)")
    return url


def parse_policy(value) -> str:
    if not isinstance(value, str):
        raise SettingsError("holds_policy: её правило текстом")
    text = " ".join(value.split())
    if len(text) > 300:
        raise SettingsError("holds_policy: не длиннее 300 символов")
    return text


def parse_x_handle(value) -> str:
    if not isinstance(value, str):
        raise SettingsError("x_handle: её ник в X без @")
    name = value.strip().rstrip("/")
    name = re.split(r"(?:x|twitter)\.com/", name)[-1].split("/")[0].split("?")[0].lstrip("@")
    if name and not X_HANDLE.match(name):
        raise SettingsError("x_handle: ник X — до 15 латинских букв, цифр и _")
    return name


def _split_words(value, name: str) -> list[str]:
    """Как _words, но строка «a, b» или «#a #b» — несколько слов (для хэштегов и выбора из списка).
    Элемент списка не дробим: ["two words"] — один кривой хэштег, а не два."""
    if isinstance(value, str):
        value = [w for w in re.split(r"[\s,;]+", value) if w]
    return _words(value, name)


def parse_hashtags(value, strict: bool = True, dropped: list[str] | None = None) -> list[str]:
    """Хэштеги без «#», латиница/цифры/_. strict — больше X_HASHTAGS_MAX нельзя; иначе берём первые
    (что выкинули — в dropped)."""
    words = _split_words(value, "x_hashtags")
    tags, seen = [], set()
    for w in words:
        tag = w.lstrip("#").strip()
        if not X_TAG.match(tag):
            if not strict:
                if dropped is not None:
                    dropped.append(f"«{w}» — не хэштег")
                continue
            raise SettingsError(f"x_hashtags: «{w}» — хэштег латиницей, цифрами и _, без пробелов")
        if tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    if len(tags) > X_HASHTAGS_MAX:
        if strict:
            raise SettingsError(f"x_hashtags: не больше {X_HASHTAGS_MAX} — X считает лишние хэштеги манипуляцией")
        if dropped is not None:
            dropped.append(f"{', '.join(tags[X_HASHTAGS_MAX:])} — больше {X_HASHTAGS_MAX} X считает манипуляцией")
        tags = tags[:X_HASHTAGS_MAX]
    return tags


def parse_rt_accounts(value, strict: bool = True, dropped: list[str] | None = None) -> list[dict]:
    """RT-аккаунты, которые она записала сама: {"handle", "method" (их правило её словами), "tag_ok"}.
    tag_ok=True — только если сам аккаунт просит его отмечать; иначе @упоминание не предлагаем."""
    if not isinstance(value, list):
        raise SettingsError("x_rt_accounts: список вида [{\"handle\": \"…\", \"method\": \"…\", \"tag_ok\": false}]")
    out, seen = [], set()
    for entry in value:
        try:
            if isinstance(entry, str):
                entry = {"handle": entry}
            if not isinstance(entry, dict):
                raise SettingsError(f"x_rt_accounts: {entry!r} — нужна запись с handle")
            handle = parse_x_handle(entry.get("handle", ""))
            if not handle:
                raise SettingsError("x_rt_accounts: у записи нет handle")
            method = entry.get("method", "")
            if not isinstance(method, str) or len(method.strip()) > 120:
                raise SettingsError(f"x_rt_accounts: method у @{handle} — их правило текстом, до 120 символов")
            tag_ok = entry.get("tag_ok", False)
            if not isinstance(tag_ok, bool):
                raise SettingsError(f"x_rt_accounts: tag_ok у @{handle} — true или false")
        except SettingsError as e:
            if strict:
                raise
            if dropped is not None:
                dropped.append(str(e).removeprefix("x_rt_accounts: "))
            continue
        if handle.lower() not in seen:
            seen.add(handle.lower())
            out.append({"handle": handle, "method": " ".join(method.split()), "tag_ok": tag_ok})
    return out


def parse_choices(value, allowed: tuple[str, ...], name: str, strict: bool = True,
                  dropped: list[str] | None = None) -> list[str]:
    words: list[str] = []
    for w in (w.lower() for w in _split_words(value, name)):
        if w not in words:
            words.append(w)
    bad = [w for w in words if w not in allowed]
    if bad and strict:
        raise SettingsError(f"{name}: не знаю {', '.join(bad)}; можно {', '.join(allowed)}")
    if bad and dropped is not None:
        dropped.append(f"не знаю {', '.join(bad)} (можно {', '.join(allowed)})")
    return [w for w in words if w in allowed]


def parse_usernames(value, name: str) -> list[str]:
    out: list[str] = []
    for w in _words(value, name):
        nick = w.strip("@").rstrip("/").split("deviantart.com/")[-1].split("/")[0]
        if not nick or not all(c.isalnum() or c in "-_" for c in nick):
            raise SettingsError(f"{name}: «{w}» — ник DeviantArt из букв, цифр, - и _")
        if nick.lower() not in {o.lower() for o in out}:
            out.append(nick)
    return out


def parse_commishes_username(value) -> str:
    """«@name», «https://ych.commishes.com/user/name/» → name. Пусто — сбросить."""
    if not isinstance(value, str):
        raise SettingsError("commishes_username: её ник на ych.commishes.com")
    name = value.strip().rstrip("/")
    if "commishes.com/user/" in name:
        name = name.split("commishes.com/user/", 1)[1].split("?")[0]
    name = name.lstrip("@")
    if name and ("/" in name or not COMMISHES_USERNAME.match(name)):
        raise SettingsError("commishes_username: ник ych.commishes — латинские буквы, цифры, _ . -")
    return name


def parse_commishes_categories(value, strict: bool = True, dropped: list[str] | None = None) -> list[str]:
    cats = parse_choices(value, COMMISHES_CATEGORIES, "commishes_categories", strict=strict, dropped=dropped)
    if len(cats) > COMMISHES_CATEGORIES_MAX:
        if strict:
            raise SettingsError(f"commishes_categories: не больше {COMMISHES_CATEGORIES_MAX} — выборка рынка маленькая "
                                "на совесть: помощник одной художницы, а не сборщик данных")
        if dropped is not None:
            dropped.append(f"{', '.join(cats[COMMISHES_CATEGORIES_MAX:])} — больше {COMMISHES_CATEGORIES_MAX} не беру")
        cats = cats[:COMMISHES_CATEGORIES_MAX]
    return cats


def _ranged(key: str, value) -> int:
    _sec, low, high = RANGES[key]
    number = _whole(value, low, high)
    if number is None:
        why = {"outreach_per_day": " (больше — уже рассылка, DeviantArt и X считают это спамом)",
               "outreach_cooldown_days": " (чаще одному человеку — навязчиво)",
               "pinglist_early_access_hours": " (0 — выключено)"}.get(key, "")
        raise SettingsError(f"{key}: целое от {low} до {high}{why}")
    return number


# Все ключи update_settings с пределами — для get_settings (одна строка на ключ).
SETTING_KEYS: dict[str, str] = {
    "my_da_username": "её ник DA",
    "buy_keywords": "список слов", "exclude": "список слов", "lf_keywords": "список слов",
    "artists": "ники DA", "lot_types": "|".join(ALL_LOT_TYPES),
    "max_usd": "число ≥0", "max_points": "число ≥0",
    "toyhouse_mode": "chrome|bot", "toyhouse_boards": "13 или 13.adoption-center",
    "active_days": "≥1", "pages_per_board": f"1–{MAX_PAGES_PER_BOARD}",
    "max_pages_per_board": f"1–{MAX_PAGES_PER_BOARD}",
    "deviantart_poll_minutes": f"≥{MIN_POLL_MINUTES}", "toyhouse_poll_minutes": f"≥{MIN_POLL_MINUTES}",
    "th_replies_per_day": "≥1", "th_min_gap_minutes": "≥1", "th_same_character_per_day": "≥1",
    "th_thread_cooldown_days": "≥1", "da_submissions_per_group_per_day": "≥1",
    "th_username": "ник Toyhouse", "th_auto_submit": "true|false", "da_client_id": "число",
    "default_is_mature": "true|false", "default_is_ai_generated": "true|false", "default_noai": "true|false",
    "default_gallery_folders": "папки её галереи", "timezone": "Europe/Moscow",
    "watermark_text": "≤40 симв.", "watermark_logo": "путь к PNG",
    "watermark_position": "|".join(WATERMARK_POSITIONS), "watermark_opacity": "5–100",
    "da_comments_per_day": f"0–{DA_COMMENTS_MAX}", "da_pings_per_day": f"0–{DA_PINGS_MAX}",
    "weekly_report": "true|false", "notify_market_lots": "true|false", "notify_macos": "true|false",
    "notify_telegram": "true|false",
    "boosty_url": "https://boosty.to/ник", "payment_methods": "|".join(PAYMENT_METHODS),
    "payment_window_hours": "{}–{}".format(*PAYMENT_WINDOW_HOURS), "holds_policy": "текст ≤300",
    "payment_plans": "true|false", "tos_url": "https://…",
    "x_handle": "ник X", "x_hashtags": f"≤{X_HASHTAGS_MAX} без #",
    "x_rt_accounts": "[{handle, method, tag_ok}]",
    "outreach_per_day": f"0–{OUTREACH_PER_DAY_MAX}", "outreach_cooldown_days": "{}–{}".format(*OUTREACH_COOLDOWN_DAYS),
    "outreach_window_days": "{}–{}".format(*OUTREACH_WINDOW_DAYS),
    "pinglist_early_access_hours": f"0–{EARLY_ACCESS_HOURS_MAX}",
    "da_extra_scopes": "|".join(DA_EXTRA_SCOPES),
    "backfill_days": "60–90", "backfill_groups": "1–10", "backfill_api_per_run": "1–60",
    "palette_downloads_per_hour": "0–60", "similar_watch": "true|false", "similar_artists": "ники DA",
    "toyhouse_background_lf": "true|false",
    "commission_weekly_hours": "0–168 (0 — не считать загрузку)", "commission_deposit_pct": "0–100",
    "commission_deposit_days": "1–60", "commission_deadline_warn_days": "1–60", "commission_final_days": "1–60",
    "commission_deliver_days": "1–60", "commission_reminders": "true|false", "goal_nudges": "true|false",
    "commishes_username": "её ник ych.commishes", "commishes_categories": f"≤{COMMISHES_CATEGORIES_MAX} из "
    + "|".join(COMMISHES_CATEGORIES), "commishes_background": "true|false", "commishes_market": "true|false (фон)",
}


def describe_keys() -> str:
    """«ключ: предел» через «; » — компактно, для get_settings."""
    return "; ".join(f"{k}: {v}" for k, v in SETTING_KEYS.items())


def apply_changes(cfg: Config, changes: dict) -> list[str]:
    """Меняет настройки, проверяя значения. Возвращает список того, что поменялось."""
    done = []
    f, da, th, nt = cfg.filters, cfg.deviantart, cfg.toyhouse, cfg.notify
    for key, value in changes.items():
        if value is None:
            continue
        if key == "buy_keywords":
            f.keywords = _words(value, key)
        elif key == "exclude":
            f.exclude = _words(value, key)
        elif key == "lf_keywords":
            th.lf_keywords = _words(value, key)
        elif key == "artists":
            da.artists = _words(value, key)
        elif key == "my_da_username":
            name = str(value).strip().strip("@").rstrip("/").split("deviantart.com/")[-1].split("/")[0]
            if name and not all(c.isalnum() or c in "-_" for c in name):
                raise SettingsError("my_da_username: ник DeviantArt из букв, цифр, - и _")
            da.my_username = name
        elif key == "lot_types":
            types = [t.lower() for t in _words(value, key)]
            bad = [t for t in types if t not in ALL_LOT_TYPES]
            if bad:
                raise SettingsError(f"lot_types: не знаю {', '.join(bad)}; можно {', '.join(ALL_LOT_TYPES)}")
            f.lot_types = types
        elif key == "max_usd":
            f.max_usd = _number(value, key)
        elif key == "max_points":
            f.max_points = _number(value, key)
        elif key == "toyhouse_mode":
            if value not in MODES:
                raise SettingsError("toyhouse_mode: chrome (её Chrome) или bot (автоматический парсер)")
            th.mode = value
        elif key == "toyhouse_boards":
            if not isinstance(value, list) or not value:
                raise SettingsError("toyhouse_boards: нужен список номеров досок")
            th.boards = unique_boards([parse_board(b) for b in value])
        elif key == "active_days":
            th.active_days = int(_number(value, key, 1))
        elif key == "pages_per_board":
            th.pages_per_board = int(_number(value, key, 1))
            if th.pages_per_board > MAX_PAGES_PER_BOARD:
                raise SettingsError(f"pages_per_board: не больше {MAX_PAGES_PER_BOARD}")
            th.max_pages_per_board = max(th.max_pages_per_board, th.pages_per_board)
        elif key == "max_pages_per_board":
            pages = _whole(value, 1, MAX_PAGES_PER_BOARD)
            if pages is None:
                raise SettingsError(f"max_pages_per_board: целое от 1 до {MAX_PAGES_PER_BOARD}")
            if pages < th.pages_per_board:
                raise SettingsError(f"max_pages_per_board: не меньше pages_per_board ({th.pages_per_board})")
            th.max_pages_per_board = pages
        elif key == "deviantart_poll_minutes":
            da.poll_minutes = int(_number(value, key, MIN_POLL_MINUTES))
        elif key == "toyhouse_poll_minutes":
            th.poll_minutes = int(_number(value, key, MIN_POLL_MINUTES))
        elif key in ("th_replies_per_day", "th_min_gap_minutes", "th_same_character_per_day",
                     "th_thread_cooldown_days", "da_submissions_per_group_per_day"):
            setattr(cfg.posting, key, int(_number(value, key, 1)))
        elif key == "th_username":
            name = str(value).strip().strip("@").rstrip("/").split("toyhou.se/")[-1].split("/")[0]
            if name and not all(c.isalnum() or c in "-_" for c in name):
                raise SettingsError("th_username: ник Toyhouse из букв, цифр, - и _")
            cfg.posting.th_username = name
        elif key == "th_auto_submit":
            cfg.posting.th_auto_submit = bool(value)
        elif key == "da_client_id":
            client_id = str(value).strip()
            if client_id and not client_id.isdigit():
                raise SettingsError("da_client_id: это число из настроек приложения DeviantArt")
            cfg.posting.da_client_id = client_id
        elif key in ("default_is_mature", "default_is_ai_generated", "default_noai"):
            setattr(cfg.posting, key, bool(value))
        elif key == "default_gallery_folders":
            cfg.posting.default_gallery_folders = _words(value, key)
        elif key == "timezone":
            if not valid_timezone(str(value)):
                raise SettingsError("timezone: например Europe/Moscow")
            cfg.posting.timezone = str(value)
        elif key == "watermark_text":
            # Латиница: шрифт превью кириллицу может не нарисовать (см. media.prepare_preview).
            text = " ".join(str(value).split())
            if len(text) > 40:
                raise SettingsError("watermark_text: не длиннее 40 символов")
            cfg.posting.watermark_text = text
        elif key == "watermark_logo":
            logo = str(value).strip()
            if logo and not Path(logo).expanduser().suffix.lower() == ".png":
                raise SettingsError("watermark_logo: путь к PNG-файлу с логотипом")
            cfg.posting.watermark_logo = logo
        elif key == "watermark_position":
            pos = str(value).strip().lower()
            if pos not in WATERMARK_POSITIONS:
                raise SettingsError(f"watermark_position: {', '.join(WATERMARK_POSITIONS)}")
            cfg.posting.watermark_position = pos
        elif key == "watermark_opacity":
            opacity = _number(value, key, 5)
            if opacity > 100:
                raise SettingsError("watermark_opacity: проценты, от 5 до 100")
            cfg.posting.watermark_opacity = opacity
        elif key in ("da_comments_per_day", "da_pings_per_day"):
            top = DA_COMMENTS_MAX if key == "da_comments_per_day" else DA_PINGS_MAX
            number = _whole(value, 0, top)
            if number is None:
                raise SettingsError(f"{key}: целое от 0 до {top} (больше нельзя — DeviantArt может счесть это спамом)")
            setattr(cfg.posting, key, number)
        elif key == "boosty_url":
            cfg.posting.boosty_url = parse_boosty_url(value)
            if cfg.posting.boosty_url and "boosty" not in cfg.posting.payment_methods:
                cfg.posting.payment_methods = ["boosty", *cfg.posting.payment_methods]
        elif key == "payment_methods":
            cfg.posting.payment_methods = parse_choices(value, PAYMENT_METHODS, key)
        elif key == "tos_url":
            cfg.posting.tos_url = parse_url(value)
        elif key == "holds_policy":
            cfg.posting.holds_policy = parse_policy(value)
        elif key in ("payment_plans", "commission_reminders", "goal_nudges"):
            setattr(cfg.posting, key, _flag(value, key))
        elif key == "x_handle":
            cfg.posting.x_handle = parse_x_handle(value)
        elif key == "x_hashtags":
            cfg.posting.x_hashtags = parse_hashtags(value)
        elif key == "x_rt_accounts":
            cfg.posting.x_rt_accounts = parse_rt_accounts(value)
        elif key == "da_extra_scopes":
            cfg.posting.da_extra_scopes = parse_choices(value, DA_EXTRA_SCOPES, key)
        elif key in RANGES:
            section = cfg.posting if RANGES[key][0] == "posting" else da
            setattr(section, key, _ranged(key, value))
        elif key == "similar_watch":
            da.similar_watch = _flag(value, key)
        elif key == "similar_artists":
            da.similar_artists = parse_usernames(value, key)
        elif key == "toyhouse_background_lf":
            th.background_lf = _flag(value, key)
        elif key == "commishes_username":
            cfg.commishes.username = parse_commishes_username(value)
        elif key == "commishes_categories":
            cfg.commishes.categories = parse_commishes_categories(value)
        elif key in ("commishes_background", "commishes_market"):
            setattr(cfg.commishes, key.removeprefix("commishes_"), _flag(value, key))
        elif key == "weekly_report":
            nt.weekly_report = bool(value)
        elif key == "notify_market_lots":
            nt.market_lots = bool(value)
        elif key == "notify_macos":
            nt.macos = bool(value)
        elif key == "notify_telegram":
            nt.telegram = bool(value)
        else:
            raise SettingsError(f"нет такой настройки: {key}")
        done.append(key)
    return done


def load_dotenv(path: Path | None = None) -> None:
    for candidate in [path] if path else [DATA_DIR / ".env", PROJECT_DIR / ".env"]:
        if not candidate or not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
