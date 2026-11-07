"""A small, plain, read-only LAN website: `hunt web` (port 8080, stdlib only, no JS framework).

A plain shop catalogue look (square corners, no gradients/shadows, one link colour, dense
information, bold prices), not an "AI-looking" dashboard. Server-rendered HTML with one inline CSS
block; filters are plain GET forms, no client-side JS at all.

- `/`              recent deals (would-alert or alerted listings), with `?category=` and
                    `?min_discount=` filters.
- `/prices`         the fair price table (reuses `pricing.run`, the same estimate `hunt prices` shows).
- `/category[/<id>]` the category tree (`categories.py`) as the shop's own department pages: a grid
                    of square boxes (priced models / active offers / open deals per subtree) for
                    the index and a branch node, or a leaf's (or a group's) own product list -
                    every active offer, with GET filters (price, distance, source, "deals only",
                    sort: see `Filters`/`parse_filters`).
- `/model/<key>`    one pricing model: fair price, IQR, confidence, new price, a price bar (inline
                    SVG: cheapest live offer, fair price, new price, every offer as a tick), the
                    live offers (same row format as a category's product list) and a weekly price
                    history.
- `/auctions`       open nms.gov.cz lots with their valuation (reuses `auction_cycle.value`).
- `/flips`          the flips log (buy, sell, fees, profit, days held, totals) and the `hunt eval` tables.
- `/status`         what the LLM jobs and the scraper did lately, and whether anything looks stuck.
- `/listing/<id>`   one ad: description, triage JSON, price history.

Every value that comes from an ad (title, description, seller, location, the LLM's verdict) is
untrusted input and is HTML-escaped before it goes on the page; a listing's own image is hotlinked
(`loading="lazy"`), never downloaded or re-hosted. All queries are parameterized
(`repository.py`/`pricing.py`/`auction_cycle.py`, no SQL built here). Read-only: nothing here ever
writes to the database.
"""

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape as esc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from deal_hunter import categories
from deal_hunter.text import kc

log = logging.getLogger(__name__)

# A plain catalogue look (square corners everywhere, no border-radius/shadow/gradient, one link
# colour, a 1px light-grey border, dense information, bold prices), not a "framework" look.
CSS = """
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; margin: 0;
         padding: 1rem; background: #fff; color: #111; font-size: 15px; }
  h1 { font-size: 1.3rem; margin: 0 0 .75rem; }
  h2 { font-size: 1.05rem; margin: 1.25rem 0 .5rem; border-top: 1px solid #ddd; padding-top: .75rem; }
  a { color: #0a4e9e; }
  nav { margin-bottom: 1rem; border-bottom: 1px solid #ddd; padding-bottom: .6rem; }
  nav a { margin-right: 1rem; color: #0a4e9e; text-decoration: none; }
  nav a:hover { text-decoration: underline; }
  form.filters { margin-bottom: 1rem; padding: .6rem .75rem; border: 1px solid #ddd; background: #fafafa; }
  form.filters label { margin-right: .9rem; display: inline-block; margin-bottom: .4rem; }
  form.filters input[type=number] { width: 6rem; padding: .3rem; border: 1px solid #bbb; background: #fff; }
  form.filters select { padding: .3rem; border: 1px solid #bbb; background: #fff; }
  form.filters button { padding: .4rem 1rem; border: 1px solid #888; background: #eee; cursor: pointer; }
  .src-group { display: inline-block; margin-right: .9rem; }
  .tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { border-collapse: collapse; width: 100%; min-width: 300px; }
  th, td { padding: .35rem .6rem; border-bottom: 1px solid #ddd; text-align: left; white-space: nowrap; }
  th { background: #f3f3f3; }
  td.wrap, th.wrap { white-space: normal; }
  .muted { color: #666; }
  .bad { color: #a30000; }
  .good { color: #146c2e; }
  .empty { color: #666; padding: 1rem 0; }
  pre { background: #f5f5f5; padding: .75rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word;
        border: 1px solid #ddd; }
  a.ad { color: #0a4e9e; text-decoration: none; font-weight: 600; overflow-wrap: anywhere; }
  a.ad:hover { text-decoration: underline; }
  a.detail { margin-left: .6rem; font-size: .85rem; }
  footer { margin-top: 2rem; color: #888; font-size: .85rem; }

  .catgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: .6rem;
             margin: .5rem 0 1rem; }
  .catbox { border: 1px solid #ccc; padding: .75rem; background: #fff; }
  .catbox a { font-weight: 600; text-decoration: none; }
  .catbox a:hover { text-decoration: underline; }
  .catbox-stats { color: #666; font-size: .85rem; margin-top: .35rem; }

  .row { display: flex; gap: .75rem; padding: .6rem 0; border-bottom: 1px solid #ddd; align-items: flex-start; }
  .thumb { width: 72px; height: 72px; object-fit: cover; border: 1px solid #ddd; flex-shrink: 0; }
  .thumb.empty { background: #eee; }
  .row-body { flex: 1; min-width: 0; }
  .row-meta { margin-top: .3rem; color: #333; font-size: .9rem; }
  .row-meta span { margin-right: 1rem; }
  .price { font-weight: 700; }
  .disc { color: #a30000; font-size: .9rem; margin-top: .15rem; }

  .pricebar { width: 100%; height: 76px; display: block; margin: .75rem 0; }
  .axis { stroke: #bbb; stroke-width: 1; }
  .tick-offer { stroke: #999; stroke-width: 1; }
  .mark-fair { stroke: #0a4e9e; stroke-width: 2; }
  .mark-new { stroke: #a30000; stroke-width: 2; }
  .mark-cheapest { fill: #146c2e; }
  .mark-label { font-size: 10px; fill: #333; text-anchor: middle; }

  @media (max-width: 700px) {
    body { padding: .6rem; font-size: 16px; }
    nav a { margin-right: .7rem; display: inline-block; padding: .2rem 0; }
    .catgrid { grid-template-columns: 1fr 1fr; }
    .row { flex-wrap: wrap; }
    form.filters input[type=number], form.filters select, form.filters button { min-height: 2.3rem; }
    table { min-width: 0; }
  }
"""

NAV = (
    '<nav><a href="/">Výhodné</a><a href="/category">Kategorie</a><a href="/prices">Ceny</a>'
    '<a href="/auctions">Aukce</a><a href="/flips">Nákupy</a><a href="/status">Stav</a></nav>'
)


LEVEL_CS = {"high": "vysoká", "medium": "střední", "low": "nízká"}


def page(title: str, body: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head>"
        f"<body>{NAV}<h1>{esc(title)}</h1>{body}</body></html>"
    )


def pct(x) -> str:
    return f"{x:.0%}" if x is not None else "-"


def km(x) -> str:
    return f"{x:.0f} km" if x is not None else "-"


def kc_range(lo: int, hi: int, per_tb: bool = False) -> str:
    """'5 112 - 6 933 Kč' (a hyphen with spaces around it, never an en/em dash)."""
    unit = "/TB" if per_tb else ""
    return f"{lo:,} - {hi:,} Kč{unit}".replace(",", " ")


def when(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    from deal_hunter.alerts import PRAGUE

    return f"{dt.astimezone(PRAGUE):%Y-%m-%d %H:%M}"


# --- shared catalogue bits: source names, thumbnails, one product row --------------------------------

# One display name per source, in one place (categories.yaml's icon is an ntfy tag, never rendered
# here as an emoji; this is the only source -> label mapping the UI uses).
SOURCE_NAMES = {
    "bazos": "Bazoš",
    "aukro": "Aukro",
    "mironet": "Mironet",
    "fb": "Facebook",
    "sbazar": "Sbazar",
}


def source_name(code: str | None) -> str:
    return SOURCE_NAMES.get(code or "", (code or "-").capitalize())


def thumb(images: list | None) -> str:
    """Hotlinked thumbnail of the listing's own first image, or a plain grey box. Never downloaded
    or re-hosted."""
    url = (images or [None])[0]
    if url:
        return f'<img class="thumb" src="{esc(url)}" loading="lazy" alt="">'
    return '<div class="thumb empty"></div>'


def ships_offer(title: str, description: str | None) -> bool:
    from deal_hunter import geo

    return bool(geo.shipping_offer(f"{title}\n{description or ''}"))


def _row(
    *,
    listing_id: int,
    url: str,
    title: str,
    price,
    fair,
    images,
    distance_km,
    ships: bool,
    source,
    first_seen,
    extra: str = "",
) -> str:
    """One catalogue product row: thumbnail, the ad's own title (linking out), price, the discount
    note when it is cheaper than fair, distance (or "posílá" when it ships), source and first seen."""
    disc = ""
    if price and fair and price < fair:
        disc_pct = round((1 - price / fair) * 100)
        disc = f'<div class="disc">-{disc_pct} % pod férovou cenou</div>'
    dist = "posílá" if ships else km(distance_km)
    return (
        '<div class="row">'
        f"{thumb(images)}"
        '<div class="row-body">'
        f'<a class="ad" href="{esc(url)}" target="_blank" rel="noopener">{esc(title)}</a>'
        f'<a class="detail" href="/listing/{listing_id}">detail</a>'
        '<div class="row-meta">'
        f'<span class="price">{kc(price)}</span>'
        f"<span>{esc(dist)}</span>"
        f"<span>{esc(source_name(source))}</span>"
        f"<span>{when(first_seen)}</span>"
        "</div>"
        f"{disc}{extra}"
        "</div></div>"
    )


# --- GET filters: price range, max distance, source, "deals only" and sort ---------------------------

FILTER_SOURCES = ("bazos", "aukro", "mironet", "fb")
SORTS = (("newest", "nejnovější"), ("cheapest", "nejlevnější"), ("discount", "největší sleva"))


@dataclass(frozen=True)
class Filters:
    price_od: int | None = None
    price_do: int | None = None
    max_km: float | None = None
    sources: tuple[str, ...] = ()
    deals_only: bool = False
    sort: str = "newest"


def _num(raw: str | None, kind):
    if raw is None or not raw.strip():
        return None
    try:
        v = kind(raw)
    except ValueError:
        return None
    return v if v >= 0 else None


def parse_filters(query: dict[str, list[str]]) -> Filters:
    """GET params -> Filters, ignoring anything that doesn't parse (a bad number, an unknown source
    or sort) instead of raising."""

    def first(name: str) -> str | None:
        vals = query.get(name)
        return vals[0] if vals else None

    sort = first("sort")
    sources = tuple(s for s in query.get("source", []) if s in FILTER_SOURCES)
    return Filters(
        price_od=_num(first("price_od"), int),
        price_do=_num(first("price_do"), int),
        max_km=_num(first("max_km"), float),
        sources=sources,
        deals_only=first("deals") == "1",
        sort=sort if sort in dict(SORTS) else "newest",
    )


def _filters_form(action: str, f: Filters) -> str:
    source_boxes = "".join(
        f'<label><input type="checkbox" name="source" value="{s}"'
        f"{' checked' if s in f.sources else ''}> {esc(source_name(s))}</label>"
        for s in FILTER_SOURCES
    )
    sort_opts = "".join(
        f'<option value="{s}"{" selected" if f.sort == s else ""}>{esc(label)}</option>' for s, label in SORTS
    )
    return (
        f'<form class="filters" method="get" action="{esc(action)}">'
        f'<label>Cena od <input type="number" name="price_od" min="0" step="100" '
        f'value="{f.price_od if f.price_od is not None else ""}"></label>'
        f'<label>do <input type="number" name="price_do" min="0" step="100" '
        f'value="{f.price_do if f.price_do is not None else ""}"></label>'
        f'<label>Vzdálenost do (km) <input type="number" name="max_km" min="0" step="10" '
        f'value="{f.max_km if f.max_km is not None else ""}"></label>'
        f'<span class="src-group">{source_boxes}</span>'
        f'<label><input type="checkbox" name="deals" value="1"{" checked" if f.deals_only else ""}> '
        "jen výhodné</label>"
        f'<label>Seřadit <select name="sort">{sort_opts}</select></label>'
        '<button type="submit">Filtrovat</button>'
        "</form>"
    )


# --- / : recent deals -------------------------------------------------------------------------------

DEAL_STATES = ("alerted", "queued", "would_alert", "capped")
# The categories offered by the `?category=` filter and the CLI's `--category`: every priced leaf of
# the tree, plus the "office" group alias (office_pc + office_laptop) kept for backward compatibility.
CATEGORIES = (*(n.id for n in categories.leaves(priced=True)), "office")


def _matches_category(row_category: str, wanted: str) -> bool:
    return row_category in categories.subtree(wanted)


def _deal_row(d: dict) -> str:
    cat = categories.node(d["category"])
    cat_name = esc(cat.name) if cat else esc(d["category"])
    note = f'<a href="/category/{esc(d["category"])}">{cat_name}</a> · {esc(d["state"])}'
    if d.get("verdict"):
        note += f" - {esc(d['verdict'])}"
    return _row(
        listing_id=d["listing_id"],
        url=d["url"],
        title=d["title"],
        price=d["price"],
        fair=d["fair"],
        images=d.get("images"),
        distance_km=d.get("distance_km"),
        ships=ships_offer(d["title"], d.get("description")),
        source=d.get("source"),
        first_seen=d["alerted_at"] or d["first_flagged"],
        extra=f'<div class="muted">{note}</div>',
    )


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

    rows = "".join(_deal_row(d) for d in deals)
    return filters + f'<p class="muted">{len(deals)} deal(s).</p>' + rows


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
            f'<td><a href="/model/{esc(quote(p.model))}">{esc(p.model)}</a></td>'
            f'<td><a href="/category/{esc(p.category)}">{esc(p.category)}</a></td>'
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


# --- /category and /category/<id> : the category tree, as a shop's own department pages ------------
#
# categories.py (categories.yaml) is the single source of the tree; this just walks it. The index
# (node_id=None) and a branch node (pocitace, notebooky, komponenty, ...) both show a grid of square
# boxes, one per child (or per root), each with how many priced models, active offers and open deals
# fall under it (summed over its whole subtree). A leaf (or the "office" group alias) shows its own
# product list instead: every active offer, with the GET filters from `Filters`/`parse_filters`.


def _breadcrumb(node_id: str) -> str:
    chain = categories.ancestors(node_id)
    if not chain:  # a group (e.g. "office"): no place in the physical tree, so no trail above it
        g = categories.group(node_id)
        return esc(g.name) if g else esc(node_id)
    crumbs = [f'<a href="/category/{esc(n.id)}">{esc(n.name)}</a>' for n in chain[:-1]]
    crumbs.append(esc(chain[-1].name))
    return '<p class="muted">' + " › ".join(['<a href="/category">Kategorie</a>', *crumbs]) + "</p>"


def _category_grid(
    ids: tuple[str, ...], deal_counts: Counter, price_counts: Counter, offer_counts: dict[str, int]
) -> str:
    boxes = []
    for cid in ids:
        node = categories.node(cid)
        name = node.name if node else cid
        sub = categories.subtree(cid)
        models = sum(price_counts.get(s, 0) for s in sub)
        offers = sum(offer_counts.get(s, 0) for s in sub)
        deals = sum(deal_counts.get(s, 0) for s in sub)
        boxes.append(
            '<div class="catbox">'
            f'<a href="/category/{esc(cid)}">{esc(name)}</a>'
            f'<div class="catbox-stats">{models} modelů · {offers} nabídek · {deals} akcí</div>'
            "</div>"
        )
    return f'<div class="catgrid">{"".join(boxes)}</div>'


def _listing_rows(conn, leaf_ids: tuple[str, ...], prices: dict, f: Filters) -> list[dict]:
    """A leaf's product list: every active offer in it, with the fair price (and so the discount
    note and "jen výhodné") applied per listing from its own features, and the GET filters applied.
    Price and source are filtered in SQL (repo.listings_in_categories); distance and "deals only"
    need the fair price first, so they are filtered here."""
    from deal_hunter.db import repository as repo

    rows = repo.listings_in_categories(
        conn, list(leaf_ids), price_min=f.price_od, price_max=f.price_do, sources=f.sources or None
    )
    out = []
    for r in rows:
        fp = prices.get(r["model"]) if r.get("model") else None
        fair = fp.for_listing(r.get("features") or {}) if fp else None
        out.append({**r, "fair": fair, "ships": ships_offer(r["title"], r.get("description"))})
    if f.max_km is not None:
        out = [
            r for r in out if r["ships"] or (r["distance_km"] is not None and r["distance_km"] <= f.max_km)
        ]
    if f.deals_only:
        out = [r for r in out if r["price_czk"] and r["fair"] and r["price_czk"] < r["fair"]]
    if f.sort == "cheapest":
        out.sort(key=lambda r: r["price_czk"] or 10**9)
    elif f.sort == "discount":
        out.sort(
            key=lambda r: (1 - r["price_czk"] / r["fair"]) if r["price_czk"] and r["fair"] else -1,
            reverse=True,
        )
    else:
        out.sort(key=lambda r: r["first_seen"], reverse=True)
    return out


def render_category(conn, node_id: str | None = None, filters: Filters | None = None) -> str | None:
    """`/category` (node_id=None) and `/category/<id>`: a grid of square boxes for a branch (or the
    roots), or a leaf's (or the "office" group's) own product list. None for an id this module
    doesn't know (404)."""
    from deal_hunter import pricing
    from deal_hunter.config import load_settings
    from deal_hunter.db import repository as repo

    if node_id is not None and not categories.is_known(node_id):
        return None

    deals = repo.deals_in_state(conn, *DEAL_STATES)
    deal_counts = Counter(d["category"] for d in deals)
    settings = load_settings()
    prices = pricing.run(conn, settings.alert_min_samples, settings.usd_czk, save=False)
    price_counts = Counter(p.category for p in prices.values())
    offer_counts = repo.active_offer_counts(conn)

    if node_id is None:
        roots = tuple(n.id for n in categories.roots())
        return _category_grid(roots, deal_counts, price_counts, offer_counts)

    crumb = _breadcrumb(node_id)
    kids = categories.children(node_id)
    if kids:
        return crumb + _category_grid(kids, deal_counts, price_counts, offer_counts)

    # a leaf, or the "office" group: its own product list, directly (subtree() of a leaf is
    # itself; of "office" is office_pc + office_laptop).
    leaf_ids = categories.subtree(node_id)
    n_models = sum(price_counts.get(s, 0) for s in leaf_ids)
    n_offers = sum(offer_counts.get(s, 0) for s in leaf_ids)
    n_deals = sum(deal_counts.get(s, 0) for s in leaf_ids)
    header = f'<p class="muted">Modely: {n_models} · Nabídky: {n_offers} · Výhodné: {n_deals}</p>'

    f = filters or Filters()
    form = _filters_form(f"/category/{node_id}", f)
    rows = _listing_rows(conn, leaf_ids, prices, f)
    listing_html = (
        "".join(
            _row(
                listing_id=r["id"],
                url=r["url"],
                title=r["title"],
                price=r["price_czk"],
                fair=r["fair"],
                images=r.get("images"),
                distance_km=r.get("distance_km"),
                ships=r["ships"],
                source=r.get("source"),
                first_seen=r["first_seen"],
            )
            for r in rows
        )
        if rows
        else '<p class="empty">Filtru neodpovídá žádná nabídka.</p>'
    )
    return crumb + header + form + f'<p class="muted">Nalezeno: {len(rows)}</p>' + listing_html


# --- /model/<key> : one pricing model ---------------------------------------------------------------


def _price_bar(offer_prices: list[int], fair_median: int, new_price: int | None) -> str:
    """A plain horizontal price bar (inline SVG, no library): the cheapest live offer, the fair
    price and the new price marked on one axis, with every live offer as a small tick."""
    values = [p for p in offer_prices if p] + [fair_median] + ([new_price] if new_price else [])
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        hi = lo + 1
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    width, x0, x1 = 640, 20, 620

    def x(v: float) -> float:
        return x0 + (v - lo) / (hi - lo) * (x1 - x0)

    ticks = "".join(
        f'<line x1="{x(p):.1f}" y1="22" x2="{x(p):.1f}" y2="38" class="tick-offer"></line>'
        for p in offer_prices
        if p
    )
    cheapest = min((p for p in offer_prices if p), default=None)
    cheapest_mark = (
        f'<circle cx="{x(cheapest):.1f}" cy="30" r="4" class="mark-cheapest"></circle>' if cheapest else ""
    )
    fair_mark = (
        f'<line x1="{x(fair_median):.1f}" y1="12" x2="{x(fair_median):.1f}" y2="48" class="mark-fair"></line>'
        f'<text x="{x(fair_median):.1f}" y="64" class="mark-label">fér {kc(fair_median)}</text>'
    )
    new_mark = ""
    if new_price:
        new_mark = (
            f'<line x1="{x(new_price):.1f}" y1="12" x2="{x(new_price):.1f}" y2="48" class="mark-new"></line>'
            f'<text x="{x(new_price):.1f}" y="10" class="mark-label">nové {kc(new_price)}</text>'
        )
    return (
        f'<svg class="pricebar" viewBox="0 0 {width} 74" xmlns="http://www.w3.org/2000/svg" '
        'role="img" aria-label="cenové rozpětí">'
        f'<line x1="{x0}" y1="30" x2="{x1}" y2="30" class="axis"></line>'
        f"{ticks}{cheapest_mark}{fair_mark}{new_mark}"
        "</svg>"
    )


def _history_table(weeks: list[tuple]) -> str:
    """A small weekly-median price history table, from samples already in the database (no new
    table: repo.weekly_median_prices groups listings/normalized by week)."""
    if not weeks:
        return '<p class="empty">No price history yet.</p>'
    rows = [f"<tr><td>{wk:%Y-%m-%d}</td><td>{kc(median)}</td></tr>" for wk, median in weeks]
    return _table(["Týden", "Medián"], rows)


def render_model(conn, model: str) -> str | None:
    """`/model/<key>`: the fair price, the new price (if any), a price bar, current live offers
    from every source and a weekly price history. None if the model has no fair price yet (404)."""
    from deal_hunter import pricing
    from deal_hunter.config import load_settings
    from deal_hunter.db import repository as repo

    settings = load_settings()
    prices = pricing.run(conn, settings.alert_min_samples, settings.usd_czk, save=False)
    fp = prices.get(model)
    if fp is None:
        return None

    crumb = _breadcrumb(fp.category)  # the model name is the page's <h1>, not repeated here
    median = f"{kc(fp.median)}/TB" if fp.per_tb else kc(fp.median)
    iqr = kc_range(fp.q25, fp.q75, fp.per_tb)
    lines = [
        f"<tr><th>Férová cena</th><td>{median}</td></tr>",
        f"<tr><th>Běžné rozpětí</th><td>{iqr}</td></tr>",
        f"<tr><th>Spolehlivost</th><td>{esc(LEVEL_CS.get(fp.level, fp.level))} ({fp.confidence:.2f})</td></tr>",
        f"<tr><th>Vzorek</th><td>{fp.n} z {fp.n_listings} inzerátů ({fp.sold} rychle prodáno)</td></tr>",
    ]
    if fp.new_price:
        from deal_hunter.alerts import new_text

        lines.append(f"<tr><th>Nová cena</th><td>{esc(new_text(fp))}</td></tr>")
    summary = "<table>" + "".join(lines) + "</table>"

    offers = repo.active_model_listings(conn, model)
    offer_prices = [o["price_czk"] for o in offers if o.get("price_czk")]
    price_bar = _price_bar(offer_prices, fp.median, fp.new_price)
    if offers:
        offers_html = "".join(
            _row(
                listing_id=o["id"],
                url=o["url"],
                title=o["title"],
                price=o.get("price_czk"),
                fair=fp.for_listing(o.get("features") or {}),
                images=o.get("images"),
                distance_km=o.get("distance_km"),
                ships=ships_offer(o["title"], o.get("description")),
                source=o.get("source"),
                first_seen=o["first_seen"],
            )
            for o in offers
        )
    else:
        offers_html = '<p class="empty">Teď tu není žádná nabídka.</p>'

    history_html = _history_table(repo.weekly_median_prices(conn, model))
    return (
        crumb
        + summary
        + price_bar
        + f'<h2>Aktuální nabídky</h2><p class="muted">Nabídky: {len(offers)}</p>'
        + offers_html
        + "<h2>Vývoj ceny</h2>"
        + history_html
    )


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
        table = '<p class="empty">No flips yet (tap Bought on a liked follow-up, or `hunt flip add`).</p>'
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
                elif path == "/category":
                    self._respond(200, page("Categories", render_category(conn)))
                elif path.startswith("/category/"):
                    node_id = unquote(path.removeprefix("/category/"))
                    body = render_category(conn, node_id, parse_filters(query))
                    if body is None:
                        self._respond(404, _not_found())
                    else:
                        name = categories.node(node_id) or categories.group(node_id)
                        self._respond(200, page(name.name if name else node_id, body))
                elif path.startswith("/model/"):
                    model = unquote(path.removeprefix("/model/"))
                    body = render_model(conn, model)
                    if body is None:
                        self._respond(404, _not_found())
                    else:
                        self._respond(200, page(model, body))
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
