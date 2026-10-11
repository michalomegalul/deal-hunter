"""Facebook Marketplace: logged-in Playwright, not a public API -- there isn't one.

Explicitly out of the "respect robots.txt" model the other sources use: this reads pages as the
owner's own logged-in session (a Cookie-Editor export, never committed, loaded from the path in
FB_COOKIES_FILE), which is a ban-risk the owner accepted, not a crawler. Disabled entirely when
FB_COOKIES_FILE is unset -- `run_once` checks that first and does nothing else.

The approach: open a marketplace search, capture the page's own GraphQL responses (the same JSON
the site's React code reads), and parse listings out of them -- never the rendered DOM, which
changes constantly. Low and slow: a handful of queries, `settings.fb_min_gap`-`fb_max_gap` seconds
apart, and the whole cycle itself runs every 20-30 minutes with jitter (see cli.cmd_scrape_fb), not
on the main 10-minute scrape loop. A login wall or checkpoint stops the source for the rest of the
cycle and sends one ntfy alert ("FB cookies expired"), deduplicated via kv so it only fires once
per expiry, not every cycle.

Playwright itself is an optional extra (`deal-hunter[fb]`) so the main image stays small; `run_once`
degrades to a log line if it isn't installed. See docker-compose.yml's `fb-scraper` service.

The GraphQL parser (`parse_graphql_response`) is tested against a hand-made, clearly-marked
*synthetic* fixture (tests/fixtures/fb_graphql_SYNTHETIC.json) -- Facebook's actual schema is
unpublished and changes often, so this is a best-effort shape, not a captured real response.
"""

import logging
import random
import time
from datetime import UTC, datetime
from urllib.parse import quote

from deal_hunter.bazos import Listing
from deal_hunter.sources import make_id

log = logging.getLogger(__name__)

CHECKPOINT_ALERTED_KEY = "fb_checkpoint_alerted"
SAME_SITE = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "unspecified": "Lax"}


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def convert_cookie(raw: dict) -> dict:
    """One Cookie-Editor export entry -> the shape Playwright's `context.add_cookies` wants."""
    cookie = {
        "name": raw["name"],
        "value": raw["value"],
        "domain": raw.get("domain", ".facebook.com"),
        "path": raw.get("path", "/"),
        "httpOnly": bool(raw.get("httpOnly")),
        "secure": bool(raw.get("secure", True)),
        "sameSite": SAME_SITE.get((raw.get("sameSite") or "").lower(), "Lax"),
    }
    if raw.get("session") or raw.get("expirationDate") is None:
        cookie["expires"] = -1
    else:
        cookie["expires"] = float(raw["expirationDate"])
    return cookie


def load_cookies(path: str) -> list[dict]:
    """Read a Cookie-Editor JSON export (a plain list of cookie objects) and convert every entry."""
    import json

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return [convert_cookie(c) for c in raw]


def search_url(query: str, city: str = "prague", radius_km: int = 40) -> str:
    return (
        f"https://www.facebook.com/marketplace/{city}/search/"
        f"?query={quote(query)}&sortBy=creation_time_descend&radius={radius_km}"
    )


def is_checkpoint(url: str) -> bool:
    """A navigation ended up on a login wall or security checkpoint instead of the search page."""
    return any(s in url for s in ("/checkpoint/", "/login/", "/login.php"))


def parse_listing_node(node: dict, section: str) -> Listing | None:
    listing = (node or {}).get("listing") or node
    native_id = listing.get("id")
    if native_id is None:
        return None
    price = listing.get("listing_price") or {}
    amount = price.get("amount")
    location = ((listing.get("location") or {}).get("reverse_geocode") or {}).get("city")
    photo = ((listing.get("primary_listing_photo") or {}).get("image") or {}).get("uri")
    created = listing.get("creation_time")
    posted_at = datetime.fromtimestamp(created, tz=UTC).date() if created else None
    return Listing(
        id=make_id("fb", native_id),
        source="fb",
        external_id=str(native_id),
        url=f"https://www.facebook.com/marketplace/item/{native_id}/",
        title=listing.get("marketplace_listing_title") or "",
        price_czk=int(amount) if amount is not None else None,
        price_kind="fixed" if amount is not None else "unknown",
        location=location,
        images=[photo] if photo else [],
        posted_at=posted_at,
        section=section,
    )


def _edges(payload: dict) -> list[dict]:
    """Walk to the feed's edge list, wherever this particular query nested it."""
    data = payload.get("data") or {}
    search = data.get("marketplace_search") or data.get("viewer", {}).get("marketplace_search") or {}
    feed = search.get("feed_units") or {}
    return feed.get("edges") or []


def parse_graphql_response(payload: dict, section: str) -> list[Listing]:
    """Every listing in one captured GraphQL response. See the module docstring on the fixture."""
    out = []
    for edge in _edges(payload):
        listing = parse_listing_node(edge.get("node") or {}, section)
        if listing is not None:
            out.append(listing)
    return out


def notify_checkpoint(settings, conn) -> None:
    """One ntfy push, deduplicated via kv so a stuck checkpoint doesn't alert every cycle."""
    from deal_hunter.db import repository as repo

    if repo.kv_get(conn, CHECKPOINT_ALERTED_KEY, False):
        return
    from deal_hunter.alerts import Ntfy

    ntfy = Ntfy(
        settings.ntfy_url,
        settings.ntfy_token,
        settings.ntfy_topic,
        settings.ntfy_public_url,
        settings.ntfy_feedback_topic,
        settings.ntfy_feedback_token,
    )
    ntfy.publish(
        "FB cookies expired",
        "Facebook Marketplace hit a login wall or checkpoint. Export fresh cookies with "
        "Cookie-Editor and update FB_COOKIES_FILE; the fb source is paused until then.",
        priority=4,
        tags=["warning"],
    )
    repo.kv_set(conn, CHECKPOINT_ALERTED_KEY, True)


def clear_checkpoint_alert(conn) -> None:
    """Call this once cookies are refreshed, so the next checkpoint alerts again."""
    from deal_hunter.db import repository as repo

    repo.kv_set(conn, CHECKPOINT_ALERTED_KEY, False)


def run_once(settings, conn) -> None:
    """One cycle: every configured query, low and slow. No-op unless FB_COOKIES_FILE is set."""
    if not settings.fb_cookies_file:
        log.debug("fb source disabled: FB_COOKIES_FILE not set")
        return
    if not available():
        log.warning(
            "fb source enabled (FB_COOKIES_FILE set) but the 'playwright' package isn't installed: "
            "install the 'fb' extra and run `playwright install chromium`"
        )
        return
    if not settings.fb_queries:
        log.debug("fb source enabled but HUNTER_FB_QUERIES is empty: nothing to search")
        return

    from deal_hunter.db import repository as repo

    cookies = load_cookies(settings.fb_cookies_file)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=settings.user_agent)
            context.add_cookies(cookies)
            page = context.new_page()
            captured: list[dict] = []
            page.on(
                "response",
                lambda resp: (
                    captured.append(_safe_json(resp))
                    if "graphql" in resp.url and resp.request.method == "POST"
                    else None
                ),
            )
            checkpoint = False
            for query, section in settings.fb_queries:
                page.goto(
                    search_url(query, settings.fb_city, settings.fb_radius_km), wait_until="networkidle"
                )
                if is_checkpoint(page.url):
                    checkpoint = True
                    log.warning("fb: checkpoint/login wall hit on query %r", query)
                    break
                time.sleep(random.uniform(2, 5))
                found = 0
                for payload in captured:
                    if payload is None:
                        continue
                    for listing in parse_graphql_response(payload, section):
                        repo.save_listing(conn, listing, detail=True)
                        found += 1
                captured.clear()
                log.info("fb: %r -> %d listings", query, found)
                time.sleep(random.uniform(settings.fb_min_gap, settings.fb_max_gap))
        finally:
            browser.close()

    if checkpoint:
        notify_checkpoint(settings, conn)
    else:
        clear_checkpoint_alert(conn)


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 - not every response is JSON, or the body may be gone already
        return None
