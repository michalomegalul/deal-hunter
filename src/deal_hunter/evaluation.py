"""Does the alert pipeline work? Flip profit and the `hunt eval` numbers (Phase 6).

Pure functions over the rows `repository.list_flips` and `repository.eval_deals` return, so they are
unit-testable without a database. `eval_tables` builds plain tables that `hunt eval` prints as text
and the `/flips` page renders as HTML.

"Was it a good deal" has no ground truth, so the proxy is how fast the ad disappeared after we first
saw it: a really good deal gets bought within a day or two. If ads we scored below the threshold
(`low_score`) vanish as fast as the ones we alerted on, the threshold is too strict. The caveat:
`gone_at` is when a recheck noticed the ad was gone, a few hours after it was, and an ad can vanish
because the seller gave up.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

STATES = ("alerted", "would_alert", "queued", "capped", "low_score", "dropped")
COMPARED_STATES = ("alerted", "would_alert", "low_score")
BANDS = ((0.0, 0.12, "<0.12"), (0.12, 0.2, "0.12-0.2"), (0.2, 0.3, "0.2-0.3"), (0.3, None, ">=0.3"))
UNSCORED = "unscored"


@dataclass
class Table:
    """A titled grid of already formatted cells."""

    title: str
    headers: list[str]
    rows: list[list[str]]


# --- flips ---------------------------------------------------------------------------------------------


def flip_profit(flip: dict) -> int | None:
    """sold - bought - fees; None until the flip is sold (or if a price is unknown)."""
    if flip["sold_at"] is None or flip["sold_price"] is None or flip["buy_price"] is None:
        return None
    return flip["sold_price"] - flip["buy_price"] - (flip["fees"] or 0)


def days_held(flip: dict, now: datetime) -> int:
    """Whole days from purchase to the sale (or to `now` while it is still held)."""
    end = flip["sold_at"] if flip["sold_at"] is not None else now
    return max((end - flip["bought_at"]).days, 0)


def flip_totals(flips: list[dict]) -> dict:
    """Open/sold counts, invested (open flips: buy + fees so far), realised profit and cost, ROI = profit / cost."""
    invested = sum((f["buy_price"] or 0) + (f["fees"] or 0) for f in flips if f["sold_at"] is None)
    realised = [f for f in flips if flip_profit(f) is not None]
    profit = sum(flip_profit(f) for f in realised)
    cost = sum(f["buy_price"] + (f["fees"] or 0) for f in realised)
    return {
        "open": sum(f["sold_at"] is None for f in flips),
        "sold": sum(f["sold_at"] is not None for f in flips),
        "invested": invested,
        "profit": profit,
        "cost": cost,
        "roi": profit / cost if cost > 0 else None,
    }


# --- alerts --------------------------------------------------------------------------------------------


def band_of(score: float | None) -> str:
    """The score band label of a deal; 'unscored' before triage gave it a score."""
    if score is None:
        return UNSCORED
    for low, high, label in BANDS:
        if score >= low and (high is None or score < high):
            return label
    return BANDS[0][2]  # a negative score: below everything


def gone_within(deal: dict, hours: int, now: datetime) -> bool | None:
    """Did the ad disappear within `hours` of first_seen? None if we can't tell yet (too young, still up)."""
    window = timedelta(hours=hours)
    if deal["gone_at"] is not None and deal["gone_at"] - deal["first_seen"] <= window:
        return True
    if now - deal["first_seen"] >= window:
        return False
    return None


def _share(deals: list[dict], hours: int, now: datetime) -> str:
    """'k/n pct' of the deals old enough to judge that were gone within `hours`; '-' if none are."""
    seen = [g for g in (gone_within(d, hours, now) for d in deals) if g is not None]
    return f"{sum(seen)}/{len(seen)} {sum(seen) / len(seen):.0%}" if seen else "-"


def _rate(ups: int, downs: int) -> str:
    return f"{ups / (ups + downs):.0%}" if ups + downs else "-"


def _groups(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """('all', every row) followed by one group per category (a missing category is 'unknown')."""
    categories = sorted({r["category"] or "unknown" for r in rows})
    return [("all", rows)] + [(c, [r for r in rows if (r["category"] or "unknown") == c]) for c in categories]


def eval_tables(deals: list[dict], flips: list[dict], now: datetime) -> list[Table]:
    """The tables of the report: states, score bands, vanishing speed by state, flips per category."""
    groups = _groups(deals)

    states_rows = []
    for name, group in groups:
        counts = Counter(d["state"] for d in group)
        other = sum(n for state, n in counts.items() if state not in STATES)
        states_rows.append([name, str(len(group)), *(str(counts[s]) for s in STATES), str(other)])
    tables = [Table("Deals by state", ["category", "deals", *STATES, "other"], states_rows)]

    band_rows = []
    for name, group in groups:
        for label in [b[2] for b in BANDS] + [UNSCORED]:
            in_band = [d for d in group if band_of(d["score"]) == label]
            if not in_band:
                continue
            alerted = [d for d in in_band if d["state"] == "alerted"]
            ups = sum(d["has_up"] for d in alerted)
            downs = sum(d["has_down"] for d in alerted)
            band_rows.append(
                [
                    name,
                    label,
                    str(len(in_band)),
                    str(len(alerted)),
                    str(ups),
                    str(downs),
                    _rate(ups, downs),
                    _share(in_band, 24, now),
                    _share(in_band, 72, now),
                ]
            )
    tables.append(
        Table(
            "Score bands (up/down/rate: alerted ads only; gone: all ads in the band)",
            ["category", "score", "deals", "alerted", "up", "down", "up rate", "gone 24h", "gone 72h"],
            band_rows,
        )
    )

    speed_rows = []
    for name, group in groups:
        for state in COMPARED_STATES:
            in_state = [d for d in group if d["state"] == state]
            if in_state:
                speed_rows.append(
                    [name, state, str(len(in_state)), _share(in_state, 24, now), _share(in_state, 72, now)]
                )
    tables.append(
        Table(
            "Gone within 24 h / 72 h of first seen (ads too young to judge are left out)",
            ["category", "state", "deals", "gone 24h", "gone 72h"],
            speed_rows,
        )
    )

    flip_rows = []
    for name, group in _groups(flips):
        totals = flip_totals(group)
        roi = f"{totals['roi']:.0%}" if totals["roi"] is not None else "-"
        flip_rows.append([name, str(len(group)), str(totals["sold"]), str(totals["profit"]), roi])
    tables.append(
        Table("Flips bought in the period", ["category", "bought", "sold", "profit Kč", "ROI"], flip_rows)
    )
    return tables


def format_text(tables: list[Table]) -> str:
    """The tables as plain text, columns padded to the widest cell."""
    out = []
    for t in tables:
        widths = [max(len(c) for c in col) for col in zip(t.headers, *t.rows)]
        out.append(t.title)
        for row in [t.headers, *t.rows]:
            out.append("  " + "  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
        out.append("")
    return "\n".join(out).rstrip() + "\n"
