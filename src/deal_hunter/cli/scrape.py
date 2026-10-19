"""Collecting listings: scrape, backfill, recheck, normalize, review, stats."""

import argparse
import logging
import time

from deal_hunter import scraper
from deal_hunter.bazos import CATEGORY, SECTIONS
from deal_hunter.config import MAX_BACKFILL_PAGES, load_settings


def _with_db(work):
    """Open the main and throttle connections, migrate, run `work(client, conn)`, close everything."""
    from deal_hunter.db import repository as repo

    settings = load_settings()
    with repo.connect(autocommit=True) as conn, repo.connect(autocommit=True) as throttle_conn:
        repo.migrate(conn)
        with scraper.make_client(settings, throttle_conn) as client:
            return work(client, conn)


def _sections(settings, gpu_pages: int) -> dict[str, int]:
    """{section: pages per cycle} for the sections enabled in HUNTER_SECTIONS."""
    every = scraper.section_pages(None, gpu_pages)
    unknown = set(settings.sections) - set(every)
    if unknown:
        raise SystemExit(f"Unknown HUNTER_SECTIONS: {', '.join(sorted(unknown))} (known: {', '.join(every)})")
    return {s: n for s, n in every.items() if s in settings.sections}


def cmd_scrape(args: argparse.Namespace) -> None:
    from deal_hunter.sources import KNOWN_SOURCES

    settings = load_settings()
    unknown = set(settings.sources) - set(KNOWN_SOURCES)
    if unknown:
        raise SystemExit(
            f"Unknown HUNTER_SOURCES: {', '.join(sorted(unknown))} (known: {', '.join(KNOWN_SOURCES)})"
        )
    pages = args.pages or settings.pages
    sections = _sections(settings, pages)
    if args.dry_run:
        with scraper.make_client(settings) as client:
            listings = scraper.dry_run(client, pages, sections)
        for x in listings:
            price = f"{x.price_czk:,} Kč".replace(",", " ") if x.price_czk is not None else x.price_kind
            where = f"{x.location or ''} {x.psc or ''}".strip()
            print(f"{x.id}  {x.section:<8}  {price:>12}  {x.posted_at or ''!s:<10}  {where:<28}  {x.title}")
        print(f"\n{len(listings)} listings parsed (dry run, nothing saved)")
        return

    def step():
        from deal_hunter import normalize
        from deal_hunter.db import repository as repo

        def work(client, conn):
            if "bazos" in settings.sources:
                stats = scraper.scrape_once(client, conn, pages, sections)
                if fast := scraper.fast_recheck(client, conn):
                    print(fast.summary(), flush=True)
            else:
                stats = repo.RunStats("scrape")
            if "aukro" in settings.sources:
                print(scraper.scrape_aukro_once(client, conn, settings.aukro_pages).summary(), flush=True)
            if "mironet" in settings.sources:
                with (
                    repo.connect(autocommit=True) as throttle_conn,
                    _shop_http(settings, throttle_conn) as shop_http,
                ):
                    done = scraper.scrape_shops_once(
                        shop_http, conn, settings.shop_pages, settings.shop_hours
                    )
                if done.requests:  # nothing to say between passes (every HUNTER_SHOP_HOURS)
                    print(done.summary(), flush=True)
            normalize.run(conn)  # cheap: only new listings (or all, after a catalog version bump)
            return stats

        print(_with_db(work).summary(), flush=True)

    if args.loop:
        scraper.forever(settings.loop_minutes * 60, step, "scrape")
    else:
        step()


def cmd_scrape_fb(args: argparse.Namespace) -> None:
    """Facebook Marketplace via a logged-in browser: its own loop, its own (jittered) cadence.

    Never part of `hunt scrape` -- Playwright is a separate extra and a separate compose service,
    and FB wants to be read low and slow, not on the main 10-minute cycle. A no-op, once, if
    FB_COOKIES_FILE isn't set.
    """
    import random

    from deal_hunter import facebook

    settings = load_settings()
    if not settings.fb_cookies_file:
        print("fb source disabled: set FB_COOKIES_FILE to enable it (see .env.example)")
        return

    def step():
        from deal_hunter.db import repository as repo

        with repo.connect(autocommit=True) as conn:
            repo.migrate(conn)
            facebook.run_once(settings, conn)

    if args.loop:
        while True:
            try:
                step()
            except Exception:
                logging.getLogger(__name__).exception("fb cycle failed")
            wait = random.uniform(settings.fb_cycle_min_minutes, settings.fb_cycle_max_minutes) * 60
            logging.getLogger(__name__).info("fb: next cycle in %.0f s", wait)
            time.sleep(wait)
    else:
        step()


def cmd_backfill(args: argparse.Namespace) -> None:
    stats = _with_db(lambda client, conn: scraper.backfill(client, conn, args.pages, args.section))
    print(stats.summary())


def cmd_recheck(args: argparse.Namespace) -> None:
    settings = load_settings()
    hours = args.older_than if args.older_than is not None else settings.recheck_hours
    limit = args.limit or settings.recheck_limit

    def step():
        stats = _with_db(lambda client, conn: scraper.recheck(client, conn, hours, limit))
        print(stats.summary(), flush=True)

    if args.loop:
        scraper.forever(settings.recheck_hours * 3600, step, "recheck")
    else:
        step()


def cmd_stats(args: argparse.Namespace) -> None:
    from deal_hunter.db import repository as repo

    with repo.connect() as conn:
        s = repo.stats(conn)
    print(
        f"Listings: {s['listings']} ({s['active']} active, {s['gone']} gone, {s['with_detail']} with detail, "
        f"{s['fixed_price']} with a price)"
    )
    print(f"Last 24 h: {s['new_24h']} new, {s['gone_24h']} gone. Price changes overall: {s['price_changes']}")
    if s["first"]:
        print(f"Collected from {s['first']:%Y-%m-%d %H:%M} to {s['last']:%Y-%m-%d %H:%M} UTC")
    for kind, n, failed, last in s["runs"]:
        print(f"  {kind:<9} {n:>5} runs, {failed} with errors, last {last:%Y-%m-%d %H:%M}")
    if s["normalized"]:
        total = sum(n for _, n in s["normalized"])
        print("Normalizer: " + ", ".join(f"{k} {n} ({n / total:.0%})" for k, n in s["normalized"]))
    for section, n, active, matched, unmatched, new_24h, first in s["sections"]:
        print(
            f"  {section:<9} {n:>6} listings ({active} active, {new_24h} new in 24 h), {matched} matched to a "
            f"model, {unmatched} unmatched, since {first:%Y-%m-%d}"
        )


def cmd_normalize(args: argparse.Namespace) -> None:
    from deal_hunter import normalize
    from deal_hunter.db import repository as repo

    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        done = normalize.run(conn, everything=args.all)
        print(f"Normalized {done} listings with catalog v{normalize.version()}.")
        if args.verbose:
            for label, n in repo.normalized_summary(conn):
                print(f"  {n:>5}  {label}")
        s = repo.stats(conn)["normalized"]
    total = sum(n for _, n in s) or 1
    print("Overall: " + ", ".join(f"{k} {n} ({n / total:.0%})" for k, n in s))


def cmd_review(args: argparse.Namespace) -> None:
    from deal_hunter.db import repository as repo

    with repo.connect() as conn:
        rows = repo.unmatched(conn, args.limit, args.section)
    for listing_id, title, price, url, section in rows:
        price_s = f"{price:,}".replace(",", " ") if price is not None else "-"
        print(
            f"{listing_id}  {section:<8}  {price_s:>8}  {title}" + (f"\n{'':>30}{url}" if args.urls else "")
        )
    print(
        f"\n{len(rows)} unmatched (not a known model, not excluded). "
        "Add rules to gpu_catalog.yaml (GPUs), office.py (PCs, laptops) or storage_catalog.yaml (disks)."
    )


def _shop_http(settings, throttle_conn=None):
    """The polite client for the shop outlets (Mironet): >= 6 s apart, http_throttle row 4."""
    from deal_hunter.config import SHOP_USER_AGENT, USER_AGENT
    from deal_hunter.http import PoliteClient

    throttle = (
        scraper.DbThrottle(throttle_conn, settings.shop_min_delay, row=4)
        if throttle_conn is not None
        else None
    )
    agent = settings.user_agent if settings.user_agent != USER_AGENT else SHOP_USER_AGENT
    return PoliteClient(agent, min_delay=settings.shop_min_delay, throttle=throttle, timeout=30)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("scrape", help="read the RSS feed + first category pages, fetch new ads")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one cycle and exit (the default)")
    mode.add_argument("--loop", action="store_true", help="service: a cycle every HUNTER_LOOP_MINUTES")
    p.add_argument("--dry-run", action="store_true", help="parse and print, no database, no detail pages")
    p.add_argument("--pages", type=int, default=None, help="category pages per cycle (default 2)")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser(
        "scrape-fb", help="Facebook Marketplace via a logged-in browser; no-op unless FB_COOKIES_FILE is set"
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one cycle and exit (the default)")
    mode.add_argument("--loop", action="store_true", help="service: a cycle every 20-30 min, jittered")
    p.set_defaults(func=cmd_scrape_fb)

    p = sub.add_parser("backfill", help="walk deeper category pages once to seed history")
    p.add_argument("--pages", type=int, default=MAX_BACKFILL_PAGES, help=f"max {MAX_BACKFILL_PAGES}")
    p.add_argument("--section", default=CATEGORY, choices=sorted(SECTIONS), help="default graficka")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("recheck", help="re-fetch active ads not seen lately; mark removed ones gone")
    p.add_argument("--loop", action="store_true", help="service: repeat every HUNTER_RECHECK_HOURS")
    p.add_argument("--older-than", type=float, default=None, help="hours since last seen (default 6)")
    p.add_argument("--limit", type=int, default=None, help="max ads per run (default 1500)")
    p.set_defaults(func=cmd_recheck)

    p = sub.add_parser("stats", help="what has been collected so far")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("normalize", help="map listing titles to canonical models (GPUs, office, disks)")
    p.add_argument("--all", action="store_true", help="redo every listing, not just new/outdated ones")
    p.add_argument("-v", "--verbose", action="store_true", help="print counts per model")
    p.set_defaults(func=cmd_normalize)

    p = sub.add_parser("review", help="titles the normalizer couldn't place")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--urls", action="store_true")
    p.add_argument("--section", choices=sorted(SECTIONS), default=None)
    p.set_defaults(func=cmd_review)
