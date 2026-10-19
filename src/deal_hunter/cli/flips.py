"""The flips log and the alert evaluation: flip ..., eval."""

import argparse
from datetime import UTC, datetime, timedelta

from deal_hunter.cli.common import num


def _price(text: str) -> int:
    """argparse type: a whole, non-negative amount in Kč."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number of Kč: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {value}")
    return value


def _day(text: str) -> datetime:
    """argparse type: YYYY-MM-DD, as noon in Prague (so the date survives any timezone display)."""
    from deal_hunter.alerts import PRAGUE

    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(hour=12, tzinfo=PRAGUE)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text!r}") from None


def _update_flip(listing_id: int, **fields) -> None:
    """Apply the given fields to a flip, or exit with a message if the ad has no flip."""
    from deal_hunter.db import repository as repo

    fields = {k: v for k, v in fields.items() if v is not None}
    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        if not repo.update_flip(conn, listing_id, **fields):
            raise SystemExit(f"No flip for ad {listing_id}. See `hunt flip list`, or `hunt flip add`.")
    print(f"Updated flip {listing_id}: " + ", ".join(f"{k}={v}" for k, v in fields.items()))


def cmd_flip_list(args: argparse.Namespace) -> None:
    from deal_hunter import evaluation
    from deal_hunter.db import repository as repo

    status = "open" if args.open else "sold" if args.sold else None
    now = datetime.now(UTC)
    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        flips = repo.list_flips(conn, status)
    for f in flips:
        profit = evaluation.flip_profit(f)
        print(
            f"{f['listing_id']:>12}  {num(f['buy_price']):>8} -> {num(f['sold_price']):>8}  "
            f"fees {num(f['fees']):>6}  profit {num(profit):>8}  {evaluation.days_held(f, now):>3} d  "
            f"{(f['title'] or '-')[:40]}"
        )
    totals = evaluation.flip_totals(flips)
    roi = f"{totals['roi']:.0%}" if totals["roi"] is not None else "-"
    print(
        f"\n{len(flips)} flip(s): {totals['open']} open, {totals['sold']} sold. "
        f"Invested in open flips {num(totals['invested'])} Kč; realised profit {num(totals['profit'])} Kč "
        f"on {num(totals['cost'])} Kč spent, ROI {roi}"
    )


def cmd_flip_add(args: argparse.Namespace) -> None:
    from deal_hunter.db import repository as repo

    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        if not repo.add_flip(conn, args.ad, args.price, args.at or datetime.now(UTC), args.notes):
            raise SystemExit(f"Ad {args.ad} is already in the flips log. Use `hunt flip buy-price`.")
    print(f"Logged ad {args.ad} as bought for {num(args.price)} Kč")


def cmd_flip_buy_price(args: argparse.Namespace) -> None:
    _update_flip(args.listing_id, buy_price=args.price, notes=args.notes)


def cmd_flip_sell(args: argparse.Namespace) -> None:
    _update_flip(
        args.listing_id,
        sold_price=args.price,
        sold_at=args.at or datetime.now(UTC),
        fees=args.fees,
        notes=args.notes,
    )


def cmd_flip_fees(args: argparse.Namespace) -> None:
    _update_flip(args.listing_id, fees=args.price, notes=args.notes)


def cmd_eval(args: argparse.Namespace) -> None:
    from deal_hunter import evaluation
    from deal_hunter.db import repository as repo

    now = datetime.now(UTC)
    since = now - timedelta(days=args.days)
    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        deals = repo.eval_deals(conn, since)
        flips = [f for f in repo.list_flips(conn) if f["bought_at"] >= since]
    print(f"Alert evaluation, deals flagged in the last {args.days} days ({len(deals)} deals)\n")
    print(evaluation.format_text(evaluation.eval_tables(deals, flips, now)))


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("flip", help="the flips log: what you bought, sold and made")
    fsub = p.add_subparsers(required=True)
    f = fsub.add_parser("list", help="flips with buy, sell, fees, profit and days held, plus totals")
    grp = f.add_mutually_exclusive_group()
    grp.add_argument("--open", action="store_true", help="only flips not sold yet")
    grp.add_argument("--sold", action="store_true", help="only sold flips")
    f.set_defaults(func=cmd_flip_list)
    f = fsub.add_parser("add", help="log an item bought outside the 👍 flow")
    f.add_argument("--ad", type=int, required=True, help="listing id")
    f.add_argument("--price", type=_price, required=True, help="buy price, Kč")
    f.add_argument("--at", type=_day, default=None, help="purchase date YYYY-MM-DD (default now)")
    f.add_argument("--notes")
    f.set_defaults(func=cmd_flip_add)
    f = fsub.add_parser("buy-price", help="set the haggled buy price")
    f.add_argument("listing_id", type=int)
    f.add_argument("price", type=_price, help="Kč")
    f.add_argument("--notes")
    f.set_defaults(func=cmd_flip_buy_price)
    f = fsub.add_parser("sell", help="record a sale")
    f.add_argument("listing_id", type=int)
    f.add_argument("price", type=_price, help="sold for, Kč")
    f.add_argument("--fees", type=_price, default=None, help="postage, cleaning, parts; Kč")
    f.add_argument("--at", type=_day, default=None, help="sale date YYYY-MM-DD (default now)")
    f.add_argument("--notes")
    f.set_defaults(func=cmd_flip_sell)
    f = fsub.add_parser("fees", help="set the fees of a flip (postage, cleaning, parts)")
    f.add_argument("listing_id", type=int)
    f.add_argument("price", type=_price, help="Kč")
    f.add_argument("--notes")
    f.set_defaults(func=cmd_flip_fees)

    p = sub.add_parser(
        "eval", help="measure the alert pipeline: states, score bands, votes, how fast ads vanish"
    )
    p.add_argument("--days", type=int, default=90, help="deals flagged in the last N days (default 90)")
    p.set_defaults(func=cmd_eval)
