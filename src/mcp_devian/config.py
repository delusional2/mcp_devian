import difflib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("MCP_DEVIAN_DATA", "~/Library/Application Support/mcp_devian")).expanduser()

DEFAULT_LOT_TYPES = ["auction", "ota", "bta", "flatsale", "raffle", "adopt"]


@dataclass
class Filters:
    keywords: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    lot_types: list[str] = field(default_factory=lambda: list(DEFAULT_LOT_TYPES))
    max_usd: float = 0
    max_points: float = 0


@dataclass
class DAGroup:
    url: str
    label: str = ""


@dataclass
class DeviantArt:
    poll_minutes: int = 30
    groups: list[DAGroup] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)


@dataclass
class Toyhouse:
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
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)


SECTIONS = {"filters": Filters, "deviantart": DeviantArt, "toyhouse": Toyhouse, "notify": Notify}


def _pick(cls, raw: dict, section: str, warnings: list[str]):
    fields = cls.__dataclass_fields__
    for key in raw:
        if key not in fields:
            hint = difflib.get_close_matches(key, list(fields), n=1)
            warnings.append(f"[{section}] {key} — неизвестный ключ" + (f", может, {hint[0]}?" if hint else ""))
    return cls(**{k: v for k, v in raw.items() if k in fields})


def config_path() -> Path:
    env = os.environ.get("MCP_DEVIAN_CONFIG")
    if env:
        return Path(env).expanduser()
    return PROJECT_DIR / "config.toml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config(path=None)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    warnings: list[str] = []
    for section in raw:
        if section not in SECTIONS:
            warnings.append(f"[{section}] — неизвестный раздел")

    da_raw = dict(raw.get("deviantart", {}))
    groups = []
    for g in da_raw.pop("groups", []):
        groups.append(DAGroup(url=g, label="") if isinstance(g, str) else _pick(DAGroup, g, "deviantart.groups", warnings))
    deviantart = _pick(DeviantArt, da_raw, "deviantart", warnings)
    deviantart.groups = groups

    return Config(
        filters=_pick(Filters, raw.get("filters", {}), "filters", warnings),
        deviantart=deviantart,
        toyhouse=_pick(Toyhouse, raw.get("toyhouse", {}), "toyhouse", warnings),
        notify=_pick(Notify, raw.get("notify", {}), "notify", warnings),
        path=path,
        warnings=warnings,
    )


def load_dotenv(path: Path | None = None) -> None:
    path = path or PROJECT_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
