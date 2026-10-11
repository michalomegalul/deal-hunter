"""Aukro.cz: the `searchItemsCommon` JSON API its own category pages call (no HTML scraping).

robots.txt (checked 2026-10-11, https://aukro.cz/robots.txt) disallows a short list of account/
checkout/admin paths for every bot; category and item pages, and this backend API, are not among
them, so our honest custom User-Agent (not a named crawler) is free to read them. It also disallows
"ClaudeBot" by name entirely -- that is Anthropic's own crawler product, not this client, which
identifies as deal-hunter the same way the Bazoš scraper does, so it isn't covered by that rule.

Aukro's own "-aukce" and "-bazar" URL suffixes turned out to be cosmetic landing pages: the API
returns the same mixed bag of auctions and fixed-price offers either way (confirmed against the
live API while building this), telling the two apart per item instead:

- `auction: false` -- a plain fixed-price listing; `price` is the price.
- `auction: true, buyNowActive: true` -- biddable, but also buyable now; `buyNowPrice` is the
  reliable number, `price` is only the current bid.
- `auction: true, buyNowActive: false` -- bidding only; `price` is just the current bid (often a
  token "1 Kč" opener) and tells us nothing about value, so it is kept as price_kind='auction',
  which price_samples (pricing.py) already excludes same as 'negotiable'/'in_text'/etc.

So we query one slug per category (no need to fetch "-aukce" and "-bazar" separately).
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from deal_hunter.bazos import Listing
from deal_hunter.sources import make_id

log = logging.getLogger(__name__)

BACKEND_URL = "https://aukro.cz/backend-web/api/offers/searchItemsCommon"
PAGE_SIZE = 60


@dataclass(frozen=True)
class Category:
    seo_url: str  # Aukro's own category slug
    section: str  # one of sources.CANONICAL_SECTIONS


SECTIONS = {
    "graficka": Category("pc-graficke-karty", "graficka"),
    "pc": Category("stolni-pocitace", "pc"),
    "notebook": Category("notebooky-netbooky", "notebook"),
    "hdd": Category("pc-interni-disky", "hdd"),
}


def request_body(seo_url: str) -> dict:
    """The JSON body the category page itself posts (minus the A/B test noise, which is optional)."""
    return {
        "splitGroups": {"listing": None},
        "fallbackItemsCount": 12,
        "categorySeoUrl": seo_url,
        "subbrandExclusive": False,
    }


def category_url(seo_url: str, page: int = 0, size: int = PAGE_SIZE) -> str:
    return f"{BACKEND_URL}?page={page}&size={size}"


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _price(item: dict) -> tuple[int | None, str]:
    """(price_czk, price_kind) -- see the module docstring for the auction/buyNowActive logic."""
    price = (item.get("price") or {}).get("amount")
    if not item.get("auction"):
        return (int(price) if price is not None else None), "fixed"
    if item.get("buyNowActive"):
        buy_now = (item.get("buyNowPrice") or {}).get("amount")
        if buy_now:
            return int(buy_now), "fixed"
    return (int(price) if price is not None else None), "auction"


def parse_item(item: dict, section: str) -> Listing | None:
    item_id = item.get("itemId")
    seo_url = item.get("seoUrl")
    if item_id is None or not seo_url:
        return None
    price_czk, price_kind = _price(item)
    path = item.get("categoryPath") or []
    posted = _dt(item.get("startingTime"))
    return Listing(
        id=make_id("aukro", item_id),
        source="aukro",
        external_id=str(item_id),
        url=f"https://aukro.cz/{seo_url}-{item_id}",
        title=item.get("itemName") or "",
        price_czk=price_czk,
        price_kind=price_kind,
        location=item.get("location"),
        psc=item.get("postcode"),
        posted_at=posted.date() if posted else None,
        seller=item.get("sellerLogin"),
        images=[item["titleImageUrl"]] if item.get("titleImageUrl") else [],
        raw_category=path[-1]["seoUrl"] if path else None,
        section=section,
        auction_end_at=_dt(item.get("endingTime")) if item.get("auction") else None,
    )


def parse_response(data: dict, section: str) -> list[Listing]:
    """Every item of one `searchItemsCommon` page, in the order the API returned them."""
    out = []
    for item in data.get("content") or []:
        listing = parse_item(item, section)
        if listing is not None:
            out.append(listing)
    return out


def total_pages(data: dict) -> int:
    page = data.get("page") or {}
    return int(page.get("totalPages") or 1)
