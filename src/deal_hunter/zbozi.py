"""New prices from Zboží.cz: the lowest current offer for a handful of curated product pages.

Zboží.cz is a Czech price comparison site. Its robots.txt (checked 2026-10-10) disallows search
(`/hledani/`, `/hledej/`, `?q=`), multi-choice filters (any URL with a comma), `sId=`/`pId=`/
`catOrder=` parameters, click-throughs to shops (`/action/`, `/clickthru`) and most URLs with several
parameters. Product pages (`/vyrobek/<slug>/`) are allowed, and the sitemap lists them.

Category listings are rendered in the browser (their server-side HTML has no products), so there is
no robots-compliant way to go from "rtx 3060 12gb" to its product pages automatically. Instead,
`new_price_pages.yaml` maps each pricing key to a few product pages (common budget cards of that
chip), picked once by hand from the sitemap and checked with this parser. A model's new price is the
lowest offer across its pages: the cheapest way to buy one new, roughly.

A product page is server-rendered: its `__NEXT_DATA__` JSON has the product name, the category, the
lowest offer (`minPrice`, in hellers) and the number of shops. A page is only accepted if it is in the
graphics card category and its name, run through our own GPU normalizer, gives the same pricing key:
a wrong or moved URL can't silently price the wrong card.

Fetched weekly (HUNTER_NEW_PRICE_DAYS), at least 6 s apart, with the honest User-Agent and a request
budget of its own (`http_throttle` row 3): ~60 requests a week.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib import resources
from urllib.parse import urlsplit

import yaml

from deal_hunter import normalize

log = logging.getLogger(__name__)

BASE_URL = "https://www.zbozi.cz"
GPU_CATEGORY = "pocitace/pocitacove-komponenty/graficke-karty"
RETRY_AFTER = timedelta(hours=24)  # a failed or rejected read is retried after this long
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)


@dataclass(frozen=True)
class Product:
    name: str
    category: str  # 'pocitace/pocitacove-komponenty/graficke-karty'
    min_price: int | None  # Kč, the lowest offer; None when no shop offers it right now
    shops: int


def allowed(url: str) -> bool:
    """Only product pages without parameters or commas (see the module doc for robots.txt)."""
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc == "www.zbozi.cz"
        and re.fullmatch(r"/vyrobek/[a-z0-9-]+/", parts.path) is not None
        and not parts.query
        and "," not in url
        and "%2C" not in url.upper()
    )


def parse_product(html: str) -> Product | None:
    """The product name, category, lowest offer and shop count from a product page, or None."""
    m = NEXT_DATA.search(html)
    if m is None:
        return None
    try:
        data = json.loads(m.group(1))["props"]["pageProps"]["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("name"):
        return None
    offers = data.get("offers") or {}
    shops = int(offers.get("shopCount") or 0)
    raw = data.get("minPrice")
    min_price = round(raw / 100) if isinstance(raw, int | float) and raw > 0 and shops else None
    category = (data.get("category") or {}).get("path") or ""
    return Product(" ".join(str(data["name"]).split()), category, min_price, shops)


def check(product: Product, model: str) -> str | None:
    """Why this page can't be the model's new price (None = fine)."""
    if product.category != GPU_CATEGORY:
        return f"category {product.category!r} is not graphics cards"
    got = normalize.classify(product.name, "graficka").model
    if got != model:
        return f"{product.name!r} normalizes to {got!r}, not {model!r}"
    return None


@cache
def pages() -> dict[str, tuple[str, ...]]:
    """{pricing key: product page URLs} from new_price_pages.yaml."""
    raw = yaml.safe_load(resources.files("deal_hunter").joinpath("new_price_pages.yaml").read_text())
    out = {}
    for model, urls in (raw.get("models") or {}).items():
        bad = [u for u in urls if not allowed(u)]
        if bad:
            raise ValueError(f"new_price_pages.yaml: not an allowed product URL for {model}: {bad}")
        out[model] = tuple(urls)
    return out


@dataclass
class FetchStats:
    fetched: int = 0
    priced: int = 0
    rejected: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"new prices: {self.fetched} pages read, {self.priced} with offers, {self.rejected} rejected, "
            f"{self.errors} errors"
        )


def fetch_due(http, conn, refresh_days: float, limit: int, now: datetime | None = None) -> FetchStats:
    """Read the product pages not read in `refresh_days` (at most `limit` per call) and store them."""
    from deal_hunter.db import repository as repo

    now = now or datetime.now(UTC)
    last = repo.new_price_fetched_at(conn)
    stats = FetchStats()

    def is_due(url: str) -> bool:
        ok, tried = last.get(url, (None, None))
        if refresh_days <= 0:
            return True
        if ok is not None and now - ok < timedelta(days=refresh_days):
            return False  # fresh
        return tried is None or tried == ok or now - tried >= RETRY_AFTER  # a failure waits a day

    due = [(model, url) for model, urls in pages().items() for url in urls if is_due(url)]
    for model, url in due[:limit]:
        try:
            resp = http.get(url)
        except Exception as exc:  # noqa: BLE001 - the next run tries again
            stats.errors += 1
            repo.save_new_price(conn, model, url, None, None, None, f"{type(exc).__name__}: {exc}"[:300])
            continue
        stats.fetched += 1
        product = parse_product(resp.text) if resp.status == 200 else None
        if product is None:
            stats.errors += 1
            repo.save_new_price(conn, model, url, None, None, None, f"HTTP {resp.status}, no product data")
            continue
        why = check(product, model)
        if why:
            stats.rejected += 1
            log.warning("zbozi: %s: %s", url, why)
            repo.save_new_price(conn, model, url, product.name, None, product.shops, why[:300])
            continue
        repo.save_new_price(conn, model, url, product.name, product.min_price, product.shops)
        stats.priced += product.min_price is not None
    return stats
