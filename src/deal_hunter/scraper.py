"""Scrape cycles: read the RSS feed + category pages, fetch new ads, re-check old ones for removal."""

import logging
import time
from collections.abc import Callable

import psycopg

from deal_hunter import bazos
from deal_hunter.bazos import Listing
from deal_hunter.config import MAX_BACKFILL_PAGES, Settings
from deal_hunter.db import repository as repo
from deal_hunter.http import Page, PoliteClient

log = logging.getLogger(__name__)


class DbThrottle:
    """Spaces requests across processes (the scraper and the rechecker share one budget).

    Each request takes a row lock on `http_throttle`, waits until `min_delay` has passed since the
    last request by anyone, stamps the row and commits. A second process blocks on the lock meanwhile.
    Row 1 is Bazoš; row 2 is the auction site (nms.gov.cz), which has its own, slower budget.
    """

    def __init__(self, conn: psycopg.Connection, min_delay: float, sleep=time.sleep, row: int = 1):
        self.conn, self.min_delay, self.sleep, self.row = conn, min_delay, sleep, row

    def wait_turn(self) -> None:
        with self.conn.transaction():
            waited = self.conn.execute(
                "SELECT extract(epoch FROM clock_timestamp() - last_request) FROM http_throttle WHERE id = %s FOR UPDATE",
                (self.row,),
            ).fetchone()[0]
            gap = self.min_delay - float(waited)
            if gap > 0:
                self.sleep(gap)
            self.conn.execute(
                "UPDATE http_throttle SET last_request = clock_timestamp() WHERE id = %s", (self.row,)
            )


def make_client(settings: Settings, throttle_conn: psycopg.Connection | None = None) -> PoliteClient:
    throttle = DbThrottle(throttle_conn, settings.min_delay) if throttle_conn is not None else None
    return PoliteClient(settings.user_agent, min_delay=settings.min_delay, throttle=throttle)


def read_category(
    client: PoliteClient, pages: range, stats: repo.RunStats, section: str = bazos.CATEGORY
) -> dict[int, Listing]:
    """Listings from category pages (stops early at an empty page)."""
    found: dict[int, Listing] = {}
    for page in pages:
        url = bazos.category_url(page, section)
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001 - keep going, log it in the run
            stats.errors.append(f"{url}: {exc}")
            continue
        if resp.status != 200:
            stats.errors.append(f"{url}: HTTP {resp.status}")
            continue
        items = bazos.parse_category(resp.text)
        if not items:
            log.info("%s has no ads, stopping", url)
            break
        for item in items:
            item.section = section
            found.setdefault(item.id, item)
    return found


def read_rss(client: PoliteClient, stats: repo.RunStats, section: str = bazos.CATEGORY) -> dict[int, Listing]:
    try:
        resp = client.get(bazos.rss_url(section))
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        items = bazos.parse_rss(resp.text)
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(f"rss {section}: {exc}")
        return {}
    return {
        i.id: Listing(
            id=i.id, url=i.url, title=i.title, price_czk=i.price_czk, price_kind=i.price_kind, section=section
        )
        for i in items
    }


def read_section(client: PoliteClient, section: str, pages: int, stats: repo.RunStats) -> dict[int, Listing]:
    """RSS feed + the first `pages` category pages of one section (category data is richer)."""
    found = read_rss(client, stats, section)
    found.update(read_category(client, range(pages), stats, section))
    return found


def wants_detail(item: Listing) -> bool:
    """Office sections are mostly gaming PCs and MacBooks we never price: only fetch the detail page
    of ads the normalizer doesn't exclude. That keeps the request budget for what matters."""
    section = bazos.SECTIONS.get(item.section)
    if section is None or section.details == "all":
        return True
    from deal_hunter.normalize import classify

    return classify(item.title, item.section).excluded_reason is None


def fetch_ad(client: PoliteClient, url: str) -> tuple[Listing | None, str | None, Page]:
    """(listing, gone_reason, page). Exactly one of listing/gone_reason is set, unless the status is odd.

    A redirect to the same ad id (slug changed after an edit) is followed once; a redirect anywhere
    else means Bazoš no longer shows the ad.
    """
    page = client.get(url)
    listing_id = bazos.ad_id(url)
    if page.status in (301, 302, 303, 307, 308):
        target = page.location or ""
        if target and bazos.ad_id(target) == listing_id:
            if target.startswith("/"):
                target = bazos.BASE_URL + target
            page = client.get(target)
            url = target
        else:
            return None, "redirect", page
    if page.status in (404, 410):
        return None, f"http_{page.status}", page
    if page.status != 200:
        return None, None, page
    listing = bazos.parse_detail(page.text, url)
    if listing is None:
        return None, "removed_page", page
    return listing, None, page


def ingest(
    client: PoliteClient, conn: psycopg.Connection, found: dict[int, Listing], stats: repo.RunStats
) -> None:
    """Save what we found: fetch the detail page for ads we don't have in full yet."""
    stats.seen += len(found)
    have = repo.detailed_ids(conn, list(found))
    for listing_id, summary in found.items():
        item, detail = summary, False
        if listing_id not in have and wants_detail(summary):
            try:
                full, gone, page = fetch_ad(client, summary.url)
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"{summary.url}: {exc}")
                full, gone, page = None, None, None
            if full is not None:
                item, detail = full, True
                # The list page knows the seller's name and the thumbnail even when the detail page is odd.
                item.seller = item.seller or summary.seller
                item.section = summary.section
            elif gone is not None:
                continue  # removed between the list page and now: nothing worth storing
            elif page is not None:
                stats.errors.append(f"{summary.url}: HTTP {page.status}")
        is_new, changed = repo.save_listing(conn, item, detail)
        stats.new += is_new
        stats.updated += not is_new
        stats.price_changes += changed


def _run(conn: psycopg.Connection, kind: str, body: Callable[[repo.RunStats], None]) -> repo.RunStats:
    stats = repo.RunStats(kind)
    run_id = repo.start_run(conn, kind)
    try:
        body(stats)
    except Exception as exc:
        stats.errors.append(f"aborted: {type(exc).__name__}: {exc}")
        conn.rollback()
        raise
    finally:
        repo.finish_run(conn, run_id, stats)
        log.info(stats.summary())
    return stats


def section_pages(sections: dict[str, int] | None, gpu_pages: int) -> dict[str, int]:
    """{section: category pages per cycle}. GPUs read `gpu_pages` (HUNTER_PAGES), others their default."""
    if sections is not None:
        return sections
    return {s.slug: (gpu_pages if s.slug == bazos.CATEGORY else s.pages) for s in bazos.SECTIONS.values()}


def scrape_once(
    client: PoliteClient, conn: psycopg.Connection, pages: int, sections: dict[str, int] | None = None
) -> repo.RunStats:
    """One cycle over every section: RSS + the first category pages; details only for new ads.

    With the defaults that is 3 RSS feeds + 4 category pages = 7 requests, plus one per new ad
    worth keeping (all new GPU ads, only in-scope office ads).
    """

    def body(stats):
        start = client.requests
        for section, n in section_pages(sections, pages).items():
            ingest(client, conn, read_section(client, section, n, stats), stats)
        stats.requests = client.requests - start

    return _run(conn, "scrape", body)


def backfill(
    client: PoliteClient, conn: psycopg.Connection, pages: int, section: str = bazos.CATEGORY
) -> repo.RunStats:
    """Walk deeper category pages once to seed history (capped at MAX_BACKFILL_PAGES)."""

    def body(stats):
        start = client.requests
        for page in range(min(pages, MAX_BACKFILL_PAGES)):
            found = read_category(client, range(page, page + 1), stats, section)
            if not found:
                break
            ingest(client, conn, found, stats)
            log.info("backfill %s page %d: %d ads (%d new so far)", section, page, len(found), stats.new)
        stats.requests = client.requests - start

    return _run(conn, "backfill", body)


def recheck(client: PoliteClient, conn: psycopg.Connection, older_than_hours: float, limit: int):
    """Re-fetch active ads we haven't seen lately: refresh price/views, or mark them gone."""

    def body(stats):
        start = client.requests
        due = repo.due_for_recheck(conn, older_than_hours, limit)
        log.info("recheck: %d ads due", len(due))
        for listing_id, url in due:
            stats.seen += 1
            try:
                listing, gone, page = fetch_ad(client, url)
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"{url}: {exc}")
                continue
            if listing is not None:
                _, changed = repo.save_listing(conn, listing, detail=True)
                stats.updated += 1
                stats.price_changes += changed
            elif gone is not None:
                stats.gone += repo.mark_gone(conn, listing_id, gone)
            else:
                stats.errors.append(f"{url}: HTTP {page.status}")
        stats.requests = client.requests - start

    return _run(conn, "recheck", body)


def dry_run(client: PoliteClient, pages: int, sections: dict[str, int] | None = None) -> list[Listing]:
    """Read RSS + category pages without a database and return what was parsed."""
    stats = repo.RunStats("dry-run")
    found: dict[int, Listing] = {}
    for section, n in section_pages(sections, pages).items():
        found.update(read_section(client, section, n, stats))
    for err in stats.errors:
        log.warning(err)
    return list(found.values())


def forever(every_seconds: float, step: Callable[[], object], name: str) -> None:
    """Run `step` every `every_seconds` (measured start to start). Errors are logged, never fatal."""
    while True:
        started = time.monotonic()
        try:
            step()
        except Exception:
            log.exception("%s cycle failed", name)
        wait = every_seconds - (time.monotonic() - started)
        if wait > 0:
            log.info("%s: next cycle in %.0f s", name, wait)
            time.sleep(wait)
