"""Fair prices and new prices: price, prices, newprices."""

import argparse

from deal_hunter.cli.common import load_estimates, make_new_price_http, num
from deal_hunter.config import load_settings
from deal_hunter.pricing import RETAIL_SOURCES


def cmd_price(args: argparse.Namespace) -> None:
    from deal_hunter.db import repository as repo

    query = " ".join(args.model.lower().split())
    with repo.connect() as conn:
        prices = load_estimates(conn)
        models = [m for m in prices if m == query] or sorted(m for m in prices if m.startswith(query))
        if not models:
            print(f"No fair price for {query!r} yet (fewer than 4 priced ads in 60 days, or no such model).")
        for model in models:
            fp = prices[model]
            unit = "Kč/TB" if fp.per_tb else "Kč"
            print(
                f"{model} ({fp.category}): fair ~{num(fp.median)} {unit}, IQR {num(fp.q25)}-{num(fp.q75)} {unit}, "
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
                    f"  raw median {num(fp.raw_median)}{suffix} shrunk toward the {fp.prior_source} prior "
                    f"{num(fp.prior)}{suffix} (weight {fp.prior_weight:g} samples; the gate counts n={fp.n_eff:g})"
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
                    f"  {lid}  {num(price) if price else kind:>8}  {first:%m-%d}  {state:<10} {spec:<10} {title[:60]}"
                )


def _per_tb(fp) -> str:
    """'812 Kč/TB (new 1 036 Kč/TB)' for a capacity-specific disk key ('hdd35 enterprise sas 4tb').

    A pooled class (fp.per_tb) is already quoted in Kč/TB; callers skip this for those."""
    import re

    m = re.search(r"(\d+(?:\.\d+)?)(tb|gb)$", fp.model)
    if m is None:
        return ""
    tb = float(m.group(1)) / (1000 if m.group(2) == "gb" else 1)
    text = f"{num(round(fp.median / tb))} Kč/TB"
    if fp.new_price and fp.new_source in RETAIL_SOURCES:
        text += f" (new {num(round(fp.new_price / tb))} Kč/TB)"
    return text


def cmd_prices(args: argparse.Namespace) -> None:
    from deal_hunter.db import repository as repo
    from deal_hunter.pricing import LEVEL_RANK

    with repo.connect() as conn:
        prices = load_estimates(conn, priors=not args.no_prior)
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
        iqr = f"{num(p.q25)}-{num(p.q75)}"
        new = (num(p.new_price) + ("" if p.new_source in RETAIL_SOURCES else "*")) if p.new_price else "-"
        prior = f"{p.prior_source} {num(p.prior)}" if p.prior else ""
        flags = []
        if p.raw_median and p.raw_median != p.median:
            flags.append(f"raw {num(p.raw_median)}")
        if p.over_new:
            flags.append("USED>=95% NEW")
        if p.prior_note:
            flags.append(p.prior_note)
        if p.category == "hdd":
            flags.insert(0, f"{num(p.median)} Kč/TB" if p.per_tb else _per_tb(p))
        print(
            f"{p.model:<28} {p.category:<13} {p.n:>4} {num(p.median):>8} {iqr:>15} {p.confidence:>5.2f}  {p.level:<6} "
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
    """The curated Zboží.cz product pages and Datart.cz category rows, and their latest lowest offers
    (--fetch reads the due ones)."""
    from deal_hunter import datart, zbozi
    from deal_hunter.db import repository as repo

    settings = load_settings()
    with repo.connect(autocommit=True) as conn, repo.connect(autocommit=True) as throttle_conn:
        repo.migrate(conn)
        if args.fetch:
            with make_new_price_http(settings, throttle_conn) as http:
                days = 0 if args.all else settings.new_price_days
                fetched = zbozi.fetch_due(http, conn, days, args.limit)
                print((fetched + datart.fetch_due(http, conn, days, settings.datart_pages)).summary())
        rows = repo.new_price_rows(conn)
        best = repo.latest_new_offers(conn)
    pages = zbozi.pages()
    print(f"{len(pages)} models, {sum(len(u) for u in pages.values())} product pages in new_price_pages.yaml")
    print("(Datart category rows are listed only where they are the model's new price)")
    for r in rows:
        top = best.get(r["model"])
        is_best = top is not None and (top[0], top[2]) == (r["min_price"], r["shop"])
        if r["shop"] != "zbozi" and not is_best:
            continue
        what = (
            f"{num(r['min_price']):>8} Kč {r['shops'] or 0:>2} shops"
            if r["min_price"]
            else (r["error"] or "no offers")[:40]
        )
        print(
            f"{'*' if is_best else ' '} {r['model']:<20} {what:<24} {r['shop']:<6} {r['fetched_at']:%Y-%m-%d}  "
            f"{(r['product'] or r['url'])[:60]}"
        )
    print("\n* = the model's new price (lowest offer across Zboží and Datart, fetched in the last 30 days)")


def register(sub: argparse._SubParsersAction) -> None:
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
