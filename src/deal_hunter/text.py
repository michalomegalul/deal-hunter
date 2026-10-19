import unicodedata


def fold(text: str | None) -> str:
    """Lower case without accents: 'Ústí nad Labem' -> 'usti nad labem'."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def kc(n: float | None) -> str:
    """12345.6 -> '12 346 Kč'; None -> '-'."""
    return f"{round(n):,} Kč".replace(",", " ") if n is not None else "-"
