"""Auction lots: what is in a lot (LLM extraction) and what it is worth to a reseller (valuation).

A lot is not a Bazoš ad: it is N units, an end time and a current price that only goes up. So the
question is not "is this cheap?" but "up to what bid does buying, refurbishing and reselling these
units on Bazoš pay?".

1. Extraction: the local LLM (same Ollama client, cache and tolerant parser as `triage.py`) reads the
   lot's description, attachment names and item list, and returns the item groups
   ({model, quantity, condition, missing parts, defects, specs}) plus notes (VAT, pickup, inspection,
   fees). Every group must quote the text it came from, and the quote must really be there; missing
   parts and fees are only kept when the text talks about them (the scam-signal idea from triage).
2. Mapping: each group's model goes through the office and GPU normalizers to a pricing key
   ('latitude 5511', 'optiplex 3050 sff', 'rtx 3070').
3. Valuation (see README "Auctions"): per unit, the Bazoš fair price at the unit's spec, times a
   resale factor, minus refurbishment parts and labour; minus per-lot costs; and a max bid at which
   the expected profit is still the target margin of the resale revenue.
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from deal_hunter import normalize, office, triage
from deal_hunter.pricing import LEVEL_RANK, FairPrice

PROMPT_VERSION = 1  # bump when the prompt or schema changes: lots are extracted again

KINDS = ("laptop", "desktop", "monitor", "printer", "server", "network", "tablet", "phone", "other")
CONDITIONS = ("working", "untested", "partly_broken", "broken", "unknown")
MISSING = ("storage", "charger", "battery", "ram", "os", "other")

SYSTEM_PROMPT = """You read Czech state-property auction lots (nms.gov.cz) for a buyer who refurbishes \
ex-office PCs and laptops and resells them one by one on Bazos.cz. Read the lot and fill in the form. \
Use only what the text says; when it says nothing about a point, answer false / an empty list / null.

- items: one entry per distinct model in the lot (merge identical models, add up their quantities).
  - model: brand and model as written, e.g. "Dell Latitude 5511", "HP ProDesk 400 G4 SFF", \
"Lenovo ThinkPad T480". Keep the generation (G4, Gen 2) and form factor (SFF, Mini, Tiny) when given. \
If the text only says "notebooky" without a model, use "unknown laptop".
  - kind: laptop, desktop, monitor, printer, server, network, tablet, phone or other.
  - quantity: how many units of this model. A lot "19 ks notebooků" with no breakdown is one item \
with quantity 19.
  - condition: "working" (stated functional, "funkční"), "untested" (not stated or "nezaručují \
funkčnost"), "partly_broken" (some units faulty), "broken" (not working, "nefunkční", "na díly").
  - missing: parts the text says are missing or dead: "storage" (bez disku, bez HDD/SSD, disky \
odstraněny), "charger" (bez nabíječky/adaptéru/zdroje, bez příslušenství), "battery" (baterie \
nefunkční / 0 %), "ram", "os" (bez OS, bez licence), "other".
  - ram_gb, storage_gb, cpu: per unit, when stated.
  - defects: short English phrases for stated defects (broken screen, missing keys...).
  - quote: the exact words of the text this item comes from (model and count). No quote, no item.
- vat_deductible: the text says VAT can be deducted ("odpočet DPH") or the price is plus VAT.
- pickup: where the buyer collects the lot, if stated (address or town). null otherwise.
- inspection: when/how the lot can be inspected, if stated, in a few words.
- fees: a buyer's premium or extra fee stated in the text: {"percent": number or null, \
"czk": number or null, "quote": exact words}. Use null when no fee is stated. A deposit (kauce, \
jistota) that is returned is not a fee.
- notes: one or two short English sentences on anything else that matters to a reseller."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "quantity": {"type": "integer"},
                    "condition": {"type": "string", "enum": list(CONDITIONS)},
                    "missing": {"type": "array", "items": {"type": "string", "enum": list(MISSING)}},
                    "ram_gb": {"type": ["integer", "null"]},
                    "storage_gb": {"type": ["integer", "null"]},
                    "cpu": {"type": ["string", "null"]},
                    "defects": {"type": "array", "items": {"type": "string"}},
                    "quote": {"type": "string"},
                },
                "required": ["model", "kind", "quantity", "condition", "missing", "defects", "quote"],
            },
        },
        "vat_deductible": {"type": "boolean"},
        "pickup": {"type": ["string", "null"]},
        "inspection": {"type": ["string", "null"]},
        "fees": {
            "type": ["object", "null"],
            "properties": {
                "percent": {"type": ["number", "null"]},
                "czk": {"type": ["number", "null"]},
                "quote": {"type": "string"},
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["items", "vat_deductible", "pickup", "inspection", "fees", "notes"],
}

MAX_DESCRIPTION = 2500
MAX_LIST = 4000


def source_text(lot: dict) -> str:
    """Everything the LLM is shown about a lot, and what its quotes are checked against."""
    parts = [f"Title: {lot['title']}"]
    if lot.get("description"):
        parts.append("Description:\n" + lot["description"][:MAX_DESCRIPTION])
    if lot.get("inspection"):
        parts.append("Inspection: " + lot["inspection"][:500])
    if lot.get("address"):
        parts.append("Seller's address: " + lot["address"])
    names = [a["name"] for a in lot.get("attachments") or []]
    if names:
        parts.append("Attachments: " + "; ".join(names))
    if lot.get("attachment_text"):
        parts.append("Item list from the attachments:\n" + lot["attachment_text"][:MAX_LIST])
    return "\n\n".join(parts)


def build_prompt(lot: dict) -> str:
    price = lot.get("price") or lot.get("start_price")
    head = f"Auction lot {lot['lot_id']} from {lot.get('institution') or 'a state office'}."
    if price:
        head += f" Current price: {price:,} CZK.".replace(",", " ")
    return head + "\n\n" + source_text(lot)


@dataclass
class Item:
    model: str
    kind: str = "other"
    quantity: int = 1
    condition: str = "unknown"
    missing: list[str] = field(default_factory=list)
    ram_gb: int | None = None
    storage_gb: int | None = None
    cpu: str | None = None
    defects: list[str] = field(default_factory=list)
    quote: str = ""
    key: str | None = None  # pricing key from the normalizers ('latitude 5511'), None = not priced
    category: str | None = None  # office_pc | office_laptop | gpu
    verified: bool = True  # the quote was found in the lot's text


@dataclass
class Extraction:
    items: list[Item] = field(default_factory=list)
    vat_deductible: bool = False
    pickup: str | None = None
    inspection: str | None = None
    fee_percent: float = 0.0
    fee_czk: int = 0
    fee_quote: str | None = None
    notes: str = ""
    stated_units: int | None = None  # "22 ks" in the title/description, to check the item counts

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["items"] = [dict(i.__dict__) for i in self.items]
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Extraction":
        data = dict(data)
        items = [Item(**i) for i in data.pop("items", [])]
        return cls(items=items, **data)


def _ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", _ascii(text)).split())


def map_model(model: str, kind: str = "") -> tuple[str | None, str | None]:
    """(pricing key, category) through the office normalizer, then the GPU one."""
    r = office.normalize(model)
    if r.model:
        return r.model, r.category
    if kind not in ("laptop", "desktop", "monitor", "printer", "server"):
        g = normalize.classify(model, "graficka")
        if g.model:
            return g.model, "gpu"
    return None, None


# What the text has to talk about for a "missing" claim to count (on the ASCII-folded source).
MISSING_WORDS = {
    "storage": re.compile(r"disk|hdd|ssd|ulozist|pamet\w* (medi|disk)|nvme"),
    "charger": re.compile(r"nabij|adapter|zdroj|napaj|prislusenstv|charger"),
    "battery": re.compile(r"bateri|akumul|battery"),
    "ram": re.compile(r"\bram\b|pamet|operacni pamet"),
    "os": re.compile(r"\bos\b|operacni system|windows|licenc|linux"),
    "other": re.compile(r"."),
}
FEE_WORDS = re.compile(r"poplat|proviz|prirazk|premi|aukcni (poplatek|odmen)|odmen|fee")
UNITS_RE = re.compile(r"\b(\d{1,4})\s?(?:ks|kus\w*)\b|\bsoubor (\d{1,4})\b")
VAT_WORDS = re.compile(r"\bdph\b|\bvat\b")
PORTABLE = ("laptop", "tablet", "phone")


def quantity_supported(q: int, ascii_source: str) -> bool:
    """A count above one must be written as a count: '22 ks', 'soubor 22', '18× ...', '(14)'."""
    return bool(
        re.search(
            rf"\b{q}\s?(ks|kus\w*|x\b|×)|soubor {q}\b|\({q}\)|\b{q} (notebook|pocitac|monitor)", ascii_source
        )
    )


def stated_units(text: str) -> int | None:
    """The unit count the title or description states ('Soubor 22 ks', '19ks notebooků')."""
    m = UNITS_RE.search(_ascii(text))
    if not m:
        return None
    n = int(m.group(1) or m.group(2))
    return n if 0 < n < 5000 else None


def _int(value) -> int | None:
    return triage._int(value)


def clean(data: dict, source: str, title: str = "") -> tuple[Extraction, list[str]]:
    """Coerce the model's answer into an Extraction, checking every claim against the text."""
    notes: list[str] = []
    x = Extraction()
    plain_source, ascii_source = _plain(source), _ascii(source)
    raw_items = data.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = [raw_items]
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        model = " ".join(str(raw.get("model") or "").split())[:120]
        if not model:
            continue
        kind = str(raw.get("kind") or "other").lower()
        item = Item(model=model, kind=kind if kind in KINDS else "other")
        item.quantity = max(1, min(5000, _int(raw.get("quantity")) or 1))
        if item.quantity > 1 and not quantity_supported(item.quantity, ascii_source):
            notes.append(f"item {model!r}: quantity {item.quantity} is not written as a count in the text")
            item.quantity = -1  # fixed below, once we know how many items and units the lot states
        condition = str(raw.get("condition") or "unknown").lower().replace(" ", "_")
        item.condition = condition if condition in CONDITIONS else "unknown"
        quote = " ".join(str(raw.get("quote") or "").split())
        item.quote = quote[:200]
        if len(_plain(quote)) < 3 or _plain(quote) not in plain_source:
            item.verified = False
            notes.append(f"item {model!r}: quote {quote[:60]!r} is not in the lot text (unverified)")
        missing = raw.get("missing") or []
        for part in missing if isinstance(missing, list) else [missing]:
            part = str(part).lower()
            if part not in MISSING:
                continue
            if part in ("charger", "battery") and item.kind not in PORTABLE:
                continue  # a desktop's "příslušenství" is a keyboard and a cable, not a charger
            if not MISSING_WORDS[part].search(ascii_source):
                notes.append(f"ignored missing {part!r} for {model!r}: the text says nothing about it")
                continue
            if part not in item.missing:
                item.missing.append(part)
        item.ram_gb = _int(raw.get("ram_gb"))
        item.storage_gb = _int(raw.get("storage_gb"))
        item.cpu = (str(raw["cpu"]).strip() or None) if raw.get("cpu") else None
        item.defects = triage._strings(raw.get("defects"))[:8]
        item.key, item.category = map_model(model, item.kind)
        x.items.append(item)
    x.stated_units = stated_units(title) or stated_units(source[:600])
    for item in x.items:
        if item.quantity == -1:
            alone = len(x.items) == 1 and x.stated_units
            item.quantity = x.stated_units if alone else 1
    x.vat_deductible = triage._bool(data.get("vat_deductible", False))
    if x.vat_deductible and not VAT_WORDS.search(ascii_source):
        notes.append("ignored vat_deductible: the text says nothing about VAT (DPH)")
        x.vat_deductible = False
    x.pickup = (" ".join(str(data.get("pickup") or "").split()) or None) if data.get("pickup") else None
    x.inspection = (
        (" ".join(str(data.get("inspection") or "").split())[:200] or None)
        if data.get("inspection")
        else None
    )
    fees = data.get("fees") if isinstance(data.get("fees"), dict) else None
    if fees:
        quote = " ".join(str(fees.get("quote") or "").split())
        pct = fees.get("percent")
        czk = _int(fees.get("czk"))
        pct = float(pct) if isinstance(pct, int | float) and 0 < pct <= 30 else 0.0
        if (
            (pct or czk)
            and _plain(quote)
            and _plain(quote) in plain_source
            and FEE_WORDS.search(_ascii(quote))
        ):
            x.fee_percent, x.fee_czk, x.fee_quote = pct, czk or 0, quote[:200]
        elif pct or czk:
            notes.append(f"ignored fee {pct or czk}: quote {quote[:60]!r} is not a stated fee in the text")
    x.notes = " ".join(str(data.get("notes") or "").split())[:300]
    total = sum(i.quantity for i in x.items)
    if x.stated_units and x.items and total != x.stated_units:
        notes.append(f"item quantities add up to {total}, the lot says {x.stated_units}")
    return x, notes


@dataclass
class Outcome:
    status: str  # done | failed
    result: Extraction
    raw: str = ""
    notes: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    error: str | None = None


def extract(client, lot: dict, cache_dir: Path | None) -> Outcome:
    """Ask the model what is in one lot. Two attempts at a readable answer, then 'failed'.

    Raises triage.LLMUnavailable when Ollama can't be reached (the lot waits for the next cycle).
    """
    user = build_prompt(lot)
    source = source_text(lot)
    raw, stats, error = "", {}, None
    for attempt in range(2):
        system = SYSTEM_PROMPT if attempt == 0 else SYSTEM_PROMPT + "\nAnswer with the JSON object only."
        raw, stats, cached = triage.chat_cached(client, system, user, cache_dir, RESPONSE_SCHEMA)
        stats = {**stats, "cached": cached}
        try:
            data, notes = triage.parse_answer(raw)
            result, fixes = clean(data, source, lot["title"])
            return Outcome("done", result, raw, notes + fixes, stats)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            error = f"attempt {attempt + 1}: {type(exc).__name__}: {exc}"
    return Outcome("failed", Extraction(), raw, [error or "unreadable answer"], stats, error)


# --- valuation --------------------------------------------------------------------------------------

# Parts to make one unit sellable (Kč). README "Auctions" has the same table; keep them in sync.
REFURB_COSTS = {
    "storage": 500,  # a new 256 GB SATA/NVMe SSD
    "battery": 600,  # an aftermarket laptop battery
    "charger": 250,  # a generic 65 W adapter
    "ram": 300,  # one 8 GB DDR4 module
    "os": 0,  # Windows: reactivates from the BIOS key / refurb licence (see README for the catch)
    "other": 200,
}
SSD_ADDED_GB = 256  # a unit sold without storage gets this SSD, and is priced at this spec
BROKEN_FACTOR = 0.25  # broken units are worth this share of a working one (parts)
PARTLY_BROKEN_FACTOR = 0.75  # "some units faulty": expected value per unit


@dataclass(frozen=True)
class Costs:
    """Valuation settings (env, see config.py). Money in Kč, time in hours unless named."""

    hourly_rate: float = 200.0
    minutes_per_unit: float = 40.0  # clean, test, install, photograph, list, answer, hand over
    resale_factor: float = 0.9  # Bazoš medians are asking prices; buyers haggle
    sell_per_week: float = 5.0  # how many units of one lot Bazoš absorbs in a week
    hold_discount_per_week: float = 0.01  # prices drift down while stock waits to sell
    lot_cost: float = 500.0  # fuel/transport for the pickup, per lot
    lot_hours: float = 2.0  # driving there and back, loading
    target_margin: float = 0.30  # profit as a share of resale revenue, for the max bid
    min_profit: float = 1500.0  # don't bother below this, at the next possible bid


@dataclass
class Line:
    """One item group, valued."""

    model: str
    key: str | None
    quantity: int
    fair: int | None  # Bazoš fair price per unit at the unit's spec (after SSD if added)
    level: str  # fair price confidence: none | low | medium | high
    n: int  # samples behind the fair price
    resale: float  # expected Bazoš sale price per unit after haggling and condition
    parts: float  # refurb parts per unit
    revenue: float  # quantity x resale (0 when not priced)


@dataclass
class Valuation:
    lines: list[Line]
    units: int
    priced_units: int
    revenue: float  # expected resale revenue of everything priced
    parts: float  # refurb parts in total
    hours: float  # labour for the priced units + the pickup
    labour: float  # hours x hourly rate
    fixed: float  # per-lot transport + fixed fees
    fee_percent: float  # buyer's premium on the hammer price
    weeks_to_sell: float
    max_bid: int | None  # the bid at which profit = target margin x revenue
    price: int | None  # what the lot costs now (current price or start price)
    next_bid: int | None  # the lowest bid we could still place
    profit: float | None  # at next_bid
    profit_per_hour: float | None
    confidence: str  # high | medium | low | none (fair prices behind most of the revenue)
    qualifies: bool
    reasons: list[str]

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["lines"] = [dict(line.__dict__) for line in self.lines]
        return out


def unit_parts(item: Item) -> float:
    return sum(REFURB_COSTS.get(part, 0) for part in item.missing)


def unit_features(item: Item) -> dict:
    feats = {}
    if item.ram_gb:
        feats["ram_gb"] = item.ram_gb
    if "storage" in item.missing:
        feats["ssd_gb"] = SSD_ADDED_GB
    elif item.storage_gb:
        feats["ssd_gb"] = item.storage_gb
    return feats


def profit_at(bid: float, v: "Valuation") -> float:
    return v.revenue - v.parts - v.labour - v.fixed - bid * (1 + v.fee_percent / 100)


def value_lot(
    x: Extraction,
    prices: dict[str, FairPrice],
    price: int | None,
    min_increment: int | None,
    bids: int,
    costs: Costs,
) -> Valuation:
    """What the lot is worth to a reseller at its current price (see README "Auctions")."""
    lines, units, priced_units, revenue, parts, unit_hours = [], 0, 0, 0.0, 0.0, 0.0
    weights: dict[str, float] = {}
    for item in x.items:
        units += item.quantity
        fp = prices.get(item.key) if item.key else None
        if fp is None or not item.verified:
            lines.append(Line(item.model, item.key, item.quantity, None, "none", 0, 0.0, 0.0, 0.0))
            continue
        fair = fp.for_listing(unit_features(item))
        factor = {"broken": BROKEN_FACTOR, "partly_broken": PARTLY_BROKEN_FACTOR}.get(item.condition, 1.0)
        resale = fair * costs.resale_factor * factor
        per_unit_parts = unit_parts(item) if item.condition != "broken" else 0.0
        line_revenue = item.quantity * resale
        lines.append(
            Line(
                item.model,
                item.key,
                item.quantity,
                fair,
                fp.level,
                fp.n,
                round(resale),
                per_unit_parts,
                round(line_revenue),
            )
        )
        priced_units += item.quantity
        revenue += line_revenue
        parts += item.quantity * per_unit_parts
        unit_hours += item.quantity * costs.minutes_per_unit / 60
        weights[fp.level] = weights.get(fp.level, 0.0) + line_revenue
    weeks = priced_units / costs.sell_per_week if costs.sell_per_week > 0 else 0.0
    revenue *= max(0.0, 1 - costs.hold_discount_per_week * weeks / 2)  # average unit waits weeks/2
    hours = unit_hours + costs.lot_hours
    labour = hours * costs.hourly_rate
    fixed = costs.lot_cost + x.fee_czk
    if not weights:
        confidence = "none"
    else:
        # the lowest level that carries at least a fifth of the revenue
        total = sum(weights.values())
        confidence = min(
            (lvl for lvl, w in weights.items() if w >= 0.2 * total), key=lambda lvl: LEVEL_RANK[lvl]
        )
    if units and priced_units < 0.7 * units and confidence != "none":
        confidence = "low"  # most of the lot is unpriced or unverified
    if x.stated_units and units and units != x.stated_units and confidence != "none":
        confidence = "low"  # the item list doesn't add up to what the lot says
    v = Valuation(
        lines=lines,
        units=units,
        priced_units=priced_units,
        revenue=round(revenue),
        parts=round(parts),
        hours=round(hours, 1),
        labour=round(labour),
        fixed=round(fixed),
        fee_percent=x.fee_percent,
        weeks_to_sell=round(weeks, 1),
        max_bid=None,
        price=price,
        next_bid=None,
        profit=None,
        profit_per_hour=None,
        confidence=confidence,
        qualifies=False,
        reasons=[],
    )
    if confidence == "none":
        v.reasons.append("no fair price for any item yet")
        return v
    max_bid = (revenue * (1 - costs.target_margin) - parts - labour - fixed) / (1 + x.fee_percent / 100)
    v.max_bid = max(0, int(max_bid // 100 * 100))
    if price is not None:
        v.next_bid = price + (min_increment or 0) if bids else price
        v.profit = round(profit_at(v.next_bid, v))
        v.profit_per_hour = round(v.profit / hours) if hours else None
    v.qualifies, v.reasons = qualifies(v, costs)
    return v


HUGE_MARGIN = 2.0  # a low-confidence lot alerts only when the max bid is >= 2x the next bid ...
HUGE_PROFIT = 3.0  # ... and the profit at the next bid is >= 3x the minimum


def qualifies(v: Valuation, costs: Costs) -> tuple[bool, list[str]]:
    """(worth a push, why not). Low confidence needs a huge margin (README "Auctions")."""
    if v.next_bid is None or v.profit is None or v.max_bid is None:
        return False, ["no price"]
    reasons = []
    if v.max_bid < v.next_bid:
        reasons.append(f"next bid {v.next_bid} is above the max bid {v.max_bid}")
    if v.profit < costs.min_profit:
        reasons.append(f"profit {v.profit:.0f} < {costs.min_profit:.0f}")
    if LEVEL_RANK[v.confidence] < LEVEL_RANK["medium"]:
        huge = v.max_bid >= HUGE_MARGIN * max(v.next_bid, 1) and v.profit >= HUGE_PROFIT * costs.min_profit
        if not huge:
            reasons.append(f"{v.confidence} confidence without a huge margin")
    return not reasons, reasons


# --- presentation -----------------------------------------------------------------------------------


def kc(n) -> str:
    return f"{round(n):,} Kč".replace(",", " ") if n is not None else "-"


def summary(x: Extraction, title: str) -> str:
    """'22× HP ProBook 645 G4' (+ 'and 2 more models') or the lot title when nothing was extracted."""
    if not x.items:
        return title
    top = max(x.items, key=lambda i: i.quantity)
    text = f"{top.quantity}× {top.model}"
    others = len(x.items) - 1
    if others:
        text += f" + {others} more model{'s' if others > 1 else ''}"
    return text


def format_alert(lot: dict, x: Extraction, v: Valuation, kind: str, local_end: datetime) -> tuple[str, str]:
    """(title, message) of an auction push. `kind`: first | day | final."""
    when = {"first": "", "day": "⏰ ends tomorrow · ", "final": "⏰ ends in ~2 h · "}[kind]
    title = f"🏛 Aukce: {summary(x, lot['title'])}"[:100]
    priced = [line for line in v.lines if line.fair]
    resale = ", ".join(f"{line.quantity}× ~{kc(line.resale)}" for line in priced[:3])
    lines = [
        f"{when}ends {local_end:%a %H:%M}, now {kc(v.price)}"
        + (f" ({lot['bids']} bid{'s' if lot['bids'] > 1 else ''})" if lot.get("bids") else " (no bids)"),
        (
            f"est. resale {resale}; max bid ~{kc(v.max_bid)}, profit ~{kc(v.profit)}, "
            f"profit/h ~{kc(v.profit_per_hour)}"
        ),
        f"{v.confidence} confidence; ~{v.weeks_to_sell:g} weeks to sell {v.priced_units} units",
    ]
    extras = []
    if x.vat_deductible:
        extras.append("VAT deductible")
    if x.fee_percent or x.fee_czk:
        extras.append(f"fees {x.fee_percent:g} % + {kc(x.fee_czk)}")
    if x.pickup or lot.get("address"):
        extras.append(f"📍 {x.pickup or lot.get('address')}")
    if extras:
        lines.append(" · ".join(extras))
    return title, "\n".join(lines)
