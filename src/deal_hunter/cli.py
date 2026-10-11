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
        "Add rules to gpu_catalog.yaml (GPUs), office.py (PCs, laptops) or storage_catalog.yaml (disks)."
    )


# --- pricing, triage, alerts ------------------------------------------------------------------------


def _kc(n) -> str:
    return f"{n:,}".replace(",", " ") if n is not None else "-"


def _distance(settings):
    from deal_hunter.geo import DistanceRules

    if settings.pickup_km <= 0:
        return None
    return DistanceRules(
        home=settings.home,
        pickup_km=settings.pickup_km,
        pickup_km_by_category=settings.pickup_km_by_category,
        shipping_czk=settings.shipping_czk,
        travel_czk_per_km=settings.travel_czk_per_km,
    )


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
        usd_czk=settings.usd_czk,
        distance=_distance(settings),
        hdd_min_tb=settings.hdd_min_tb,
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


def _estimates(conn, priors: bool = True):
    """Fair prices as the worker sees them (with new-price/auction priors), or the raw medians."""
    from deal_hunter import pricing
    from deal_hunter.db import repository as repo

    if not priors:
        return pricing.estimate_all(repo.price_samples(conn, pricing.WINDOW_DAYS))
    settings = load_settings()
    return pricing.run(conn, settings.alert_min_samples, settings.usd_czk, save=False)


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
            unit = "Kč/TB" if fp.per_tb else "Kč"
            print(
                f"{model} ({fp.category}): fair ~{_kc(fp.median)} {unit}, IQR {_kc(fp.q25)}-{_kc(fp.q75)} {unit}, "
                f"n={fp.n} of {fp.n_listings} ads ({fp.sold} gone fast), confidence {fp.confidence:.2f} ({fp.level})"
            )
            if fp.category == "hdd" and not fp.per_tb:
                print(f"  {_per_tb(fp)}")
            if fp.new_price:
                from deal_hunter.alerts import new_text

                print(
                    f"  {new_text(fp)}"
                    + ("  ** used >= 95 % of new: candidates dropped **" if fp.over_new else "")
                )
            if fp.prior:
                suffix = "/TB" if fp.per_tb else ""
                print(
                    f"  raw median {_kc(fp.raw_median)}{suffix} shrunk toward the {fp.prior_source} prior "
                    f"{_kc(fp.prior)}{suffix} (weight {fp.prior_weight:g} samples; the gate counts n={fp.n_eff:g})"
                )
            elif fp.prior_note:
                print(f"  {fp.prior_note}")
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


def _per_tb(fp) -> str:
    """'812 Kč/TB (new 1 036 Kč/TB)' for a capacity-specific disk key ('hdd35 enterprise sas 4tb').

    A pooled class (fp.per_tb) is already quoted in Kč/TB; callers skip this for those."""
    import re

    m = re.search(r"(\d+(?:\.\d+)?)(tb|gb)$", fp.model)
    if m is None:
        return ""
    tb = float(m.group(1)) / (1000 if m.group(2) == "gb" else 1)
    text = f"{_kc(round(fp.median / tb))} Kč/TB"
    if fp.new_price and fp.new_source == "zbozi":
        text += f" (new {_kc(round(fp.new_price / tb))} Kč/TB)"
    return text


def cmd_prices(args: argparse.Namespace) -> None:
    from deal_hunter.db import repository as repo
    from deal_hunter.pricing import LEVEL_RANK

    with repo.connect() as conn:
        prices = _estimates(conn, priors=not args.no_prior)
    rows = [
        p
        for p in prices.values()
        if (args.category is None or p.category.startswith(args.category))
        and LEVEL_RANK[p.level] >= LEVEL_RANK[args.min_level]
    ]
    rows.sort(key=lambda p: (p.category, -p.n, p.model))
    print(
        f"{'model':<28} {'category':<13} {'n':>4} {'median':>8} {'IQR':>15} {'conf':>5}  {'level':<6} "
        f"{'new':>8}  {'prior':>13}"
    )
    for p in rows:
        iqr = f"{_kc(p.q25)}-{_kc(p.q75)}"
        new = (_kc(p.new_price) + ("" if p.new_source == "zbozi" else "*")) if p.new_price else "-"
        prior = f"{p.prior_source} {_kc(p.prior)}" if p.prior else ""
        flags = []
        if p.raw_median and p.raw_median != p.median:
            flags.append(f"raw {_kc(p.raw_median)}")
        if p.over_new:
            flags.append("USED>=95% NEW")
        if p.prior_note:
            flags.append(p.prior_note)
        if p.category == "hdd":
            flags.insert(0, f"{_kc(p.median)} Kč/TB" if p.per_tb else _per_tb(p))
        print(
            f"{p.model:<28} {p.category:<13} {p.n:>4} {_kc(p.median):>8} {iqr:>15} {p.confidence:>5.2f}  {p.level:<6} "
            f"{new:>8}  {prior:>13}  {'; '.join(flags)}"
        )
    print(
        f"\n{len(rows)} models (of {len(prices)} with an estimate). Kč, asking prices of the last 60 days"
        + (
            ", raw medians (no priors)."
            if args.no_prior
            else "; new = Zboží lowest offer, * = launch MSRP + VAT."
        )
        + " hdd rows for a class (hdd35 nas/enterprise/desktop/surveillance, external) show Kč/TB, not"
        " a total price."
    )


def cmd_newprices(args: argparse.Namespace) -> None:
    """The curated Zboží.cz product pages and their latest lowest offers (--fetch reads the due ones)."""
    from deal_hunter import zbozi
    from deal_hunter.db import repository as repo

    settings = load_settings()
    with repo.connect(autocommit=True) as conn, repo.connect(autocommit=True) as throttle_conn:
        repo.migrate(conn)
        if args.fetch:
            with _new_price_http(settings, throttle_conn) as http:
                days = 0 if args.all else settings.new_price_days
                print(zbozi.fetch_due(http, conn, days, args.limit).summary())
        rows = repo.new_price_rows(conn)
        best = repo.latest_new_prices(conn)
    pages = zbozi.pages()
    print(f"{len(pages)} models, {sum(len(u) for u in pages.values())} product pages in new_price_pages.yaml")
    for r in rows:
        mark = "*" if best.get(r["model"], (None,))[0] == r["min_price"] and r["min_price"] else " "
        what = (
            f"{_kc(r['min_price']):>8} Kč {r['shops'] or 0:>2} shops"
            if r["min_price"]
            else (r["error"] or "no offers")[:40]
        )
        print(
            f"{mark} {r['model']:<20} {what:<24} {r['fetched_at']:%Y-%m-%d}  {(r['product'] or r['url'])[:60]}"
        )
    print("\n* = the model's new price (lowest offer across its pages, fetched in the last 30 days)")


def _new_price_http(settings, throttle_conn=None):
    """The polite client for Zboží.cz: >= 6 s apart, shared across processes via http_throttle row 3."""
    from deal_hunter.config import NEW_PRICE_USER_AGENT, USER_AGENT
    from deal_hunter.http import PoliteClient

    throttle = (
        scraper.DbThrottle(throttle_conn, settings.new_price_min_delay, row=3)
        if throttle_conn is not None
        else None
    )
    agent = settings.user_agent if settings.user_agent != USER_AGENT else NEW_PRICE_USER_AGENT
    return PoliteClient(agent, min_delay=settings.new_price_min_delay, throttle=throttle, timeout=30)


def cmd_work(args: argparse.Namespace) -> None:
    from pathlib import Path

    from deal_hunter import alerts
    from deal_hunter.db import repository as repo

    settings = load_settings()
    rules = _rules(settings)

    def step():
        from deal_hunter import auction_cycle, zbozi

        llm, ntfy, cache = _llm(settings), _ntfy(settings), Path(settings.llm_cache)
        with repo.connect(autocommit=True) as conn, repo.connect(autocommit=True) as throttle_conn:
            repo.migrate(conn)
            with (
                _auction_http(settings, throttle_conn) as http,
                _new_price_http(settings, throttle_conn) as zbozi_http,
            ):
                auction_rules = _auction_rules(settings)

                def auction_step(conn, prices, quiet, now):
                    return auction_cycle.tick(conn, http, llm, cache, ntfy, prices, auction_rules, quiet, now)

                def new_price_step(conn):
                    return zbozi.fetch_due(
                        zbozi_http, conn, settings.new_price_days, settings.new_price_per_cycle
                    )

                stats = alerts.work_once(
                    conn,
                    rules,
                    llm,
                    cache,
                    ntfy,
                    settings.triage_per_cycle,
                    auction_step=auction_step if settings.auctions_enabled else None,
                    new_price_step=new_price_step if settings.new_prices_enabled else None,
                )
        print(stats.summary(), flush=True)

    if args.loop:
        scraper.forever(settings.work_minutes * 60, step, "work")
    else:
        step()


# --- auctions (Phase 7) ---------------------------------------------------------------------------------


def _auction_rules(settings):
    from deal_hunter.auction_cycle import AuctionRules
    from deal_hunter.auctions import Costs

    return AuctionRules(
        enabled=settings.auctions_enabled,
        alerts=settings.alerts_enabled,
        list_hours=settings.auction_list_hours,
        details_per_cycle=settings.auction_details_per_cycle,
        extract_per_cycle=settings.auction_extract_per_cycle,
        max_per_day=settings.auction_max_per_day,
        costs=Costs(
            hourly_rate=settings.auction_hourly_rate,
            minutes_per_unit=settings.auction_minutes_per_unit,
            resale_factor=settings.auction_resale_factor,
            sell_per_week=settings.auction_sell_per_week,
            lot_cost=settings.auction_lot_cost,
            lot_hours=settings.auction_lot_hours,
            target_margin=settings.auction_target_margin,
            min_profit=settings.auction_min_profit,
        ),
    )


def _auction_http(settings, throttle_conn=None):
    """The polite client for nms.gov.cz: >= 5 s apart, shared across processes via http_throttle row 2."""
    from deal_hunter.config import AUCTION_USER_AGENT, USER_AGENT
    from deal_hunter.http import PoliteClient

    throttle = (
        scraper.DbThrottle(throttle_conn, settings.auction_min_delay, row=2)
        if throttle_conn is not None
        else None
    )
    agent = settings.user_agent if settings.user_agent != USER_AGENT else AUCTION_USER_AGENT
    return PoliteClient(agent, min_delay=settings.auction_min_delay, throttle=throttle, timeout=40)


def _end(dt) -> str:
    from deal_hunter.alerts import PRAGUE

    return f"{dt.astimezone(PRAGUE):%a %d.%m. %H:%M}" if dt else "-"


def _print_valuation(lot: dict, x, v) -> None:
    from deal_hunter.auctions import kc

    print(f"{lot['lot_id']}  {lot['title']}\n  {lot['url']}")
    print(
        f"  {lot['status']}, ends {_end(lot['end_at'])}, now {kc(lot.get('current_price') or lot.get('start_price'))}"
        f" ({lot.get('bids') or 0} bid(s), +{kc(lot.get('min_increment'))}), {lot.get('institution') or ''}"
    )
    if x is None:
        print("  (not extracted yet)")
        return
    for item in x.items:
        flags = ", ".join(item.missing) or "complete"
        spec = " ".join(
            f"{k} {getattr(item, k)}" for k in ("ram_gb", "storage_gb", "cpu") if getattr(item, k)
        )
        print(
            f"  {item.quantity:>4}× {item.model:<34} -> {item.key or '(no pricing key)':<24} {item.condition:<13}"
            f" missing: {flags}{'  ' + spec if spec else ''}{'' if item.verified else '  [UNVERIFIED quote]'}"
        )
    extras = [
        f"VAT deductible: {'yes' if x.vat_deductible else 'not stated'}",
        f"fees: {x.fee_percent:g} % + {kc(x.fee_czk)}"
        if (x.fee_percent or x.fee_czk)
        else "fees: none stated",
    ]
    if x.pickup:
        extras.append(f"pickup: {x.pickup}")
    if x.inspection:
        extras.append(f"inspection: {x.inspection}")
    print("  " + "; ".join(extras))
    if x.notes:
        print(f"  notes: {x.notes}")
    if v is None:
        return
    for line in v.lines:
        if line.fair:
            print(
                f"    {line.quantity:>4}× {line.key:<24} fair {kc(line.fair)} ({line.level}, n={line.n}) -> resale "
                f"{kc(line.resale)}, parts {kc(line.parts)}/unit = {kc(line.revenue)}"
            )
        else:
            print(f"    {line.quantity:>4}× {line.model:<24} no fair price (not counted)")
    print(
        f"  revenue {kc(v.revenue)} (after ~{v.weeks_to_sell:g} weeks of selling), parts {kc(v.parts)}, "
        f"labour {v.hours:g} h = {kc(v.labour)}, transport/fees {kc(v.fixed)}"
        + (f" + {v.fee_percent:g} % premium" if v.fee_percent else "")
    )
    print(
        f"  max bid {kc(v.max_bid)}; at the next bid {kc(v.next_bid)}: profit {kc(v.profit)}, "
        f"{kc(v.profit_per_hour)}/h; {v.confidence} confidence -> "
        + ("QUALIFIES" if v.qualifies else "no: " + "; ".join(v.reasons))
    )


def cmd_auctions_scrape(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from deal_hunter import auction_cycle, nms
    from deal_hunter.db import repository as repo

    settings = load_settings()
    if args.dry_run:
        with _auction_http(settings) as http:
            resp = http.post_json(nms.LIST_API, nms.list_body("active"))
        lots = nms.parse_list(auction_cycle.resp_json(resp))
        for lot in lots:
            print(
                f"{lot.lot_id}  {lot.status:<7} {_kc(lot.price):>9}  {lot.bids or 0:>2} bids  {_end(lot.end_at)}  {lot.title}"
            )
        print(f"\n{len(lots)} running IT lots on nms.gov.cz (dry run, nothing saved)")
        return
    rules = _auction_rules(settings)
    llm = _llm(settings)
    with repo.connect(autocommit=True) as conn, repo.connect(autocommit=True) as throttle_conn:
        repo.migrate(conn)
        with _auction_http(settings, throttle_conn) as http:
            stats = auction_cycle.AuctionStats()
            now = datetime.now(UTC)
            auction_cycle.scrape(http, conn, rules, stats, now, force=True)
            if llm is not None:
                auction_cycle.run_extraction(
                    conn, llm, Path(settings.llm_cache), _with_extract(rules, args.extract), stats
                )
            auction_cycle.value_running(conn, _estimates(conn), rules, stats)
    for err in stats.errors:
        print(f"  error: {err}")
    print(stats.summary())


def _with_extract(rules, n: int | None):
    from dataclasses import replace

    return replace(rules, extract_per_cycle=n) if n else rules


def cmd_auctions_list(args: argparse.Namespace) -> None:
    from deal_hunter import auction_cycle
    from deal_hunter.auctions import kc, summary
    from deal_hunter.db import repository as repo

    settings = load_settings()
    costs = _auction_rules(settings).costs
    with repo.connect() as conn:
        prices = _estimates(conn)
        active = repo.lots_in_status(conn, "active", "prepared")
        print(f"Running IT lots ({len(active)}):")
        for lot in sorted(active, key=lambda r: r["end_at"] or 0):
            x = auction_cycle.extraction_for(conn, lot)
            what = summary(x, lot["title"]) if x else lot["title"] + "  (not extracted yet)"
            now_price = lot.get("current_price") or lot.get("start_price")
            line = f"  {lot['lot_id']}  ends {_end(lot['end_at'])}  now {kc(now_price):>10}  {what[:60]}"
            if x is not None:
                v = auction_cycle.value(lot, x, prices, costs)
                line += (
                    f"\n{'':>9}max bid {kc(v.max_bid)}, profit {kc(v.profit)}, {kc(v.profit_per_hour)}/h, "
                    f"{v.confidence} conf -> " + ("QUALIFIES" if v.qualifies else "; ".join(v.reasons))
                )
            print(line)
        ended = [lot for lot in repo.lots_in_status(conn, "ended", limit=args.limit) if lot["final_price"]]
        print(f"\nSold lots with a final price ({len(ended)}): final price per unit vs our Bazoš fair price")
        for lot in ended:
            x = auction_cycle.extraction_for(conn, lot)
            units = sum(i.quantity for i in x.items) if x and x.items else None
            per_unit = lot["final_price"] / units if units else None
            fair = ""
            if x and x.items:
                top = max(x.items, key=lambda i: i.quantity)
                fp = prices.get(top.key) if top.key else None
                fair = (
                    f"fair {kc(fp.median)} ({fp.level})" if fp else f"{top.key or 'unpriced'}: no fair price"
                )
            print(
                f"  {lot['lot_id']}  {_end(lot['end_at'])[:13]}  {kc(lot['final_price']):>10}"
                f"  {(str(units) + ' u') if units else '? u':>6}  {kc(per_unit) if per_unit else '-':>9}/u  "
                f"{(summary(x, lot['title']) if x else lot['title'])[:44]:<44}  {fair}"
            )


def cmd_auctions_value(args: argparse.Namespace) -> None:
    from deal_hunter import auction_cycle
    from deal_hunter.db import repository as repo

    settings = load_settings()
    costs = _auction_rules(settings).costs
    with repo.connect() as conn:
        lot = repo.get_lot(conn, auction_cycle.SOURCE, str(args.id))
        if lot is None:
            raise SystemExit(f"No lot {args.id} (run `hunt auctions scrape` first)")
        x = auction_cycle.extraction_for(conn, lot)
        v = auction_cycle.value(lot, x, _estimates(conn), costs) if x else None
    _print_valuation(lot, x, v)


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


def _feedback_context(settings, conn, throttle_conn, bazos):
    from pathlib import Path

    from deal_hunter.feedback import Context

    return Context(
        conn=conn,
        ntfy=_ntfy(settings, required=True),
        bazos=bazos,
        llm=_llm(settings),
        cache_dir=Path(settings.llm_cache),
        confident_n=settings.alert_min_samples,
        usd_czk=settings.usd_czk,
        offer_discount=settings.offer_discount,
        auction_costs=_auction_rules(settings).costs,
        distance=_distance(settings),
    )


def cmd_feedback(args: argparse.Namespace) -> None:
    """Votes from the deals-feedback topic: live (--loop, the service) or what is cached now (once)."""
    from deal_hunter import feedback
    from deal_hunter.db import repository as repo

    settings = load_settings()
    with repo.connect(autocommit=True) as conn, repo.connect(autocommit=True) as throttle_conn:
        repo.migrate(conn)
        with scraper.make_client(settings, throttle_conn) as bazos:
            ctx = _feedback_context(settings, conn, throttle_conn, bazos)
            if args.loop:
                feedback.forever(ctx)
                return
            since = repo.kv_get(conn, feedback.LAST_ID_KEY) or "all"
            events = ctx.ntfy.poll_feedback(since)
            for event in events:
                if event.get("event") != "message":
                    continue
                print(f"{event['id']}: {feedback.handle(ctx, event)}")
                repo.kv_set(conn, feedback.LAST_ID_KEY, event["id"])
            print(f"{sum(e.get('event') == 'message' for e in events)} messages since {since}")


def cmd_notify_test(args: argparse.Namespace) -> None:
    if args.ad:
        return _notify_test_ad(args.ad)
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


def _notify_test_ad(listing_id: int) -> None:
    """A real ad as a deal push, marked TEST; its 👍 asks for a TEST follow-up (nothing is stored as a label)."""
    from deal_hunter import alerts, geo
    from deal_hunter.db import repository as repo

    settings = load_settings()
    ntfy = _ntfy(settings, required=True)
    with repo.connect() as conn:
        row = repo.listing_for_followup(conn, listing_id)
        if row is None:
            raise SystemExit(f"No ad {listing_id} in the database")
        prices = _estimates(conn)
    fp = prices.get(row["model"]) if row.get("model") else None
    price = row["price_czk"] or 0
    if row.get("category") == "hdd":
        from deal_hunter.storage import unit_price

        price = unit_price(price, row.get("features")) or price  # one drive of a multi-drive ad
    fair = fp.for_listing(row.get("features")) if fp else price
    deal = {
        "title": row["title"],
        "price": price,
        "fair": fair or price,
        "discount": max(0.0, 1 - price / fair) if fair else 0.0,
        "model": row.get("model") or "unknown model",
        "category": row.get("category") or "gpu",
        "verdict": row.get("verdict"),
        "location": row.get("location"),
        "psc": row.get("psc"),
        "distance_km": geo.distance_km(settings.home, row.get("psc"), row.get("location"))[0],
        "ships": geo.shipping_offer(f"{row['title']}\n{row.get('description') or ''}"),
        "features": row.get("features"),
    }
    title, message, tags = alerts.format_alert(deal, fp, _rules(settings))
    msg_id = ntfy.publish(
        "TEST · " + title,
        message + "\n(TEST push of a real ad: tap 👍 to get the detailed follow-up)",
        3,
        tags + ["test_tube"],
        row["url"],
        ntfy.vote_actions(listing_id, test=True),
        attach=alerts.first_image(row),
    )
    print(f"Sent TEST push for ad {listing_id} (id {msg_id}), photo: {alerts.first_image(row) or 'none'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hunt", description="Bazoš deal hunter (GPUs, ex-office PCs/laptops, hard disks)"
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

    p = sub.add_parser("normalize", help="map listing titles to canonical models (GPUs, office, disks)")
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

    p = sub.add_parser("prices", help="fair price table: model, n, median, IQR, confidence, new price, prior")
    p.add_argument("--category", choices=["gpu", "office", "office_pc", "office_laptop", "hdd"], default=None)
    p.add_argument("--min-level", choices=["low", "medium", "high"], default="low")
    p.add_argument(
        "--no-prior", action="store_true", help="raw medians, without the new-price/auction priors"
    )
    p.set_defaults(func=cmd_prices)

    p = sub.add_parser("newprices", help="new prices from the curated Zboží.cz product pages")
    p.add_argument("--fetch", action="store_true", help="read the pages that are due (weekly)")
    p.add_argument("--all", action="store_true", help="with --fetch: read every page now")
    p.add_argument("--limit", type=int, default=200, help="max pages to read")
    p.set_defaults(func=cmd_newprices)

    p = sub.add_parser("feedback", help="handle 👍/👎/bought taps from the deals-feedback topic")
    p.add_argument("--loop", action="store_true", help="service: hold the live stream open")
    p.set_defaults(func=cmd_feedback)

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

    p = sub.add_parser("auctions", help="state auctions of IT lots (nms.gov.cz): scrape, list, value")
    asub = p.add_subparsers(required=True)
    a = asub.add_parser("scrape", help="read the lot lists and due detail pages, extract, value (no pushes)")
    a.add_argument("--once", action="store_true", help="one run and exit (the default)")
    a.add_argument("--dry-run", action="store_true", help="read the running IT lots and print, nothing saved")
    a.add_argument("--extract", type=int, default=None, help="LLM extractions this run (default 3)")
    a.set_defaults(func=cmd_auctions_scrape)
    a = asub.add_parser("list", help="running lots with valuations; sold lots: final price per unit vs fair")
    a.add_argument("--limit", type=int, default=40, help="sold lots to show")
    a.set_defaults(func=cmd_auctions_list)
    a = asub.add_parser("value", help="one lot: items, mapping and the valuation breakdown")
    a.add_argument("id")
    a.set_defaults(func=cmd_auctions_value)

    p = sub.add_parser("notify-test", help="send one test push to the deals topic")
    p.add_argument(
        "--ad", type=int, default=None, help="push this real ad (marked TEST) with photo and 👍/👎"
    )
    p.set_defaults(func=cmd_notify_test)

    register_db(sub)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)
