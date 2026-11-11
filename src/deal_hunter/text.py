import unicodedata
from datetime import datetime


def fold(text: str | None) -> str:
    """Lower case without accents: 'Ústí nad Labem' -> 'usti nad labem'."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def thousands(n: float) -> str:
    """12345.6 -> '12 346' (rounded, space-grouped, no unit)."""
    return f"{round(n):,}".replace(",", " ")


def kc(n: float | None) -> str:
    """12345.6 -> '12 346 Kč'; None -> '-'."""
    return f"{thousands(n)} Kč" if n is not None else "-"


def ago(dt: datetime | None, now: datetime, suffix: str = " ago") -> str:
    """How long ago `dt` was, relative to `now`: '35 min ago', '2 h 15 min ago', '3 days ago', or
    'never' when `dt` is None. The single implementation shared by web.py (`/status`'s timestamps)
    and health.py (the watchdog's messages); `suffix` lets a caller drop or change the trailing
    word, e.g. suffix="" for "worker step 'x' failing for 35 min" instead of "... for 35 min ago"."""
    if dt is None:
        return "never"
    minutes = max(0, int((now - dt).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min{suffix}"
    if minutes < 48 * 60:
        return f"{minutes // 60} h {minutes % 60} min{suffix}"
    return f"{minutes // 1440} days{suffix}"
