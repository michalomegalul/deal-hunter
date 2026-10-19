"""The detailed follow-up after a 👍: everything about the ad or lot in one push, plus a message to send.

A 👍 on a deal push means "interested". The feedback service (feedback.py) then sends one more push
with what you need to decide and act: the photo, price vs fair vs new, location and distance, posted
time, views, seller, the LLM's reading of the ad, the description, a suggested price if you want to
haggle, and a short Czech message to the seller, ready to copy. Buttons: "Open ad" and "Bought ✅"
(which starts the flips log).

The message is a fixed template, not an LLM draft: it has to sound like a person, and it must never
say anything the ad doesn't. It names the item the way people do ("grafickou kartu ZOTAC RTX 3070")
and only asks whether it is still available:

    Dobrý den, zajímám se o grafickou kartu ZOTAC RTX 3070. Mohl byste mi prosím sdělit, zda je
    stále k dispozici?

No offer in it (the price is talked about once the seller answers); the suggested offer is shown
separately, ~7 % below asking (HUNTER_OFFER_DISCOUNT), rounded, never more than 10 % below.

The item name is the brand and model, cleaned from the title: the words we recognise (the board
partner or maker, the chip / line / model, the capacity) in the seller's own spelling, nothing else,
so "PRODÁM", prices, emoji and sales talk never end up in the message. An ad without a model (only
test pushes get that far) is "... o grafickou kartu z vašeho inzerátu".

Auction lots get their own follow-up: the item list, the valuation breakdown, the max bid, the end
time, inspection and pickup.
"""

import re
from datetime import datetime

from deal_hunter.text import kc

MAX_DESCRIPTION = 1500
NTFY_LIMIT = 3900  # bytes; ntfy turns longer messages into an attachment (limit 4096)


# --- the offer ----------------------------------------------------------------------------------------


def round_step(price: int) -> int:
    if price < 2000:
        return 50
    if price < 20000:
        return 100
    if price < 60000:
        return 500
    return 1000


def offer_price(price: int, discount: float = 0.07) -> int:
    """~`discount` below the asking price, rounded down to a round number, 3-10 % below asking."""
    step = round_step(price)
    offer = int(price * (1 - discount) // step * step)
    if offer > price * 0.97:
        offer -= step
    floor = price * 0.90
    while offer < floor:
        offer += step
    return max(step, min(offer, price - step)) if price > step else price


# --- the message to the seller --------------------------------------------------------------------------

TEMPLATE = "Dobrý den, zajímám se o {noun} {item}. Mohl byste mi prosím sdělit, zda je stále k dispozici?"

# What the item is, in the accusative ("zajímám se o ...").
NOUNS = {
    "gpu": "grafickou kartu",
    "office_pc": "počítač",
    "office_laptop": "notebook",
    "aio": "all-in-one počítač",
    "hdd": "disk",
    "hdd_many": "disky",
    "external": "externí disk",
    "ssd": "SSD",
}
SECTION_CATEGORY = {"graficka": "gpu", "pc": "office_pc", "notebook": "office_laptop", "hdd": "hdd"}

GPU_BRANDS = (
    "asus|msi|gigabyte|zotac|palit|gainward|sapphire|powercolor|xfx|asrock|evga|pny|kfa2|galax|inno3d"
    "|biostar|manli|colorful|sparkle|acer|dell|hp|lenovo|fujitsu"
)
CHIP_MAKERS = "nvidia|amd|intel"
GPU_SPAN = re.compile(
    r"(?<![a-z0-9])(?:(?:rtx|gtx|gt|rx|arc)[\s-]*)?(?P<number>\d{4}|[ab]\d{3})"
    r"(?:\s*(?:ti\s*super|ti|super|xtx|xt|gre|s))?(?![a-z0-9])",
    re.IGNORECASE,
)

OFFICE_BRANDS = "lenovo|dell|hp|fujitsu"
CANONICAL = {
    "thinkpad": "ThinkPad",
    "thinkcentre": "ThinkCentre",
    "latitude": "Latitude",
    "optiplex": "OptiPlex",
    "elitebook": "EliteBook",
    "probook": "ProBook",
    "elitedesk": "EliteDesk",
    "prodesk": "ProDesk",
    "eliteone": "EliteOne",
    "proone": "ProOne",
    "esprimo": "Esprimo",
    "sff": "SFF",
    "x360": "x360",
    "rtx": "RTX",
    "gtx": "GTX",
    "gt": "GT",
    "rx": "RX",
    "arc": "Arc",
    "xt": "XT",
    "xtx": "XTX",
    "gre": "GRE",
}


def _word(title: str, word: str) -> str | None:
    """`word` as the seller spelled it in the title (a whole word, any case), or None."""
    m = re.search(rf"(?<![^\W_]){re.escape(word)}(?![^\W_])", title, re.IGNORECASE)
    return m.group(0) if m else None


def _canonical(token: str) -> str:
    if token in CANONICAL:
        return CANONICAL[token]
    if re.fullmatch(r"[a-z]{1,2}\d+[a-z]*", token):  # t480s, e5470, m720q, g4
        return token[0].upper() + token[1:]
    return token.capitalize() if token.isalpha() else token


def _brand(title: str, brands: str) -> str | None:
    m = re.search(rf"(?<![^\W_])({brands})(?![^\W_])", title, re.IGNORECASE)
    return m.group(0) if m else None


def gpu_name(title: str, key: str) -> str:
    """'Prodám ZOTAC GAMING GeForce RTX 3070 Twin Edge OC' + 'rtx 3070' -> 'ZOTAC RTX 3070'."""
    m = re.fullmatch(r"(.+?)(?: (\d{1,2})gb)?", key)
    name, vram = m.group(1), m.group(2)
    number = name.split()[1] if len(name.split()) > 1 else name
    chip = None
    for span in GPU_SPAN.finditer(title):
        if span.group("number").lower() == number:
            chip = " ".join(span.group(0).split())
            break
    if chip is None:
        chip = " ".join(_canonical(t) for t in name.split())
    parts = [_brand(title, GPU_BRANDS) or _brand(title, CHIP_MAKERS), chip]
    if vram:
        parts.append(f"{vram}GB")
    return " ".join(p for p in parts if p)


def office_name(title: str, key: str) -> str:
    """'Notebook DELL Latitude 5490 i5/8GB' + 'latitude 5490' -> 'DELL Latitude 5490'."""
    words = [_word(title, t) or _canonical(t) for t in key.split()]
    brand = _brand(title, OFFICE_BRANDS)
    return " ".join([brand, *words] if brand else words)


def item_name(listing: dict) -> tuple[str, str]:
    """(noun, item) for the message to the seller."""
    category = listing.get("category") or SECTION_CATEGORY.get(listing.get("section") or "", "gpu")
    key = listing.get("model")
    title = listing.get("title") or ""
    features = listing.get("features") or {}
    if category == "hdd":
        from deal_hunter import storage

        return storage.noun_and_name(title, features)
    if key and category == "gpu":
        return NOUNS["gpu"], gpu_name(title, key)
    if key and category in ("office_pc", "office_laptop"):
        noun = NOUNS["aio"] if key.startswith(("eliteone", "proone")) else NOUNS[category]
        return noun, office_name(title, key)
    return NOUNS.get(category, NOUNS["gpu"]), ""  # no model: "... o grafickou kartu z vašeho inzerátu"


def seller_message(listing: dict) -> str:
    """The message to the seller (see the module doc)."""
    noun, item = item_name(listing)
    return TEMPLATE.format(noun=noun, item=item or "z vašeho inzerátu")


def disk_summary(features: dict | None, t: dict | None, price: int | None, fp) -> str:
    """The disk line of a follow-up (storage.py): capacity, Kč/TB vs new, badges, SMART."""
    from deal_hunter import storage
    from deal_hunter.pricing import RETAIL_SOURCES

    f = features or {}
    new = (
        fp.new_for_listing(f.get("capacity_tb"))
        if fp is not None and fp.new_source in RETAIL_SOURCES
        else None
    )
    return storage.summary_line(f, (t or {}).get("disk") or {}, storage.unit_price(price, f), new)


# --- formatting ---------------------------------------------------------------------------------------


def triage_summary(t: dict | None) -> str:
    """'used · no defects · receipt/warranty · flags: none — verdict' from a stored triage result."""
    if not t:
        return "🤖 not triaged"
    bits = [t.get("condition") or "?"]
    defects = t.get("defects") or []
    bits.append("defects: " + ", ".join(defects[:4]) if defects else "no defects mentioned")
    bits.append("receipt/warranty ✓" if t.get("warranty_or_receipt") else "no receipt/warranty mentioned")
    flags = []
    if t.get("for_parts"):
        flags.append("for parts")
    if t.get("mining_suspected"):
        flags.append("mining?")
    if t.get("is_bundle"):
        flags.append("bundle")
    if t.get("urgent_sale"):
        flags.append("urgent sale")
    flags += [f"⚠ {s}" for s in t.get("scam_signals") or []]
    bits.append("flags: " + (", ".join(flags) if flags else "none"))
    text = "🤖 " + " · ".join(bits)
    if t.get("one_line_verdict"):
        text += f"\n„{t['one_line_verdict']}“"
    return text


def _fit(head: str, description: str, tail: str) -> str:
    """head + description + tail within NTFY_LIMIT bytes, trimming the description."""
    budget = NTFY_LIMIT - len((head + tail).encode()) - 10
    desc = description[:MAX_DESCRIPTION]
    while desc and len(desc.encode()) > budget:
        desc = desc[: max(0, len(desc) - 100)]
    if desc != description:
        desc = desc.rstrip() + " […]"
    return head + desc + tail


def format_listing(
    listing: dict,
    fp,
    fair: int | None,
    t: dict | None,
    message: str,
    offer: int,
    now: datetime,
    test: bool = False,
    place: str | None = None,
) -> tuple[str, str]:
    """(title, message) of the follow-up for a Bazoš ad. `fp` may be None (model not priced yet).

    `message` is the text for the seller (seller_message), `offer` the suggested price when haggling
    (0 = none), `place` the '📍 town · km' bit (alerts.place_line; worked out here when not given).
    """
    from deal_hunter.alerts import new_text, place_line

    price = listing.get("price_czk")
    title = ("TEST · " if test else "") + "👍 " + listing["title"]
    first = f"💰 {kc(price)}"
    if fair and price:
        disc = 1 - price / fair
        first += f" · fair ~{kc(fair)} (" + (f"−{disc:.0%}" if disc >= 0 else f"+{-disc:.0%}") + ")"
        if fp is not None:
            first += f", n={fp.n} {fp.level}"
    new = new_text(fp)
    if new:
        first += f" · {new}"
    lines = [first]
    model = listing.get("model")
    if model:
        lines.append(f"🏷 {model}")
    place = place_line(listing) if place is None else place
    meta = [place] if place else []
    if listing.get("posted_at"):
        meta.append(f"posted {listing['posted_at']:%d.%m.}")
    if listing.get("first_seen"):
        hours = (now - listing["first_seen"]).total_seconds() / 3600
        meta.append(f"seen {hours:.0f} h ago" if hours < 48 else f"seen {hours / 24:.0f} days ago")
    if listing.get("views") is not None:
        meta.append(f"👁 {listing['views']}")
    if listing.get("seller"):
        meta.append(f"seller {listing['seller']}")
    if meta:
        lines.append(" · ".join(meta))
    extra = disk_summary(listing.get("features"), t, price, fp) if listing.get("category") == "hdd" else ""
    if extra:
        lines.append(extra)
    lines.append(triage_summary(t))
    head = "\n".join(lines) + "\n\n📝 "
    haggle = f"\n\n💬 If negotiating: ~{kc(offer)}" if offer else ""
    tail = f"{haggle}\n\n✉️ Message:\n{message}"
    description = " ".join((listing.get("description") or "(no description)").split())
    return title[:200], _fit(head, description, tail)


def format_gone(kind: str, title: str, when: datetime | None, test: bool = False) -> tuple[str, str]:
    what = "ad" if kind == "listing" else "auction lot"
    since = f" (since {when:%d.%m. %H:%M})" if when else ""
    return (
        ("TEST · " if test else "") + f"👍 gone: {title}"[:200],
        f"Sorry, this {what} is no longer available{since}. Nothing to do.",
    )


def format_lot(lot: dict, x, v, local_end: datetime | None, test: bool = False) -> tuple[str, str]:
    """(title, message) of the follow-up for an auction lot (auctions.Extraction / Valuation)."""
    from deal_hunter.auctions import summary

    title = ("TEST · " if test else "") + f"👍 🏛 {summary(x, lot['title'])}"
    bids = lot.get("bids") or 0
    lines = [
        f"⏰ ends {local_end:%a %d.%m. %H:%M}" if local_end else "⏰ end time unknown",
        (
            f"💰 now {kc(v.price)} ({bids} bid{'s' if bids != 1 else ''}), next bid {kc(v.next_bid)} · "
            f"max bid ~{kc(v.max_bid)}"
        ),
        f"profit at next bid ~{kc(v.profit)}, {kc(v.profit_per_hour)}/h · {v.confidence} confidence",
        "",
        "Items:",
    ]
    for item, line in zip(x.items, v.lines):
        missing = ", missing " + "/".join(item.missing) if item.missing else ""
        value = (
            f" → fair {kc(line.fair)} ({line.level}, n={line.n}), resale {kc(line.resale)}"
            if line.fair
            else " → no fair price"
        )
        lines.append(f"• {item.quantity}× {item.model} ({item.condition}{missing}){value}")
    lines += [
        "",
        f"Valuation: revenue {kc(v.revenue)} (~{v.weeks_to_sell:g} weeks to sell {v.priced_units} units)"
        f" − parts {kc(v.parts)} − labour {v.hours:g} h = {kc(v.labour)} − transport/fees {kc(v.fixed)}"
        + (f", +{v.fee_percent:g} % premium" if v.fee_percent else ""),
    ]
    extras = []
    if x.vat_deductible:
        extras.append("VAT deductible")
    inspection = x.inspection or lot.get("inspection")
    if inspection:
        extras.append(f"🔍 inspection: {' '.join(str(inspection).split())[:300]}")
    pickup = x.pickup or lot.get("address")
    if pickup:
        extras.append(f"📍 pickup: {pickup}")
    if x.notes:
        extras.append(f"🤖 {x.notes}")
    if v.reasons:
        extras.append("not qualifying: " + "; ".join(v.reasons))
    message = "\n".join(lines + ([""] + extras if extras else []))
    while len(message.encode()) > NTFY_LIMIT:
        message = message[:-100]
    return title[:200], message
