"""Command line entry point: `hunt scrape | backfill | recheck | normalize | review | stats | db | ...`."""

import argparse
import logging

from deal_hunter import scraper
from deal_hunter.bazos import CATEGORY, SECTIONS
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


def _sections(settings, gpu_pages: int) -> dict[str, int]:
    """{section: pages per cycle} for the sections enabled in HUNTER_SECTIONS."""
    every = scraper.section_pages(None, gpu_pages)
    unknown = set(settings.sections) - set(every)
    if unknown:
        raise SystemExit(f"Unknown HUNTER_SECTIONS: {', '.join(sorted(unknown))} (known: {', '.join(every)})")
    return {s: n for s, n in every.items() if s in settings.sections}


def cmd_scrape(args: argparse.Namespace) -> None:
    settings = load_settings()
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

        def work(client, conn):
            stats = scraper.scrape_once(client, conn, pages, sections)
            normalize.run(conn)  # cheap: only new listings (or all, after a catalog version bump)
            return stats

        print(_with_db(work).summary(), flush=True)

    if args.loop:
        scraper.forever(settings.loop_minutes * 60, step, "scrape")
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
        "Add rules to gpu_catalog.yaml (GPUs) or office.py (PCs, laptops)."
    )


# --- pricing, triage, alerts ------------------------------------------------------------------------


def _kc(n) -> str:
    return f"{n:,}".replace(",", " ") if n is not None else "-"


def _rules(settings):
    from deal_hunter.alerts import Rules, parse_quiet_hours

    start, end = parse_quiet_hours(settings.quiet_hours)
    return Rules(
        enabled=settings.alerts_enabled,
        min_score=settings.alert_min_score,
        max_per_day=settings.alert_max_per_day,
        min_samples=settings.alert_min_samples,
        min_days=settings.alert_min_days,
        quiet_start=start,
        quiet_end=end,
        quiet_bypass=settings.quiet_bypass_score,
        candidate_discount=settings.candidate_discount,
    )


def _llm(settings):
    from deal_hunter.triage import OllamaClient

    return OllamaClient(settings.llm_model, settings.ollama_url) if settings.llm_model else None


def _ntfy(settings, required: bool = False):
    from deal_hunter.alerts import Ntfy

    if not settings.ntfy_token:
        if required:
            raise SystemExit("Missing NTFY_TOKEN (the deal-hunter publisher token) in .env")
        return None
    return Ntfy(
        settings.ntfy_url,
        settings.ntfy_token,
        settings.ntfy_topic,
        settings.ntfy_public_url,
        settings.ntfy_feedback_topic,
        settings.ntfy_feedback_token,
    )


def _estimates(conn):
    from deal_hunter import pricing
    from deal_hunter.db import repository as repo

    return pricing.estimate_all(repo.price_samples(conn, pricing.WINDOW_DAYS))


def cmd_price(args: argparse.Namespace) -> None:
    from deal_hunter.db import repository as repo

    query = " ".join(args.model.lower().split())
    with repo.connect() as conn:
        prices = _estimates(conn)
        models = [m for m in prices if m == query] or sorted(m for m in prices if m.startswith(query))
        if not models:
            print(f"No fair price for {query!r} yet (fewer than 4 priced ads in 60 days, or no such model).")
        for model in models:
            fp = prices[model]
            print(
                f"{model} ({fp.category}): fair ~{_kc(fp.median)} Kč, IQR {_kc(fp.q25)}-{_kc(fp.q75)}, "
                f"n={fp.n} of {fp.n_listings} ads ({fp.sold} gone fast), confidence {fp.confidence:.2f} ({fp.level})"
            )
            if fp.slopes:
                ref = (
                    ", ".join(f"{k.split('_')[0].upper()} {v} GB" for k, v in fp.ref.items())
                    or "unknown spec"
                )
                slopes = ", ".join(f"{k.split('_')[0].upper()} {v:+.0%}" for k, v in fp.slopes.items())
                print(f"  quoted at {ref}; per doubling: {slopes}")
            for lid, price, kind, title, first, gone, feats, url in repo.model_listings(
                conn, model, args.ads
            ):
                state = f"gone {gone:%m-%d}" if gone else "active"
                spec = " ".join(f"{v}{k[0]}" for k, v in feats.items() if k in ("ram_gb", "ssd_gb"))
                print(
                    f"  {lid}  {_kc(price) if price else kind:>8}  {first:%m-%d}  {state:<10} {spec:<10} {title[:60]}"
                )


def cmd_prices(args: argparse.Namespace) -> None:
    from deal_hunter.db import repository as repo
    from deal_hunter.pricing import LEVEL_RANK

    with repo.connect() as conn:
        prices = _estimates(conn)
    rows = [
        p
        for p in prices.values()
        if (args.category is None or p.category.startswith(args.category))
        and LEVEL_RANK[p.level] >= LEVEL_RANK[args.min_level]
    ]
    rows.sort(key=lambda p: (p.category, -p.n, p.model))
    print(f"{'model':<28} {'category':<13} {'n':>4} {'median':>8} {'IQR':>15} {'conf':>5}  level")
    for p in rows:
        iqr = f"{_kc(p.q25)}-{_kc(p.q75)}"
        print(
            f"{p.model:<28} {p.category:<13} {p.n:>4} {_kc(p.median):>8} {iqr:>15} {p.confidence:>5.2f}  {p.level}"
        )
    print(f"\n{len(rows)} models (of {len(prices)} with an estimate). Kč, asking prices of the last 60 days.")


def cmd_work(args: argparse.Namespace) -> None:
    from pathlib import Path

    from deal_hunter import alerts
    from deal_hunter.db import repository as repo

    settings = load_settings()
    rules = _rules(settings)

    def step():
        with repo.connect(autocommit=True) as conn:
            repo.migrate(conn)
            stats = alerts.work_once(
                conn,
                rules,
                _llm(settings),
                Path(settings.llm_cache),
                _ntfy(settings),
                settings.triage_per_cycle,
            )
        print(stats.summary(), flush=True)

    if args.loop:
        scraper.forever(settings.work_minutes * 60, step, "work")
    else:
        step()


def cmd_deals(args: argparse.Namespace) -> None:
    from deal_hunter.db import repository as repo

    states = (
        args.state.split(",")
        if args.state
        else [
            "triage",
            "dropped",
            "low_score",
            "would_alert",
            "queued",
            "alerted",
            "capped",
            "duplicate",
            "expired",
        ]
    )
    with repo.connect() as conn:
        deals = repo.deals_in_state(conn, *states)
    deals.sort(key=lambda d: d["first_flagged"], reverse=True)
    for d in deals[: args.limit]:
        score = f"{d['score']:.3f}" if d["score"] is not None else "  -  "
        print(
            f"{d['listing_id']}  {d['state']:<11} {score}  {_kc(d['price']):>7} vs {_kc(d['fair']):>7} "
            f"(-{d['discount']:.0%})  {d['model']:<24} {d['title'][:50]}"
        )
        if d["verdict"] or d["reason"]:
            print(f"{'':>12}{d['verdict'] or ''}{'  [' + d['reason'] + ']' if d['reason'] else ''}")
    print(f"\n{len(deals)} deals in {', '.join(states)}")


def cmd_gate(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime

    from deal_hunter.alerts import gate_status
    from deal_hunter.db import repository as repo

    settings = load_settings()
    rules = _rules(settings)
    with repo.connect() as conn:
        prices = _estimates(conn)
        status = gate_status(prices, repo.data_since(conn), rules, datetime.now(UTC))
    print(
        f"Alerts {'ENABLED' if rules.enabled else 'disabled (HUNTER_ALERTS_ENABLED)'}; a model needs "
        f"n >= {rules.min_samples} and >= {rules.min_days:g} days of data in its category."
    )
    print("Days of data: " + ", ".join(f"{c} {d:g}" for c, d in status["days"].items()))
    print(f"Ready ({len(status['ready'])}): {', '.join(status['ready']) or '-'}")
    for model, why in list(status["waiting"].items())[: args.limit]:
        print(f"  waiting  {model:<28} {why}")


def cmd_triage(args: argparse.Namespace) -> None:
    """Run the LLM on given ads and print the answer (nothing is stored)."""
    import json
    from pathlib import Path

    from deal_hunter import triage
    from deal_hunter.db import repository as repo

    settings = load_settings()
    client = _llm(settings)
    if client is None:
        raise SystemExit("HUNTER_LLM_MODEL is empty")
    with repo.connect() as conn:
        prices = _estimates(conn)
        for listing_id in args.ids:
            row = conn.execute(
                "SELECT l.title, l.price_czk, l.location, l.psc, l.description, n.model, n.category, n.features "
                "FROM listings l LEFT JOIN normalized n ON n.listing_id = l.id WHERE l.id = %s",
                (listing_id,),
            ).fetchone()
            if row is None:
                print(f"{listing_id}: not found")
                continue
            title, price, location, psc, description, model, category, features = row
            fp = prices.get(model)
            fair = fp.for_listing(features) if fp else price
            deal = {
                "title": title,
                "price": price or 0,
                "fair": fair or 0,
                "location": location,
                "psc": psc,
                "description": description,
                "model": model or "unknown",
                "category": category or "gpu",
            }
            outcome = triage.triage(client, deal, Path(settings.llm_cache))
            value, factor, reason = triage.score(
                max(0.0, 1 - (price or 0) / fair) if fair else 0, fp.confidence if fp else 0, outcome.result
            )
            print(
                f"{listing_id}  {title}\n  {_kc(price)} Kč vs fair {_kc(fair)} ({model}); {outcome.status}, "
                f"{outcome.stats.get('seconds', 0)} s{' (cached)' if outcome.stats.get('cached') else ''}"
            )
            print("  " + json.dumps(outcome.result.as_dict(), ensure_ascii=False))
            print(f"  score {value:.3f} (penalty x{factor}){' DROP: ' + reason if reason else ''}")


def cmd_notify_test(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime

    from deal_hunter.alerts import gate_status
    from deal_hunter.config import VERSION
    from deal_hunter.db import repository as repo

    settings = load_settings()
    ntfy = _ntfy(settings, required=True)
    rules = _rules(settings)
    try:
        with repo.connect() as conn:
            status = gate_status(_estimates(conn), repo.data_since(conn), rules, datetime.now(UTC))
        state = f"{len(status['ready'])} models alert-ready; data: " + ", ".join(
            f"{c} {d:g} d" for c, d in status["days"].items()
        )
    except SystemExit:
        state = "no database here"
    message = (
        f"Test push from deal-hunter {VERSION}. Alerts {'on' if rules.enabled else 'off'}; {state}.\n"
        "Tap 👍 or 👎 to test the feedback path (stored as a vote for ad 0)."
    )
    msg_id = ntfy.publish(
        "deal-hunter test",
        message,
        priority=3,
        tags=["test_tube"],
        click="https://github.com/michalomegalul/deal-hunter",
        actions=ntfy.vote_actions(0),
    )
    print(f"Sent to {settings.ntfy_url}/{settings.ntfy_topic} (id {msg_id})")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hunt", description="Bazoš deal hunter (GPUs, ex-office PCs/laptops)"
    )
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
    p.add_argument("--section", default=CATEGORY, choices=sorted(SECTIONS), help="default graficka")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("recheck", help="re-fetch active ads not seen lately; mark removed ones gone")
    p.add_argument("--loop", action="store_true", help="service: repeat every HUNTER_RECHECK_HOURS")
    p.add_argument("--older-than", type=float, default=None, help="hours since last seen (default 6)")
    p.add_argument("--limit", type=int, default=None, help="max ads per run (default 1500)")
    p.set_defaults(func=cmd_recheck)

    p = sub.add_parser("stats", help="what has been collected so far")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("normalize", help="map listing titles to canonical models (GPUs, office PCs/laptops)")
    p.add_argument("--all", action="store_true", help="redo every listing, not just new/outdated ones")
    p.add_argument("-v", "--verbose", action="store_true", help="print counts per model")
    p.set_defaults(func=cmd_normalize)

    p = sub.add_parser("review", help="titles the normalizer couldn't place")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--urls", action="store_true")
    p.add_argument("--section", choices=sorted(SECTIONS), default=None)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser(
        "price", help="fair price of one model (prefix match: 'rtx 3080' shows both VRAM sizes)"
    )
    p.add_argument("--model", required=True)
    p.add_argument("--ads", type=int, default=8, help="recent ads to list")
    p.set_defaults(func=cmd_price)

    p = sub.add_parser("prices", help="fair price table: model, n, median, IQR, confidence")
    p.add_argument("--category", choices=["gpu", "office", "office_pc", "office_laptop"], default=None)
    p.add_argument("--min-level", choices=["low", "medium", "high"], default="low")
    p.set_defaults(func=cmd_prices)

    p = sub.add_parser("work", help="price, find candidates, LLM triage, score, push, read feedback")
    p.add_argument("--loop", action="store_true", help="service: a cycle every HUNTER_WORK_MINUTES")
    p.set_defaults(func=cmd_work)

    p = sub.add_parser("deals", help="candidate deals and what happened to them")
    p.add_argument("--state", default=None, help="comma-separated, e.g. would_alert,alerted")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_deals)

    p = sub.add_parser("gate", help="which models are allowed to alert yet")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("triage", help="run the LLM on some ads and print its answer (not stored)")
    p.add_argument("ids", type=int, nargs="+")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("notify-test", help="send one test push to the deals topic")
    p.set_defaults(func=cmd_notify_test)

    register_db(sub)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)
