import re

UNITS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}

_PART = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?", re.I)


def relative_seconds(text: str | None) -> int | None:
    """'1 month, 4 days ago' -> секунды. Для 'N/A' и мусора None."""
    if not text:
        return None
    parts = _PART.findall(text)
    if not parts:
        return 0 if re.search(r"\bjust now\b", text, re.I) else None
    return sum(int(n) * UNITS[unit.lower()] for n, unit in parts)


def relative_to_ts(text: str | None, now: float) -> float | None:
    seconds = relative_seconds(text)
    return None if seconds is None else now - seconds
