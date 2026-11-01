"""Настройки. Лежат в settings.json в папке данных, заполняет их Claude по её ответам в чате."""

import contextlib
import json
import os
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
    boards: list[int] = field(default_factory=lambda: [13, 5866])
    pages_per_board: int = 2
    active_days: int = 5
    lf_keywords: list[str] = field(default_factory=list)
    read_first_posts: int = 5
    headless: bool = False


@dataclass
class Notify:
    macos: bool = True
    telegram: bool = False


@dataclass
class Config:
    filters: Filters = field(default_factory=Filters)
    deviantart: DeviantArt = field(default_factory=DeviantArt)
    toyhouse: Toyhouse = field(default_factory=Toyhouse)
    notify: Notify = field(default_factory=Notify)
    configured: bool = False
    configured_at: float = 0
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    unreadable: bool = False


SECTIONS = {"filters": Filters, "deviantart": DeviantArt, "toyhouse": Toyhouse, "notify": Notify}


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
        configured=bool(raw.get("configured")),
        configured_at=float(raw.get("configured_at") or 0),
        path=path,
        warnings=warnings,
    )
    cfg.toyhouse.mode = LEGACY_MODES.get(cfg.toyhouse.mode, cfg.toyhouse.mode)
    if cfg.toyhouse.mode not in MODES:
        warnings.append(f"toyhouse.mode = {cfg.toyhouse.mode!r}, беру bot")
        cfg.toyhouse.mode = "bot"
    return cfg


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
            th.boards = [int(_number(b, key, 1)) for b in value]
        elif key == "active_days":
            th.active_days = int(_number(value, key, 1))
        elif key == "pages_per_board":
            th.pages_per_board = int(_number(value, key, 1))
        elif key == "deviantart_poll_minutes":
            da.poll_minutes = int(_number(value, key, MIN_POLL_MINUTES))
        elif key == "toyhouse_poll_minutes":
            th.poll_minutes = int(_number(value, key, MIN_POLL_MINUTES))
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
