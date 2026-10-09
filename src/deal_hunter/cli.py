"""Command line entry point: `hunt scrape | backfill | recheck | stats | db`."""

import argparse
import logging

from deal_hunter import scraper
from deal_hunter.config import MAX_BACKFILL_PAGES, load_settings
from deal_hunter.db.cli import register as register_db


def _with_db(work):
    """Open the main and throttle connections, migrate, run `work(client, conn)`, close everything."""
    from deal_hunter.db import repository as repo

    settings = load_settings()
    with repo.connect(autocommit=True) as conn, repo.connect(autocommit=True) as throttle_conn:
        repo.migrate(conn)
        with scraper.make_client(settings, throttle_conn) as client:
            return work(client, conn)


def cmd_scrape(args: argparse.Namespace) -> None:
    settings = load_settings()
    pages = args.pages or settings.pages
    if args.dry_run:
        with scraper.make_client(settings) as client:
            listings = scraper.dry_run(client, pages)
        for x in listings:
            price = f"{x.price_czk:,} Kč".replace(",", " ") if x.price_czk is not None else x.price_kind
            where = f"{x.location or ''} {x.psc or ''}".strip()
            print(f"{x.id}  {price:>12}  {x.posted_at or ''!s:<10}  {where:<28}  {x.title}")
        print(f"\n{len(listings)} listings parsed (dry run, nothing saved)")
        return

    def step():
        stats = _with_db(lambda client, conn: scraper.scrape_once(client, conn, pages))
        print(stats.summary(), flush=True)

    if args.loop:
        scraper.forever(settings.loop_minutes * 60, step, "scrape")
    else:
        step()


def cmd_backfill(args: argparse.Namespace) -> None:
    stats = _with_db(lambda client, conn: scraper.backfill(client, conn, args.pages))
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="hunt", description="Bazoš GPU deal hunter")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("scrape", help="read the RSS feed + first category pages, fetch new ads")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one cycle and exit (the default)")
    mode.add_argument("--loop", action="store_true", help="service: a cycle every HUNTER_LOOP_MINUTES")
    p.add_argument("--dry-run", action="store_true", help="parse and print, no database, no detail pages")
    p.add_argument("--pages", type=int, default=None, help="category pages per cycle (default 2)")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("backfill", help="walk deeper category pages once to seed history")
    p.add_argument("--pages", type=int, default=MAX_BACKFILL_PAGES, help=f"max {MAX_BACKFILL_PAGES}")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("recheck", help="re-fetch active ads not seen lately; mark removed ones gone")
    p.add_argument("--loop", action="store_true", help="service: repeat every HUNTER_RECHECK_HOURS")
    p.add_argument("--older-than", type=float, default=None, help="hours since last seen (default 6)")
    p.add_argument("--limit", type=int, default=None, help="max ads per run (default 1500)")
    p.set_defaults(func=cmd_recheck)

    p = sub.add_parser("stats", help="what has been collected so far")
    p.set_defaults(func=cmd_stats)

    register_db(sub)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)
