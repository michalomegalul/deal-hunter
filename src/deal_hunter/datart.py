"""New prices from Datart.cz category pages: a second feed next to Zboží.cz.

Zboží's category listings are rendered in the browser, so its product pages are curated by hand
(zbozi.py, new_price_pages.yaml). Datart is the opposite: its category pages are server-rendered
and carry every listed product as schema.org JSON-LD (`<script type="application/ld+json"
id="category-product-list-json">`: name, price in Kč with VAT, URL), 22 products to a page, so there
is nothing to curate: the product names go through the same normalizers (normalize.py for graphics
cards, storage.py for disks) and a product that yields a pricing key is stored as a `new_prices`
row with shop='datart' (one row per product; the product URL identifies it).

A model's new price is the lowest fresh row of any shop (repository.latest_new_offers), so Datart
can only lower what Zboží says or fill in models it has no curated page for. Disks are stored the
way Zboží's pages are: per class and capacity ('hdd35 nas 8tb'), pooled into Kč/TB by
pricing.disk_new_per_tb; only pooled 3.5" classes are kept (2.5" drives and SSDs have no new price).

robots.txt (checked 2026-10-12, https://www.datart.cz/robots.txt) disallows the cart, customer area,
`/vyhledavani` (search), any `/filter/` URL except a few sort orders, and `?tab=`, `?listType=`,
`?mobile=`, `?desktop=`, `?uiShowFilter=`, `?orderBY` and `?do=` parameters. Plain category pages and
their `?page=N` paging are allowed; `allowed()` accepts nothing else. Datart's "last pieces" page
(`/vyprodej-poslednich-kusu.html`) was looked at and dropped: 98 pages of new stock of every kind (e-bikes, drills,
toys) in no useful order, with a handful of its own bazar items mixed in, and no category page for those.

Read weekly (HUNTER_NEW_PRICE_DAYS), at least 6 s apart (`http_throttle` row 3, shared with Zboží),
HUNTER_DATART_PAGES (4) pages per category.
"""

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from deal_hunter import normalize, storage
from deal_hunter.zbozi import RETRY_AFTER, FetchStats

log = logging.getLogger(__name__)

SHOP = "datart"
BASE_URL = "https://www.datart.cz"
MIN_PRICE = 500  # Kč; anything cheaper on these pages is an accessory or a mis-parse
LIST_JSON = re.compile(
    r'<script type="application/ld\+json" id="category-product-list-json">(.*?)</script>', re.DOTALL
)
PAGE_QUERY = re.compile(r"page=\d{1,2}")
CATEGORY_PATH = re.compile(r"/[a-z0-9-]+\.html")  # a plain category page, never /vyhledavani, /kosik, ...


@dataclass(frozen=True)
class Category:
    path: str
    section: str  # which normalizer reads the names: graficka | hdd


CATEGORIES = (
    Category("/graficke-karty.html", "graficka"),
    Category("/interni-pevne-disky.html", "hdd"),
)


@dataclass(frozen=True)
class Product:
    name: str
    price: int  # Kč with VAT
    url: str


def category_url(cat: Category, page: int = 1) -> str:
    return f"{BASE_URL}{cat.path}" + (f"?page={page}" if page > 1 else "")


def allowed(url: str) -> bool:
    """Only the category pages above, with no parameter or just `?page=N`: that excludes everything
    robots.txt names (cart, customer area, search, `/filter/`, `?tab=`, `?do=`, ...) without listing it."""
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc == "www.datart.cz"
        and CATEGORY_PATH.fullmatch(parts.path) is not None
        and parts.path in {c.path for c in CATEGORIES}
        and (parts.query == "" or PAGE_QUERY.fullmatch(parts.query) is not None)
    )


def parse_products(text: str) -> list[Product]:
    """The products of one category page from its JSON-LD list (empty if there is none)."""
    m = LIST_JSON.search(text)
    if m is None:
        return []
    try:
        entries = json.loads(m.group(1))["itemListElement"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    out = []
    for entry in entries:
        item = entry.get("item") if isinstance(entry, dict) else None
        if not isinstance(item, dict) or not item.get("name") or not item.get("url"):
            continue
        try:
            price = round(float((item.get("offers") or {}).get("price")))
        except (TypeError, ValueError):
            continue
        if price < MIN_PRICE:
            continue
        url = item["url"] if item["url"].startswith("http") else BASE_URL + item["url"]
        out.append(Product(" ".join(html.unescape(str(item["name"])).split()), price, url))
    return out


def pricing_key(product: Product, section: str) -> str | None:
    """The `new_prices.model` of a product: a GPU pricing key ('rtx 3060 12gb'), or for a 3.5" disk
    its class and capacity ('hdd35 nas 8tb', the form zbozi.py stores); None if our normalizers can't
    place it (an unknown card, a 2.5" drive, an SSD, an unreadable name)."""
    norm = normalize.classify(product.name, section)
    if norm.model is None:
        return None
    if section != "hdd":
        return norm.model
    tb = (norm.features or {}).get("capacity_tb")
    if not storage.pooled_key(norm.model) or not tb:
        return None
    return f"{norm.model} {tb:g}tb"


def fetch_due(http, conn, refresh_days: float, pages: int, now: datetime | None = None) -> FetchStats:
    """Read the category pages if they are due (not read in `refresh_days`; a failed read waits a day)
    and store every product that yields a pricing key. `stats.fetched` counts pages, `priced` products stored."""
    from deal_hunter.db import repository as repo

    now = now or datetime.now(UTC)
    stats = FetchStats()
    ok, tried = (_kv_time(repo.kv_get(conn, f"new_prices:{SHOP}:{k}")) for k in ("ok", "tried"))
    if refresh_days > 0:
        if ok is not None and now - ok < timedelta(days=refresh_days):
            return stats
        if tried is not None and tried != ok and now - tried < RETRY_AFTER:
            return stats
    repo.kv_set(conn, f"new_prices:{SHOP}:tried", now.isoformat())
    for cat in CATEGORIES:
        for page in range(1, pages + 1):
            url = category_url(cat, page)
            if not allowed(url):  # a programming error, never a runtime condition
                raise ValueError(f"datart: {url} is not an allowed URL")
            try:
                resp = http.get(url)
            except Exception as exc:  # noqa: BLE001 - the next due run tries again
                stats.errors += 1
                log.warning("datart: %s: %s: %s", url, type(exc).__name__, exc)
                break
            stats.fetched += 1
            products = parse_products(resp.text) if resp.status == 200 else []
            if not products:
                stats.errors += int(resp.status != 200 or page == 1)  # an empty page 2+ is just the end
                break
            for product in products:
                key = pricing_key(product, cat.section)
                if key is None:
                    stats.rejected += 1
                    continue
                repo.save_new_price(conn, key, product.url, product.name, product.price, 1, shop=SHOP)
                stats.priced += 1
    if not stats.errors:
        repo.kv_set(conn, f"new_prices:{SHOP}:ok", now.isoformat())
    return stats


def _kv_time(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None
