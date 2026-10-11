"""Sbazar.cz: blocked.

robots.txt (checked 2026-10-11, https://www.sbazar.cz/robots.txt):

    User-agent: *
    Disallow: /

Every path on the site is off-limits to a generic bot. The only exceptions are a short, explicit
list of named crawlers (Googlebot, GoogleOther, SeznamBot, Bingbot, Slurp, DuckDuckBot,
Baiduspider, YandexBot, Applebot, ia_archiver, msnbot, Naverbot, and a few ad/social bots) --
deal-hunter, which identifies with its own honest User-Agent the same way the Bazoš scraper does,
is not one of them, so it falls under `User-agent: *` and is disallowed everywhere, full stop. We
never fetched anything from sbazar.cz beyond that one robots.txt request.

So `fetch_category` below refuses to run -- this isn't just documented, it's enforced, in case a
future config enables "sbazar" without rereading this. The parser exists and is unit-tested so the
groundwork is ready if robots.txt ever changes: `parse_response` is written against a *synthetic*
fixture (tests/fixtures/sbazar_category_SYNTHETIC.json) built from Sbazar's publicly documented
listing JSON shape, not a real captured response -- it has never been verified against live data
and may need adjusting if the real shape differs.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from deal_hunter.bazos import Listing
from deal_hunter.sources import make_id

log = logging.getLogger(__name__)

BLOCKED_REASON = (
    "sbazar.cz disallows generic crawlers via robots.txt (User-agent: * / Disallow: /, checked "
    "2026-10-11); the sbazar source is disabled and will stay disabled until that changes"
)


@dataclass(frozen=True)
class Category:
    seo_name: str  # Sbazar's own category name, best-effort (never confirmed live)
    section: str  # one of sources.CANONICAL_SECTIONS


SECTIONS = {
    "graficka": Category("pc-graficke-karty", "graficka"),
    "pc": Category("stolni-pocitace", "pc"),
    "notebook": Category("notebooky", "notebook"),
    "hdd": Category("pevne-disky", "hdd"),
}


def parse_item(item: dict, section: str) -> Listing | None:
    native_id = item.get("id")
    if native_id is None:
        return None
    price = (item.get("price") or {}).get("value")
    locality = item.get("locality") or {}
    images = [img["url"] for img in item.get("images") or [] if img.get("url")]
    posted = None
    raw_date = item.get("dateCreated")
    if raw_date:
        try:
            posted = datetime.fromisoformat(raw_date).date()
        except ValueError:
            posted = None
    return Listing(
        id=make_id("sbazar", native_id),
        source="sbazar",
        external_id=str(native_id),
        url=item.get("url") or f"https://www.sbazar.cz/inzerat/{native_id}",
        title=item.get("name") or "",
        price_czk=int(price) if price else None,
        price_kind="fixed" if price else "unknown",
        location=locality.get("city"),
        psc=locality.get("zip"),
        posted_at=posted,
        seller=item.get("userId") and str(item["userId"]),
        images=images,
        raw_category=(item.get("category") or {}).get("seoName"),
        section=section,
    )


def parse_response(data: dict, section: str) -> list[Listing]:
    """Every item of one listing page. Untested against real data -- see the module docstring."""
    out = []
    for item in data.get("items") or []:
        listing = parse_item(item, section)
        if listing is not None:
            out.append(listing)
    return out


def fetch_category(*args, **kwargs):
    """Refuses, always: robots.txt disallows this source for our User-Agent. See BLOCKED_REASON."""
    raise RuntimeError(BLOCKED_REASON)
