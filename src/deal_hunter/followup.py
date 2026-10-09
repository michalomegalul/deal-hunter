"""The detailed follow-up after a 👍: everything about the ad or lot in one push, plus a draft message.

A 👍 on a deal push means "interested". The feedback service (feedback.py) then sends one more push
with what you need to decide and act: the photo, price vs fair vs new, location, posted time, views,
seller, the LLM's reading of the ad, the description, and a short polite Czech message to the seller
proposing a price, ready to copy. Buttons: "Open ad" and "Bought ✅" (which starts the flips log).

The offer is computed here, not by the LLM: ~7 % below the asking price (HUNTER_OFFER_DISCOUNT),
rounded to a round number, and never more than 10 % below. The LLM only words the message. Its draft
must not invent facts, so it is checked like the triage's scam quotes:

- it must contain our offer, and every other number in it must appear in the ad (no made-up
  specs, dates or prices);
- it may only talk about picking the item up in person if the ad says so, and about shipping only if
  the ad mentions shipping;
- no links, e-mail addresses or phone numbers, at most 600 characters.

A draft that fails any check is replaced by a fixed template with the same offer, so there is always
a message, and it never says anything the ad doesn't.

Auction lots get their own follow-up: the item list, the valuation breakdown, the max bid, the end
time, inspection and pickup.
"""

import re
import unicodedata
from datetime import datetime

from deal_hunter import triage

DRAFT_VERSION = 1
MAX_DESCRIPTION = 1500
MAX_DRAFT = 600
NTFY_LIMIT = 3900  # bytes; ntfy turns longer messages into an attachment (limit 4096)

SYSTEM_PROMPT = """You write short messages in Czech to sellers on Bazos.cz (Czech classifieds) for a buyer \
who wants to buy the item. Write ONE message:
- polite and friendly, formal Czech ("vykání"), 2 to 4 short sentences, starting with "Dobrý den,";
- say the buyer is interested in the item (name it briefly, as in the ad title) and ask whether it is \
still available;
- propose exactly the offer price you are given, in Kč, as a reasonable offer (never call the asking \
price too high or the item bad);
- mention picking the item up in person ONLY if "pickup_in_person" is yes;
- do NOT state anything else about the item, the price history, shipping, dates or specs; do not \
invent facts; no links, no phone numbers;
- end with "Děkuji" or "S pozdravem" (no name).
Answer with JSON: {"message": "..."}."""

SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
}


def _ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def kc(n) -> str:
    return f"{round(n):,} Kč".replace(",", " ") if n is not None else "-"


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


# --- the Czech draft ------------------------------------------------------------------------------------

PICKUP_AD = re.compile(r"osobni (odber|prevzeti|predani|vyzvednuti)|osobne|vyzvednut")
SHIPPING = re.compile(r"zasl|posl|post|balik|dobirk|doprav|zasilk|odesl|kuryr|balikovn|zasilkovn")
PICKUP_DRAFT = re.compile(r"osobn|vyzvedn|prevzet|predani|prijed|prijel")
FORBIDDEN = re.compile(r"https?://|www\.|@|\+\d{3}")


def pickup_allowed(ad_text: str) -> bool:
    """The ad offers (or insists on) personal pickup."""
    return bool(PICKUP_AD.search(_ascii(ad_text)))


def numbers(text: str) -> set[int]:
    """Every number in a text, with Czech thousands separators ('6 500', '6.500') joined."""
    t = re.sub(r"(?<=\d)[  .](?=\d{3}\b)", "", text)
    return {int(n) for n in re.findall(r"\d+", t)}


def build_prompt(listing: dict, offer: int, pickup: bool) -> str:
    description = (listing.get("description") or "").strip()[:1500] or "(no description)"
    return "\n".join(
        [
            f"Ad title: {listing['title']}",
            f"Asking price: {kc(listing['price_czk'])}",
            f"Offer price to propose: {kc(offer)}",
            f"pickup_in_person: {'yes' if pickup else 'no'}",
            "",
            "The ad (for context only; do not repeat facts from it):",
            description,
        ]
    )


def check_draft(draft: str, listing: dict, offer: int, pickup: bool) -> list[str]:
    """Problems with an LLM draft (empty list = fine). See the module doc."""
    problems = []
    text = " ".join(draft.split())
    ad = f"{listing['title']}\n{listing.get('description') or ''}"
    if not text:
        return ["empty"]
    if len(text) > MAX_DRAFT:
        problems.append(f"too long ({len(text)} chars)")
    if offer not in numbers(text):
        problems.append(f"the offer {offer} is not in it")
    allowed = numbers(ad) | {offer, listing.get("price_czk") or 0}
    invented = sorted(numbers(text) - allowed)
    if invented:
        problems.append(f"numbers not in the ad: {invented[:5]}")
    a = _ascii(text)
    if PICKUP_DRAFT.search(a) and not pickup:
        problems.append("talks about pickup, the ad doesn't offer it")
    if SHIPPING.search(a) and not SHIPPING.search(_ascii(ad)):
        problems.append("talks about shipping, the ad doesn't")
    if FORBIDDEN.search(text):
        problems.append("link, e-mail or phone number")
    return problems


def short_title(title: str, words: int = 6) -> str:
    """The first few words of the ad title, without shouting or emoji."""
    clean = re.sub(r"[^\w\s/+.-]", " ", title)
    parts = clean.split()[:words]
    text = " ".join(parts)
    return text if not text.isupper() else text.title()


def template_draft(listing: dict, offer: int, pickup: bool) -> str:
    """The fallback message: says nothing the ad doesn't."""
    text = (
        f"Dobrý den, mám zájem o {short_title(listing['title'])}. Je ještě k dispozici? "
        f"Mohu nabídnout {kc(offer)}"
    )
    text += ", s osobním převzetím." if pickup else "."
    return text + " Děkuji za odpověď. S pozdravem"


def draft_message(llm, listing: dict, offer: int, cache_dir) -> tuple[str, str, list[str]]:
    """(message, 'llm' | 'template', notes). Never raises for a bad answer; LLM down -> template."""
    ad = f"{listing['title']}\n{listing.get('description') or ''}"
    pickup = pickup_allowed(ad)
    if llm is None:
        return template_draft(listing, offer, pickup), "template", ["no LLM configured"]
    try:
        raw, _, _ = triage.chat_cached(
            llm, SYSTEM_PROMPT, build_prompt(listing, offer, pickup), cache_dir, SCHEMA
        )
        data, notes = triage.parse_answer(raw)
        draft = " ".join(str(data.get("message") or "").split())
    except triage.LLMUnavailable as exc:
        return template_draft(listing, offer, pickup), "template", [f"LLM unreachable: {exc}"[:200]]
    except (ValueError, TypeError, KeyError) as exc:
        return template_draft(listing, offer, pickup), "template", [f"unreadable draft: {exc}"[:200]]
    problems = check_draft(draft, listing, offer, pickup)
    if problems:
        return (
            template_draft(listing, offer, pickup),
            "template",
            notes + [f"draft rejected: {p}" for p in problems],
        )
    return draft, "llm", notes


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
    draft: str,
    offer: int,
    now: datetime,
    test: bool = False,
) -> tuple[str, str]:
    """(title, message) of the follow-up for a Bazoš ad. `fp` may be None (model not priced yet)."""
    from deal_hunter.alerts import new_text

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
    where = " ".join(x for x in (listing.get("location"), listing.get("psc")) if x)
    meta = []
    if where:
        meta.append(f"📍 {where}")
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
    lines.append(triage_summary(t))
    head = "\n".join(lines) + "\n\n📝 "
    tail = f"\n\n✉️ Message (offer {kc(offer)}):\n{draft}"
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
