"""A small, plain, read-only LAN website: `hunt web` (port 8080, stdlib only, no JS framework).

Five pages plus a detail page, all server-rendered HTML with one inline CSS block:

- `/`            recent deals (would-alert or alerted listings), with `?category=` and
                  `?min_discount=` filters.
- `/prices`       the fair price table (reuses `pricing.run`, the same estimate `hunt prices` shows).
- `/auctions`     open nms.gov.cz lots with their valuation (reuses `auction_cycle.value`).
- `/flips`        the flips log (buy, sell, fees, profit, days held, totals) and the `hunt eval` tables.
- `/status`       what the LLM jobs and the scraper did lately, and whether anything looks stuck.
- `/listing/<id>` one ad: description, triage JSON, price history.

Every value that comes from an ad (title, description, seller, location, the LLM's verdict) is
untrusted input and is HTML-escaped before it goes on the page. All queries are parameterized
(`repository.py`/`pricing.py`/`auction_cycle.py`, no SQL built here). Read-only: nothing here ever
writes to the database.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from html import escape as esc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

CSS = """
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1rem; background: #fff; color: #111; }
  h1 { font-size: 1.3rem; margin: 0 0 .75rem; }
  nav { margin-bottom: 1rem; }
  nav a { margin-right: 1rem; color: #0a58ca; text-decoration: none; }
  nav a:hover { text-decoration: underline; }
  form.filters { margin-bottom: 1rem; }
  form.filters label { margin-right: .5rem; }
  form.filters input, form.filters select { margin-right: 1rem; }
  .tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { border-collapse: collapse; width: 100%; min-width: 600px; }
  th, td { padding: .35rem .6rem; border-bottom: 1px solid #ddd; text-align: left; white-space: nowrap; }
  th { background: #f3f3f3; position: sticky; top: 0; }
  td.wrap, th.wrap { white-space: normal; }
  tr:hover { background: #f8f8f8; }
  .muted { color: #777; }
  .bad { color: #a30000; }
  .good { color: #146c2e; }
  .empty { color: #777; padding: 1rem 0; }
  pre { background: #f5f5f5; padding: .75rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
  a.ad { color: #0a58ca; text-decoration: none; }
  a.ad:hover { text-decoration: underline; }
  footer { margin-top: 2rem; color: #999; font-size: .85rem; }
"""

NAV = '<nav><a href="/">Deals</a><a href="/prices">Prices</a><a href="/auctions">Auctions</a><a href="/flips">Flips</a><a href="/status">Status</a></nav>'


def page(title: str, body: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head>"
        f"<body>{NAV}<h1>{esc(title)}</h1>{body}</body></html>"
    )


def kc(n) -> str:
    return f"{round(n):,} Kč".replace(",", " ") if n is not None else "-"


def pct(x) -> str:
    return f"{x:.0%}" if x is not None else "-"


def km(x) -> str:
    return f"{x:.0f} km" if x is not None else "-"


def when(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    from deal_hunter.alerts import PRAGUE

    return f"{dt.astimezone(PRAGUE):%Y-%m-%d %H:%M}"


# --- / : recent deals -------------------------------------------------------------------------------

DEAL_STATES = ("alerted", "queued", "would_alert", "capped")
CATEGORIES = ("gpu", "office_pc", "office_laptop", "hdd")


def _matches_category(row_category: str, wanted: str) -> bool:
    if wanted == "office":
        return row_category.startswith("office")
    return row_category == wanted


def render_index(conn, category: str | None = None, min_discount: float | None = None) -> str:
    from deal_hunter.db import repository as repo

    deals = repo.deals_in_state(conn, *DEAL_STATES)
    if category:
        deals = [d for d in deals if _matches_category(d["category"], category)]
    if min_discount:
        deals = [d for d in deals if (d["discount"] or 0) >= min_discount]
    deals.sort(key=lambda d: d["alerted_at"] or d["first_flagged"], reverse=True)

    options = "".join(
        f'<option value="{esc(c)}"{" selected" if category == c else ""}>{esc(c)}</option>'
        for c in CATEGORIES
    )
    filters = (
        '<form class="filters" method="get">'
        f'<label>Category <select name="category"><option value="">any</option>{options}</select></label>'
        '<label>Min discount % <input type="number" name="min_discount" min="0" max="95" step="1" '
        f'value="{int(min_discount * 100) if min_discount else ""}"></label>'
        '<button type="submit">Filter</button>'
        "</form>"
    )
    if not deals:
        return filters + '<p class="empty">No deals in this view yet.</p>'

    rows = []
    for d in deals:
        state_cls = "good" if d["state"] == "alerted" else "muted"
        rows.append(
            "<tr>"
            f'<td class="wrap"><a class="ad" href="{esc(d["url"])}" target="_blank" rel="noopener">'
            f'{esc(d["title"])}</a><br><a href="/listing/{d["listing_id"]}">details</a></td>'
            f"<td>{kc(d['price'])}</td>"
            f"<td>{kc(d['fair'])}</td>"
            f'<td class="bad">{pct(d["discount"])}</td>'
            f"<td>{km(d['distance_km'])}</td>"
            f'<td class="wrap">{esc(d["verdict"] or "")}</td>'
            f"<td>{esc(d['category'])}</td>"
            f'<td class="{state_cls}">{esc(d["state"])}</td>'
            f"<td>{when(d['alerted_at'] or d['first_flagged'])}</td>"
            "</tr>"
        )
    table = (
        '<div class="tablewrap"><table><thead><tr>'
        "<th>Ad</th><th>Price</th><th>Fair</th><th>Discount</th><th>Distance</th>"
        '<th class="wrap">LLM verdict</th><th>Category</th><th>State</th><th>Seen</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    return filters + f'<p class="muted">{len(deals)} deal(s).</p>' + table


# --- /prices : fair price table ---------------------------------------------------------------------


def render_prices(conn) -> str:
    from deal_hunter import pricing
    from deal_hunter.config import load_settings

    settings = load_settings()
    prices = pricing.run(conn, settings.alert_min_samples, settings.usd_czk, save=False)
    rows = sorted(prices.values(), key=lambda p: (p.category, -p.n, p.model))
    if not rows:
        return '<p class="empty">No fair prices yet.</p>'

    body = []
    for p in rows:
        median = f"{kc(p.median)}/TB" if p.per_tb else kc(p.median)
        new = f"{kc(p.new_price)}{'/TB' if p.per_tb else ''} ({p.new_source})" if p.new_price else "-"
        if p.prior:
            note = f"{p.prior_source} prior {kc(p.prior)}{'/TB' if p.per_tb else ''}"
        elif p.prior_note:
            note = p.prior_note
        else:
            note = ""
        body.append(
            "<tr>"
            f"<td>{esc(p.model)}</td>"
            f"<td>{esc(p.category)}</td>"
            f"<td>{p.n}</td>"
            f"<td>{median}</td>"
            f"<td>{esc(p.level)}</td>"
            f"<td>{new}</td>"
            f'<td class="wrap">{esc(note)}</td>'
            "</tr>"
        )
    table = (
        '<div class="tablewrap"><table><thead><tr>'
        "<th>Model</th><th>Category</th><th>n</th><th>Median</th><th>Level</th><th>New price</th>"
        "<th>Prior / note</th></tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
    )
    return f'<p class="muted">{len(rows)} model(s). Asking prices of the last 60 days.</p>' + table


# --- /auctions : open nms.gov.cz lots --------------------------------------------------------------


def render_auctions(conn) -> str:
    from deal_hunter import auction_cycle, pricing
    from deal_hunter.auctions import Costs
    from deal_hunter.config import load_settings
    from deal_hunter.db import repository as repo

    settings = load_settings()
    costs = Costs(
        hourly_rate=settings.auction_hourly_rate,
        minutes_per_unit=settings.auction_minutes_per_unit,
        resale_factor=settings.auction_resale_factor,
        sell_per_week=settings.auction_sell_per_week,
        lot_cost=settings.auction_lot_cost,
        lot_hours=settings.auction_lot_hours,
        target_margin=settings.auction_target_margin,
        min_profit=settings.auction_min_profit,
    )
    prices = pricing.run(conn, settings.alert_min_samples, settings.usd_czk, save=False)
    lots = repo.lots_in_status(conn, "active", "prepared")
    lots.sort(key=lambda r: (r["end_at"] is None, r["end_at"]))
    if not lots:
        return '<p class="empty">No open lots right now.</p>'

    rows = []
    for lot in lots:
        x = auction_cycle.extraction_for(conn, lot)
        now_price = lot.get("current_price") or lot.get("start_price")
        if x is not None:
            v = auction_cycle.value(lot, x, prices, costs)
            max_bid = kc(v.max_bid)
            profit = kc(v.profit)
            conf = f"{v.confidence}{' QUALIFIES' if v.qualifies else ''}"
        else:
            max_bid = profit = conf = "not extracted yet"
        rows.append(
            "<tr>"
            f'<td class="wrap"><a class="ad" href="{esc(lot["url"])}" target="_blank" rel="noopener">'
            f"{esc(lot['title'])}</a></td>"
            f"<td>{when(lot.get('end_at'))}</td>"
            f"<td>{kc(now_price)}</td>"
            f"<td>{lot.get('bids') or 0}</td>"
            f"<td>{max_bid}</td>"
            f"<td>{profit}</td>"
            f"<td>{esc(conf)}</td>"
            "</tr>"
        )
    table = (
        '<div class="tablewrap"><table><thead><tr>'
        "<th>Lot</th><th>Ends</th><th>Current price</th><th>Bids</th><th>Max bid</th>"
        "<th>Profit at next bid</th><th>Confidence</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )
    return f'<p class="muted">{len(lots)} open lot(s).</p>' + table


# --- /flips : the flips log and how the alerts measure up --------------------------------------------

EVAL_DAYS = 90


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def render_flips(conn, now: datetime | None = None) -> str:
    from deal_hunter import evaluation
    from deal_hunter.db import repository as repo

    now = now or datetime.now(UTC)
    since = now - timedelta(days=EVAL_DAYS)
    flips = repo.list_flips(conn)
    deals = repo.eval_deals(conn, since)

    if flips:
        rows = []
        for f in flips:
            profit = evaluation.flip_profit(f)
            cls = "" if profit is None else ' class="good"' if profit >= 0 else ' class="bad"'
            rows.append(
                "<tr>"
                f'<td class="wrap"><a href="/listing/{f["listing_id"]}">{esc(f["title"] or str(f["listing_id"]))}</a>'
                f'<br><span class="muted">{esc(f["notes"] or "")}</span></td>'
                f"<td>{esc(f['category'] or '-')}</td>"
                f"<td>{when(f['bought_at'])}</td>"
                f"<td>{kc(f['buy_price'])}</td>"
                f"<td>{when(f['sold_at'])}</td>"
                f"<td>{kc(f['sold_price'])}</td>"
                f"<td>{kc(f['fees'])}</td>"
                f"<td{cls}>{kc(profit)}</td>"
                f"<td>{evaluation.days_held(f, now)}</td>"
                "</tr>"
            )
        headers = ["Ad", "Category", "Bought", "Buy", "Sold", "Sell", "Fees", "Profit", "Days held"]
        table = _table(headers, rows)
    else:
        table = '<p class="empty">No flips yet (tap Bought on a 👍 follow-up, or `hunt flip add`).</p>'
    t = evaluation.flip_totals(flips)
    roi = pct(t["roi"])
    totals = (
        f'<p class="muted">{t["open"]} open, {t["sold"]} sold. Invested in open flips {kc(t["invested"])}; '
        f"realised profit {kc(t['profit'])} on {kc(t['cost'])} spent, ROI {roi}.</p>"
    )

    recent = [f for f in flips if f["bought_at"] >= since]
    parts = [totals, table, f"<h2>Alert evaluation, last {EVAL_DAYS} days</h2>"]
    parts.append(f'<p class="muted">{len(deals)} deal(s) flagged. Same numbers as `hunt eval`.</p>')
    for t in evaluation.eval_tables(deals, recent, now):
        parts.append(f"<h3>{esc(t.title)}</h3>")
        parts.append(
            _table(t.headers, ["<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in t.rows])
        )
    return "".join(parts)


# --- /status : what ran when -------------------------------------------------------------------------

STALE_LOOPS = 3  # a listing source on the main loop is stale after this many missed cycles
NEW_PRICE_GRACE = timedelta(days=2)  # weekly new-price step: stale after HUNTER_NEW_PRICE_DAYS + this


def ago(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "never"
    minutes = max(0, int((now - dt).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h {minutes % 60} min ago"
    return f"{minutes // 1440} days ago"


def _stamp(dt: datetime | None, now: datetime) -> str:
    return "never" if dt is None else f"{when(dt)} ({ago(dt, now)})"


def _stale_flag(is_stale: bool) -> str:
    return ' <strong class="bad">STALE</strong>' if is_stale else ""


def _ad_link(listing_id: int, title: str | None) -> str:
    return f'<a class="ad" href="/listing/{listing_id}">{esc(title or str(listing_id))}</a>'


def _rows(headers: list[str], rows: list[str], empty: str) -> str:
    return _table(headers, rows) if rows else f'<p class="empty">{esc(empty)}</p>'


def _secs(x) -> str:
    return "-" if x is None else f"{x:.1f}"


def _yes(x) -> str:
    return "-" if x is None else "yes" if x else "no"


def _site_rows(settings, src: dict, now: datetime) -> list[dict]:
    """One dict per site the app knows: name, kind, enabled, last data, volume and state.

    Enabled is read from the same settings the worker and scraper load (all three services share
    .env in deploy/compose.yml): HUNTER_SOURCES, FB_COOKIES_FILE, HUNTER_NEW_PRICES, HUNTER_AUCTIONS.
    """
    loop = timedelta(minutes=settings.loop_minutes)
    fb_cycle = timedelta(minutes=settings.fb_cycle_max_minutes)
    price_cycle = timedelta(days=settings.new_price_days)
    sites = []

    def listing_site(name, source, on, limit, blocked=False):
        d = src["listings"].get(source)
        sites.append(
            {
                "name": name,
                "kind": "listings",
                "enabled": "blocked (robots.txt)" if blocked else "yes" if on else "no",
                "on": on and not blocked,
                "blocked": blocked,
                "last": d["last_seen"] if d else None,
                "limit": limit,
                "volume": f"{d['new_recent']} new in 24 h, {d['active']} active" if d else "-",
                "note": "",
            }
        )

    listing_site("Bazoš", "bazos", "bazos" in settings.sources, STALE_LOOPS * loop)
    listing_site("Aukro", "aukro", "aukro" in settings.sources, STALE_LOOPS * loop)
    listing_site(
        "Mironet", "mironet", "mironet" in settings.sources, 2 * timedelta(hours=settings.shop_hours)
    )
    listing_site("Facebook Marketplace", "fb", bool(settings.fb_cookies_file), 4 * fb_cycle)
    listing_site("Sbazar", "sbazar", "sbazar" in settings.sources, loop, blocked=True)
    for name, shop in (("Zboží.cz", "zbozi"), ("Datart", "datart")):
        d = src["prices"].get(shop)
        sites.append(
            {
                "name": name,
                "kind": "new prices",
                "enabled": "yes" if settings.new_prices_enabled else "no",
                "on": settings.new_prices_enabled,
                "blocked": False,
                "last": d["last_ok"] if d else None,
                "limit": price_cycle + NEW_PRICE_GRACE,
                "volume": f"{d['ok']} pages ok, {d['failed']} failed in the last pass" if d else "-",
                "note": f"last read failed: {d['error'][:200]}" if d and d["error"] else "",
                "seen": d is not None,
            }
        )
    nms = src["nms"]
    sites.append(
        {
            "name": "nms.gov.cz",
            "kind": "auctions",
            "enabled": "yes" if settings.auctions_enabled else "no",
            "on": settings.auctions_enabled,
            "blocked": False,
            "last": nms["last_seen"],
            "limit": 2 * timedelta(hours=settings.auction_list_hours),
            "volume": f"{nms['open']} open lots",
            "note": "",
        }
    )
    for s in sites:
        has_data = s["last"] is not None or s.get("seen", False)
        if s["blocked"]:
            s["state"] = "blocked"
        elif not s["on"]:
            s["state"] = "off"
        elif not has_data:
            s["state"] = "never ran"
        elif s["last"] is None or now - s["last"] > s["limit"]:
            s["state"] = "STALE"
        else:
            s["state"] = "OK"
    return sites


def _render_sources(sites: list[dict], now: datetime) -> str:
    enabled = [s for s in sites if s["on"]]
    counts = {k: sum(1 for s in enabled if s["state"] == k) for k in ("OK", "STALE", "never ran")}
    summary = (
        f"{len(enabled)} of {len(sites)} sites enabled · {counts['OK']} working · {counts['STALE']} stale"
    )
    if counts["never ran"]:
        summary += f" · {counts['never ran']} never ran"
    rows = []
    for s in sites:
        state = s["state"]
        if state == "STALE":
            cell = _stale_flag(True).strip()
        elif state == "OK":
            cell = '<span class="good">OK</span>'
        else:
            cell = esc(state)
        note = f'<br><span class="bad">{esc(s["note"])}</span>' if s["note"] else ""
        cls = "" if s["on"] else ' class="muted"'
        rows.append(
            f"<tr{cls}>"
            f"<td>{esc(s['name'])}</td>"
            f"<td>{esc(s['kind'])}</td>"
            f"<td>{esc(s['enabled'])}</td>"
            f"<td>{_stamp(s['last'], now)}</td>"
            f'<td class="wrap">{esc(s["volume"])}{note}</td>'
            f"<td>{cell}</td>"
            "</tr>"
        )
    return f"<h2>Sources</h2><p>{esc(summary)}</p>" + _table(
        ["Site", "Kind", "Enabled", "Last data", "Volume", "State"], rows
    )


def render_status(conn, now: datetime | None = None, settings=None) -> str:
    from deal_hunter import health
    from deal_hunter.config import load_settings
    from deal_hunter.db import repository as repo

    now = now or datetime.now(UTC)
    settings = settings or load_settings()
    sites = _site_rows(settings, repo.status_sources(conn, now), now)
    tri = repo.status_triage(conn, now)
    watch = repo.status_watch(conn, now)
    auc = repo.status_auctions(conn, now)
    pipe = repo.status_pipeline(conn)
    parts = [_render_sources(sites, now)]

    # LLM triage
    counts = tri["counts"]
    day = tri["day"]
    stuck = tri["pending_due"] > 0 and (
        tri["last_done"] is None or now - tri["last_done"] > health.TRIAGE_STALE
    )
    parts.append("<h2>LLM triage</h2>")
    parts.append(
        f"<p>Last result: {_stamp(tri['last_done'], now)}{_stale_flag(stuck)}<br>"
        f"Queue: {counts.get('pending', 0)} pending "
        f"({tri['pending_due']} due, {tri['pending_wait']} waiting to retry), "
        f"{counts.get('done', 0)} done, {counts.get('failed', 0)} failed.</p>"
    )
    if day["n"]:
        cache = pct(day["cached"] / day["n"])
        tokens = ""
        if day["prompt_tokens"] is not None or day["output_tokens"] is not None:
            tokens = f", tokens {day['prompt_tokens'] or 0} in / {day['output_tokens'] or 0} out"
        parts.append(
            f'<p class="muted">Last 24 h: {day["n"]} answered, avg {_secs(day["avg_seconds"])} s '
            f"(model calls only), cache hit rate {cache}{tokens}.</p>"
        )
    else:
        parts.append('<p class="muted">Last 24 h: nothing answered.</p>')
    if tri["last_error"]:
        at, err = tri["last_error"]
        parts.append(f'<p class="bad">Latest error ({when(at)}): {esc(err[:300])}</p>')
    rows = []
    for r in tri["recent"]:
        verdict = r["verdict"] or ""
        cls = ' class="bad"' if r["status"] == "failed" else ""
        deal = "-"
        if r["state"] is not None:
            score = "" if r["score"] is None else f" {r['score']:.2f}"
            deal = f"{esc(r['state'])}{score}"
        rows.append(
            "<tr>"
            f"<td>{when(r['at'])}</td>"
            f'<td class="wrap">{_ad_link(r["listing_id"], r["title"])}</td>'
            f"<td{cls}>{esc(r['condition'] or r['status'])}</td>"
            f'<td class="wrap">{esc(verdict)}</td>'
            f"<td>{_secs(r['seconds'])}</td>"
            f"<td>{_yes(r['cached'])}</td>"
            f"<td>{deal}</td>"
            "</tr>"
        )
    parts.append(
        _rows(
            ["When", "Ad", "Condition", "Verdict", "Seconds", "Cached", "Deal"],
            rows,
            "No triage results yet.",
        )
    )

    # Watchlists
    parts.append("<h2>Watchlist LLM checks</h2>")
    parts.append(
        f"<p>Last check: {_stamp(watch['last'], now)}<br>"
        f"Last 24 h: {watch['checked']} checked, {watch['matched']} matched, {watch['pushed']} pushed.</p>"
    )
    rows = [
        "<tr>"
        f"<td>{when(r['at'])}</td>"
        f'<td class="wrap">{esc(r["query"])}</td>'
        f'<td class="wrap">{_ad_link(r["listing_id"], r["title"])}</td>'
        f"<td>{_yes(r['matched'])}</td>"
        f"<td>{_yes(r['pushed_at'] is not None)}</td>"
        "</tr>"
        for r in watch["recent"]
    ]
    parts.append(_rows(["When", "Watch", "Ad", "Matched", "Pushed"], rows, "No watchlist checks yet."))

    # Auction extractions
    parts.append("<h2>Auction extractions</h2>")
    parts.append(
        f"<p>Last run: {_stamp(auc['last'], now)}<br>"
        f"Last 7 days: {auc['week']} lots read, {auc['pending']} pending.</p>"
    )
    rows = [
        "<tr>"
        f"<td>{when(r['at'])}</td>"
        f'<td class="wrap">{esc(r["title"])}</td>'
        f"<td>{esc(r['status'])}</td>"
        f"<td>{_secs(r['seconds'])}</td>"
        "</tr>"
        for r in auc["recent"]
    ]
    parts.append(_rows(["When", "Lot", "Status", "Seconds"], rows, "No auction extractions yet."))

    # Pipeline heartbeat
    parts.append("<h2>Pipeline</h2>")
    rows = []
    for r in pipe["runs"]:
        limit = health.STALE_AFTER.get(r["kind"])
        late = limit is not None and now - r["started_at"] > limit
        if r["finished_at"] is None:
            outcome = "running or interrupted"
        elif r["ok"]:
            outcome = '<span class="good">ok</span>'
        else:
            outcome = f'<span class="bad">{r["errors"]} error(s)</span>'
        rows.append(
            "<tr>"
            f"<td>{esc(r['kind'])}</td>"
            f"<td>{_stamp(r['started_at'], now)}{_stale_flag(late)}</td>"
            f"<td>{outcome}</td>"
            f"<td>{r['new']}</td>"
            "</tr>"
        )
    parts.append(
        '<p class="muted">scrape_runs does not record which source or section a run covered, '
        "so the latest run is shown per kind.</p>"
    )
    parts.append(_rows(["Run kind", "Started", "Outcome", "New ads"], rows, "No scrape runs yet."))
    rows = []
    for section, iso in pipe["sections"]:
        try:
            at = datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            at = None
        rows.append(f"<tr><td>{esc(section)}</td><td>{_stamp(at, now)}</td></tr>")
    parts.append("<h3>Sections</h3>")
    parts.append(_rows(["Section", "Last scraped"], rows, "No section has been scraped yet."))
    deal = pipe["deal"]
    if deal:
        listing_id, title, state, flagged = deal
        parts.append(
            f"<p>Newest deal flagged: {_ad_link(listing_id, title)} ({esc(state)}), {_stamp(flagged, now)}.</p>"
        )
    else:
        parts.append('<p class="empty">No deal flagged yet.</p>')
    return "".join(parts)


# --- /listing/<id> : one ad --------------------------------------------------------------------------


def render_listing(conn, listing_id: int) -> str | None:
    from deal_hunter.db import repository as repo

    row = repo.listing_for_followup(conn, listing_id)
    if row is None:
        return None

    lines = [
        f'<p><a class="ad" href="{esc(row["url"])}" target="_blank" rel="noopener">Open ad</a></p>',
        "<table>",
        f"<tr><th>Price</th><td>{kc(row.get('price_czk'))} ({esc(row.get('price_kind') or '')})</td></tr>",
        f"<tr><th>Fair price</th><td>{kc(row.get('fair'))}</td></tr>",
        f"<tr><th>Discount</th><td>{pct(row.get('discount'))}</td></tr>",
        f"<tr><th>Model</th><td>{esc(row.get('model') or '-')}</td></tr>",
        f"<tr><th>Category</th><td>{esc(row.get('category') or '-')}</td></tr>",
        f"<tr><th>Location</th><td>{esc(row.get('location') or '-')} ({esc(row.get('psc') or '-')})</td></tr>",
        f"<tr><th>Distance</th><td>{km(row.get('distance_km'))}</td></tr>",
        f"<tr><th>Seller</th><td>{esc(row.get('seller') or '-')}</td></tr>",
        f"<tr><th>Seen</th><td>first {when(row.get('first_seen'))}"
        + (f", gone {when(row.get('gone_at'))}" if row.get("gone_at") else ", active")
        + "</td></tr>",
        f"<tr><th>Deal state</th><td>{esc(row.get('deal_state') or '-')}</td></tr>",
        f"<tr><th>Verdict</th><td>{esc(row.get('verdict') or '-')}</td></tr>",
        "</table>",
    ]

    lines.append("<h2>Description</h2>")
    desc = row.get("description") or ""
    lines.append(f"<pre>{esc(desc)}</pre>" if desc else '<p class="muted">No description.</p>')

    triage_row = repo.latest_triage(conn, listing_id)
    lines.append("<h2>Triage</h2>")
    if triage_row and triage_row.get("result"):
        pretty = json.dumps(triage_row["result"], ensure_ascii=False, indent=2)
        lines.append(f"<pre>{esc(pretty)}</pre>")
    else:
        lines.append('<p class="muted">Not triaged (or no result yet).</p>')

    history = repo.price_history(conn, listing_id)
    lines.append("<h2>Price history</h2>")
    if history:
        hist_rows = "".join(
            f"<tr><td>{when(h['changed_at'])}</td><td>{kc(h['old_price'])} ({esc(h['old_kind'])})</td>"
            f"<td>{kc(h['new_price'])} ({esc(h['new_kind'])})</td></tr>"
            for h in history
        )
        lines.append(
            '<div class="tablewrap"><table><thead><tr><th>When</th><th>Old</th><th>New</th></tr></thead>'
            f"<tbody>{hist_rows}</tbody></table></div>"
        )
    else:
        lines.append('<p class="muted">No price changes recorded.</p>')

    return "".join(lines)


# --- HTTP server (stdlib only) -----------------------------------------------------------------------


def _not_found() -> str:
    return page("Not found", '<p class="empty">Nothing here.</p>')


def _error(message: str) -> str:
    return page("Error", f'<p class="bad">{esc(message)}</p>')


class Handler(BaseHTTPRequestHandler):
    server_version = "deal-hunter-web/1.0"

    def log_message(self, fmt, *args):  # quieter than the stdlib default
        log.info("%s - %s", self.address_string(), fmt % args)

    def _respond(self, status: int, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # stdlib method name (not snake_case)
        from deal_hunter.db import repository as repo

        parts = urlsplit(self.path)
        path, query = parts.path.rstrip("/") or "/", parse_qs(parts.query)
        try:
            with repo.connect(autocommit=True) as conn:
                if path == "/":
                    category = (query.get("category") or [""])[0].strip() or None
                    raw_disc = (query.get("min_discount") or [""])[0].strip()
                    min_discount = float(raw_disc) / 100 if raw_disc else None
                    body = render_index(conn, category, min_discount)
                    self._respond(200, page("Deals", body))
                elif path == "/prices":
                    self._respond(200, page("Fair prices", render_prices(conn)))
                elif path == "/auctions":
                    self._respond(200, page("Open auctions", render_auctions(conn)))
                elif path == "/flips":
                    self._respond(200, page("Flips", render_flips(conn)))
                elif path == "/status":
                    self._respond(200, page("Status", render_status(conn)))
                elif path.startswith("/listing/"):
                    raw_id = path.removeprefix("/listing/")
                    if not raw_id.isdigit():
                        self._respond(404, _not_found())
                        return
                    body = render_listing(conn, int(raw_id))
                    if body is None:
                        self._respond(404, _not_found())
                    else:
                        self._respond(200, page(f"Listing {raw_id}", body))
                else:
                    self._respond(404, _not_found())
        except Exception:  # LAN-only read-only tool: show a plain message, log the real error
            log.exception("error rendering %s", self.path)
            self._respond(500, _error("Something went wrong rendering this page."))


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Block forever, serving the site (`hunt web`)."""
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("deal-hunter web listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
