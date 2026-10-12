"""Shop outlets as a listing source: Mironet.cz "rozbaleno" (unboxed) and "bazar" (used) items.

Mironet sells returned, unboxed and used stock with a warranty next to its new goods. Its outlet
pages (`/vyprodej/?v=rozbaleno`, `?v=bazar`) are only landing pages that link to the category
listings filtered on the "Stav" parameter (`/graficke-karty+fc14402/?EXPT=&pn10658[]=Rozbaleno`);
those listings are server-rendered HTML, so no JavaScript or private API is involved. Each product
block has an id, the full name (a hidden input of its buy form), a short spec line, the price with
VAT (in Kč) and a badge saying which outlet it belongs to.

robots.txt (checked 2026-10-12, https://www.mironet.cz/robots.txt) disallows `/ajax/`, `/podpora/`,
`/user/`, `/order/`, `/sr2/`, a few `/productdetail/` and `/productlist/remember` actions, and any
URL with a `sort=` parameter. Category listings, the `EXPT`/`pn10658[]` filter and `PgID` paging are
not among them. It also names AI crawlers (ClaudeBot, GPTBot, ...) with the same disallow list and
blocks MegaIndex, Amazonbot and CCBot outright; this client identifies itself as deal-hunter, like
the Bazoš scraper does, and is none of those. `allowed()` accepts only the category listings below.

Mapping: a product category maps onto a canonical section where the item clearly belongs (graphics
cards -> graficka, notebooks -> notebook, computers -> pc, hard disks and SSDs -> hdd) and onto one
of Bazoš's watch-only sections for the rest (RAM, motherboards, cases and power supplies, switches),
so watchlists can match them while the price pipeline ignores them (normalize.classify marks those
sections excluded_reason='watch_only').

Prices are shop prices, not used-market prices: they are stored with price_kind='retail', which
`repository.price_samples` does not read (so an unboxed card never inflates a model's used fair
price) but candidates and watchlists do.

Read every HUNTER_SHOP_HOURS (6 h) at least 6 s apart (`http_throttle` row 4), a few pages per category.
"""

import html
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from deal_hunter.bazos import Listing
from deal_hunter.sources import make_id

SOURCE = "mironet"
BASE_URL = "https://www.mironet.cz"
CONDITIONS = {"rozbaleno": "Rozbaleno", "bazar": "Bazar"}  # badge type -> value of the "Stav" filter
SHIPPING_NOTE = "Doprava po celé ČR, e-shop Mironet.cz."


@dataclass(frozen=True)
class Category:
    path: str  # '/graficke-karty+fc14402/'
    condition: str  # a key of CONDITIONS
    section: str  # a canonical section, or one of bazos.SECTIONS' watch-only sections


CATEGORIES = (
    Category("/graficke-karty+fc14402/", "rozbaleno", "graficka"),
    Category("/notebooky+fc1504/", "rozbaleno", "notebook"),
    Category("/pocitace+fc1658/", "rozbaleno", "pc"),
    Category("/pocitace+fc1658/", "bazar", "pc"),
    Category("/pevne-disky-a-ssd+fc14401/", "rozbaleno", "hdd"),
    Category("/pameti-ram+fc14400/", "rozbaleno", "pamet"),
    Category("/zakladni-desky+fc14397/", "rozbaleno", "motherboard"),
    Category("/skrine+fc14417/", "rozbaleno", "case"),
    Category("/zdroje+fc14418/", "rozbaleno", "case"),
    Category("/zdroje+fc14418/", "bazar", "case"),
    Category("/switche+fc25074/", "rozbaleno", "sit"),
)

CATEGORY_PATH = re.compile(r"/[a-z0-9-]+\+fc\d+/")
QUERY = re.compile(r"EXPT=&pn10658\[\]=(?:Rozbaleno|Bazar)(?:&PgID=\d{1,2})?")


def category_url(cat: Category, page: int = 1) -> str:
    """The filtered listing of one category: its outlet items only, page `page` (1-based)."""
    url = f"{BASE_URL}{cat.path}?EXPT=&pn10658[]={CONDITIONS[cat.condition]}"
    return url + (f"&PgID={page}" if page > 1 else "")


def allowed(url: str) -> bool:
    """Only the outlet-filtered category listings, optionally with paging (see the module doc).

    One category segment and exactly the filter and paging parameters: that excludes everything
    robots.txt names (/ajax/, /user/, /order/, ..., `sort=`) without listing it."""
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc == "www.mironet.cz"
        and CATEGORY_PATH.fullmatch(parts.path) is not None
        and QUERY.fullmatch(parts.query) is not None
    )


BLOCK = re.compile(r'class="item_b\b[^"]*"\s+id="item_b_(\d+)"')
NAME = re.compile(r'name="NameItem" value="([^"]*)"')
PRICE = re.compile(r'class="item_b_cena">\s*([\d\s&;a-z]*?)\s*Kč')
HREF = re.compile(r'href="(/[^"]*\+dp\d+/)"')
SPEC = re.compile(r'class="item_b_popis"[^>]*>(.*?)</a>', re.DOTALL)
IMAGE = re.compile(r'src="(//www\.mironet\.cz/Foto/s\d+/\d+\.webp)"')
BADGES = re.compile(r"data-type='([a-z]+)'")
BLOCK_MAX = 40_000  # the last block would otherwise run on into the page footer


def _text(raw: str) -> str:
    return " ".join(html.unescape(raw).replace("\xa0", " ").split())


def parse_listing(block: str, product_id: str, category: Category) -> Listing | None:
    """One product block, or None if it is not an item of the outlet the page was asked for.

    A listing whose "Stav" filter wasn't applied would show new goods; requiring the badge to name
    the expected outlet keeps those out whatever the server does with an unknown filter value.
    """
    name, href = NAME.search(block), HREF.search(block)
    if category.condition not in BADGES.findall(block) or name is None or href is None:
        return None
    price = PRICE.search(block)
    digits = re.sub(r"\D", "", html.unescape(price.group(1))) if price else ""
    spec, image = SPEC.search(block), IMAGE.search(block)
    description = " ".join(x for x in (_text(spec.group(1)) if spec else "", SHIPPING_NOTE) if x)
    return Listing(
        id=make_id(SOURCE, product_id),
        source=SOURCE,
        external_id=product_id,
        url=BASE_URL + href.group(1),
        title=_text(name.group(1)),
        price_czk=int(digits) if digits else None,
        price_kind="retail" if digits else "unknown",
        seller="Mironet.cz",
        description=description,
        images=["https:" + image.group(1)] if image else [],
        raw_category=raw_category(category),
        section=category.section,
    )


def parse_page(text: str, category: Category) -> list[Listing]:
    """Every outlet item of one listing page, in page order (the same product is listed once)."""
    starts = [(m.start(), m.group(1)) for m in BLOCK.finditer(text)]
    out: dict[str, Listing] = {}
    for i, (start, product_id) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else start + BLOCK_MAX
        if product_id in out:
            continue
        item = parse_listing(text[start:end], product_id, category)
        if item is not None:
            out[product_id] = item
    return list(out.values())


def has_products(text: str) -> bool:
    """Does the page list any product at all (outlet or not)?"""
    return BLOCK.search(text) is not None


def raw_category(cat: Category) -> str:
    """`listings.raw_category` of a category's items: what mark_missing_gone retires them by."""
    return f"{cat.condition}:{cat.path.strip('/')}"


def has_next_page(text: str, page: int) -> bool:
    """Does the page link to page `page + 1`?"""
    return f"PgID={page + 1}" in text
