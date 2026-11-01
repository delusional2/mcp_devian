"""Настройки. Лежат в settings.json в папке данных, заполняет их Claude по её ответам в чате."""

import contextlib
import json
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


@dataclass
class Config:
    filters: Filters = field(default_factory=Filters)
    deviantart: DeviantArt = field(default_factory=DeviantArt)
    toyhouse: Toyhouse = field(default_factory=Toyhouse)
    notify: Notify = field(default_factory=Notify)
    posting: Posting = field(default_factory=Posting)
    configured: bool = False
    configured_at: float = 0
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    unreadable: bool = False


SECTIONS = {"filters": Filters, "deviantart": DeviantArt, "toyhouse": Toyhouse, "notify": Notify,
            "posting": Posting}


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
    return cfg


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


def _number(value, name: str, minimum: float = 0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise SettingsError(f"{name}: нужно число") from e
    if number < minimum:
        raise SettingsError(f"{name}: не меньше {minimum:g}")
    return number


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
