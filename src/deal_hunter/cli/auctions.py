"""State IT auctions (nms.gov.cz): auctions scrape | list | value."""

import argparse
from datetime import UTC, datetime

from deal_hunter.cli.common import load_estimates, make_auction_http, make_auction_rules, make_llm, num
from deal_hunter.config import load_settings


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
    from pathlib import Path

    from deal_hunter import auction_cycle, nms
    from deal_hunter.db import repository as repo

    settings = load_settings()
    if args.dry_run:
        with make_auction_http(settings) as http:
            resp = http.post_json(nms.LIST_API, nms.list_body("active"))
        lots = nms.parse_list(auction_cycle.resp_json(resp))
        for lot in lots:
            print(
                f"{lot.lot_id}  {lot.status:<7} {num(lot.price):>9}  {lot.bids or 0:>2} bids  {_end(lot.end_at)}  {lot.title}"
            )
        print(f"\n{len(lots)} running IT lots on nms.gov.cz (dry run, nothing saved)")
        return
    rules = make_auction_rules(settings)
    llm = make_llm(settings)
    with repo.connect(autocommit=True) as conn, repo.connect(autocommit=True) as throttle_conn:
        repo.migrate(conn)
        with make_auction_http(settings, throttle_conn) as http:
            stats = auction_cycle.AuctionStats()
            now = datetime.now(UTC)
            auction_cycle.scrape(http, conn, rules, stats, now, force=True)
            if llm is not None:
                auction_cycle.run_extraction(
                    conn, llm, Path(settings.llm_cache), _with_extract(rules, args.extract), stats
                )
            auction_cycle.value_running(conn, load_estimates(conn), rules, stats)
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
    costs = make_auction_rules(settings).costs
    with repo.connect() as conn:
        prices = load_estimates(conn)
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
    costs = make_auction_rules(settings).costs
    with repo.connect() as conn:
        lot = repo.get_lot(conn, auction_cycle.SOURCE, str(args.id))
        if lot is None:
            raise SystemExit(f"No lot {args.id} (run `hunt auctions scrape` first)")
        x = auction_cycle.extraction_for(conn, lot)
        v = auction_cycle.value(lot, x, load_estimates(conn), costs) if x else None
    _print_valuation(lot, x, v)


def register(sub: argparse._SubParsersAction) -> None:
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
