"""The worker and what it produces: work, deals, gate, triage, feedback, notify-test, web, watchdog."""

import argparse
from datetime import UTC, datetime

from deal_hunter import scraper
from deal_hunter.cli.common import (
    load_estimates,
    make_auction_http,
    make_auction_rules,
    make_llm,
    make_new_price_http,
    ntfy_client,
    num,
)
from deal_hunter.config import load_settings


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


def cmd_work(args: argparse.Namespace) -> None:
    from pathlib import Path

    from deal_hunter import alerts
    from deal_hunter.db import repository as repo

    settings = load_settings()
    rules = _rules(settings)

    def step():
        from deal_hunter import auction_cycle, datart, watchlist, zbozi

        llm, ntfy, cache = make_llm(settings), ntfy_client(settings), Path(settings.llm_cache)
        with repo.connect(autocommit=True) as conn, repo.connect(autocommit=True) as throttle_conn:
            repo.migrate(conn)
            with (
                make_auction_http(settings, throttle_conn) as http,
                make_new_price_http(settings, throttle_conn) as zbozi_http,
            ):
                auction_rules = make_auction_rules(settings)

                def auction_step(conn, prices, quiet, now):
                    return auction_cycle.tick(conn, http, llm, cache, ntfy, prices, auction_rules, quiet, now)

                def new_price_step(conn):
                    return zbozi.fetch_due(
                        zbozi_http, conn, settings.new_price_days, settings.new_price_per_cycle
                    ) + datart.fetch_due(zbozi_http, conn, settings.new_price_days, settings.datart_pages)

                def watch_step(conn, now):
                    return watchlist.tick(
                        conn,
                        llm,
                        cache,
                        ntfy,
                        now,
                        settings.watch_scan_hours,
                        rules.quiet_start,
                        rules.quiet_end,
                        settings.watch_max_per_day,
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
                    watch_step=watch_step,
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
            f"{d['listing_id']}  {d['state']:<11} {score}  {num(d['price']):>7} vs {num(d['fair']):>7} "
            f"(-{d['discount']:.0%})  {d['model']:<24} {d['title'][:50]}"
        )
        if d["verdict"] or d["reason"]:
            print(f"{'':>12}{d['verdict'] or ''}{'  [' + d['reason'] + ']' if d['reason'] else ''}")
    print(f"\n{len(deals)} deals in {', '.join(states)}")


def cmd_gate(args: argparse.Namespace) -> None:

    from deal_hunter.alerts import gate_status
    from deal_hunter.db import repository as repo

    settings = load_settings()
    rules = _rules(settings)
    with repo.connect() as conn:
        prices = load_estimates(conn)
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
    client = make_llm(settings)
    if client is None:
        raise SystemExit("HUNTER_LLM_MODEL is empty")
    with repo.connect() as conn:
        prices = load_estimates(conn)
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
                f"{listing_id}  {title}\n  {num(price)} Kč vs fair {num(fair)} ({model}); {outcome.status}, "
                f"{outcome.stats.get('seconds', 0)} s{' (cached)' if outcome.stats.get('cached') else ''}"
            )
            print("  " + json.dumps(outcome.result.as_dict(), ensure_ascii=False))
            print(f"  score {value:.3f} (penalty x{factor}){' DROP: ' + reason if reason else ''}")


def _feedback_context(settings, conn, throttle_conn, bazos):
    from pathlib import Path

    from deal_hunter.feedback import Context

    return Context(
        conn=conn,
        ntfy=ntfy_client(settings, required=True),
        bazos=bazos,
        llm=make_llm(settings),
        cache_dir=Path(settings.llm_cache),
        confident_n=settings.alert_min_samples,
        usd_czk=settings.usd_czk,
        offer_discount=settings.offer_discount,
        auction_costs=make_auction_rules(settings).costs,
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

    from deal_hunter.alerts import gate_status
    from deal_hunter.config import VERSION
    from deal_hunter.db import repository as repo

    settings = load_settings()
    ntfy = ntfy_client(settings, required=True)
    rules = _rules(settings)
    try:
        with repo.connect() as conn:
            status = gate_status(load_estimates(conn), repo.data_since(conn), rules, datetime.now(UTC))
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
    ntfy = ntfy_client(settings, required=True)
    with repo.connect() as conn:
        row = repo.listing_for_followup(conn, listing_id)
        if row is None:
            raise SystemExit(f"No ad {listing_id} in the database")
        prices = load_estimates(conn)
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


def cmd_web(args: argparse.Namespace) -> None:
    from deal_hunter import web

    web.serve(args.host, args.port)


def cmd_watchdog(args: argparse.Namespace) -> None:
    from deal_hunter import health
    from deal_hunter.db import repository as repo

    settings = load_settings()
    with repo.connect(autocommit=True) as conn:
        found = health.run(conn, ntfy_client(settings), sources=settings.sources)
    print("\n".join(found.values()) if found else "watchdog: all good", flush=True)


def register(sub: argparse._SubParsersAction) -> None:
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

    p = sub.add_parser("web", help="serve the read-only LAN website (deals, prices, auctions)")
    p.add_argument("--host", default="0.0.0.0", help="default 0.0.0.0 (LAN)")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser(
        "watchdog", help="push an alert if a background job is stale or failing (run from a timer)"
    )
    p.set_defaults(func=cmd_watchdog)

    p = sub.add_parser("notify-test", help="send one test push to the deals topic")
    p.add_argument(
        "--ad", type=int, default=None, help="push this real ad (marked TEST) with photo and 👍/👎"
    )
    p.set_defaults(func=cmd_notify_test)
