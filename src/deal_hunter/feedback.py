"""The feedback service: listens to the deals-feedback topic live and acts on every tap within seconds.

Push buttons POST small JSON messages to the ntfy topic `deals-feedback` (with a write-only token,
see alerts.Ntfy): {"ad": id, "vote": "up"|"down"|"bought"} for Bazoš ads, {"lot": id, "vote":
"up"|"down"} for auction lots. This service (`hunt feedback --loop`) holds ntfy's JSON stream open
(`GET /deals-feedback/json?since=<last id>`), and for each message:

- stores the vote (`feedback`, or `flips` for "bought"), deduplicated by ntfy message id;
- for a 👍 sends the detailed follow-up (followup.py), once per ad or lot: a second 👍 doesn't
  send it again, and a 👍 older than FOLLOWUP_MAX_AGE (a replay after a long outage) is only stored;
- remembers the id of the last message it handled (kv `feedback_last_id`), so a restart resumes
  where it stopped. ntfy keeps messages ~12 h, so a shorter outage loses nothing.

If the connection drops it reconnects with backoff (2 s doubling to 2 min).

Before a listing follow-up the ad page is read once more (Bazoš budget, shared throttle), so the
follow-up quotes the current price and views, or says the ad is gone.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from deal_hunter import followup, geo, listener, pricing, scraper, triage
from deal_hunter.alerts import PRAGUE, Ntfy, Vote, first_image, parse_vote, place_line

log = logging.getLogger(__name__)

LAST_ID_KEY = "feedback_last_id"
FOLLOWUP_MAX_AGE = timedelta(hours=6)


@dataclass
class Context:
    """What handling a vote needs. `bazos` (PoliteClient) and `llm` may be None."""

    conn: object
    ntfy: Ntfy
    bazos: object = None
    llm: object = None
    cache_dir: object = None
    confident_n: int = pricing.CONFIDENT_N
    usd_czk: float = 23.5
    offer_discount: float = 0.07
    auction_costs: object = None  # auctions.Costs
    distance: object = None  # geo.DistanceRules: home and travel cost for the follow-up's distance line


# --- follow-ups -----------------------------------------------------------------------------------------


def _key(item_id, test: bool) -> str:
    return f"test:{item_id}" if test else str(item_id)


def listing_followup(ctx: Context, listing_id: int, source_id: str, now: datetime, test: bool = False) -> str:
    """Send the detailed follow-up for one ad. Returns what happened (for the log)."""
    from deal_hunter.db import repository as repo

    conn = ctx.conn
    row = repo.listing_for_followup(conn, listing_id)
    if row is None:
        return f"ad {listing_id} not in the database, nothing sent"
    if row["gone_at"] is None and ctx.bazos is not None:  # is it still there? (one polite request)
        try:
            fresh, gone, _ = scraper.fetch_ad(ctx.bazos, row["url"])
            if fresh is not None:
                repo.save_listing(conn, fresh, detail=True)
            elif gone is not None:
                repo.mark_gone(conn, listing_id, gone)
            row = repo.listing_for_followup(conn, listing_id)
        except httpx.HTTPError as exc:
            log.warning("could not re-check ad %s: %s", listing_id, exc)
    if row["gone_at"] is not None:
        title, message = followup.format_gone("listing", row["title"], row["gone_at"], test)
        ntfy_id = ctx.ntfy.publish(title, message, 2, ["wastebasket"], row["url"])
        repo.record_followup(conn, "listing", _key(listing_id, test), source_id, "gone", ntfy_id)
        return f"ad {listing_id} is gone; said so"

    prices = pricing.run(conn, ctx.confident_n, ctx.usd_czk, save=False)
    fp = prices.get(row["model"]) if row.get("model") else None
    fair = fp.for_listing(row.get("features")) if fp else row.get("fair")
    t = repo.latest_triage(conn, listing_id)
    result = t["result"] if t else None
    if result is None and ctx.llm is not None and row.get("model"):
        deal = {
            "title": row["title"],
            "price": row["price_czk"] or 0,
            "fair": fair or row["price_czk"] or 0,
            "location": row.get("location"),
            "psc": row.get("psc"),
            "description": row.get("description"),
            "model": row["model"],
            "category": row.get("category") or "gpu",
        }
        try:
            outcome = triage.triage(ctx.llm, deal, ctx.cache_dir)
            if outcome.status == "done":
                result = outcome.result.as_dict()
        except triage.LLMUnavailable as exc:
            log.warning("LLM unreachable for the follow-up of %s: %s", listing_id, exc)
    price = row["price_czk"] or 0
    offer = followup.offer_price(price, ctx.offer_discount) if price else 0
    if row.get("ships") is None:
        row["ships"] = geo.shipping_offer(f"{row['title']}\n{row.get('description') or ''}")
    if row.get("distance_km") is None:
        home = ctx.distance.home if ctx.distance else geo.PRAGUE_CENTRE
        row["distance_km"] = geo.distance_km(home, row.get("psc"), row.get("location"))[0]
    seller_message = followup.seller_message(row)
    title, message = followup.format_listing(
        row, fp, fair, result, seller_message, offer, now, test, place_line(row, ctx.distance)
    )
    actions = [{"action": "view", "label": "Open ad", "url": row["url"], "clear": False}]
    if ctx.ntfy.feedback_token:
        bought = {"ad": listing_id, "vote": "bought", **({"test": True} if test else {})}
        actions.append(ctx.ntfy.feedback_action("Bought ✅", bought))
    ntfy_id = ctx.ntfy.publish(title, message, 4, ["eyes"], row["url"], actions, attach=first_image(row))
    repo.record_followup(conn, "listing", _key(listing_id, test), source_id, "sent", ntfy_id)
    # Only the text, so a long-press copies exactly what to send; tapping it then opens the ad.
    copy_title = ("TEST · " if test else "") + "✉️ Message for the seller"
    ctx.ntfy.publish(copy_title, seller_message, 3, ["envelope"], row["url"])
    return f"follow-up for ad {listing_id} sent"


def lot_followup(ctx: Context, lot_id: str, source_id: str, now: datetime, test: bool = False) -> str:
    from deal_hunter import auction_cycle
    from deal_hunter.db import repository as repo

    conn = ctx.conn
    lot = repo.get_lot(conn, auction_cycle.SOURCE, lot_id)
    if lot is None:
        return f"lot {lot_id} not in the database, nothing sent"
    if lot["status"] not in ("active", "prepared") or (lot["end_at"] and lot["end_at"] < now):
        title, message = followup.format_gone("lot", lot["title"], lot["end_at"], test)
        ntfy_id = ctx.ntfy.publish(title, message, 2, ["wastebasket"], lot["url"])
        repo.record_followup(conn, "lot", _key(lot_id, test), source_id, "gone", ntfy_id)
        return f"lot {lot_id} has ended; said so"
    x = auction_cycle.extraction_for(conn, lot)
    if x is None:
        return f"lot {lot_id} not extracted yet, nothing sent"
    from deal_hunter.auctions import Costs

    prices = pricing.run(conn, ctx.confident_n, ctx.usd_czk, save=False)
    v = auction_cycle.value(lot, x, prices, ctx.auction_costs or Costs())
    local_end = lot["end_at"].astimezone(PRAGUE) if lot["end_at"] else None
    title, message = followup.format_lot(lot, x, v, local_end, test)
    actions = [{"action": "view", "label": "Open lot", "url": lot["url"], "clear": False}]
    ntfy_id = ctx.ntfy.publish(
        title, message, 4, ["classical_building"], lot["url"], actions, attach=first_image(lot)
    )
    repo.record_followup(conn, "lot", _key(lot_id, test), source_id, "sent", ntfy_id)
    return f"follow-up for lot {lot_id} sent"


def handle(ctx: Context, event: dict, now: datetime | None = None) -> str | None:
    """One event from the feedback topic. Returns what was done (None for keepalives and junk)."""
    from deal_hunter.db import repository as repo

    now = now or datetime.now(UTC)
    vote = parse_vote(event)
    if vote is None:
        return None
    if vote.test:  # a test push: no label, no flip; only the (TEST-marked) follow-up
        done = f"TEST {vote.kind} {vote.item}: {vote.vote}"
    else:
        fresh = repo.save_votes(ctx.conn, [vote])
        done = f"{vote.kind} {vote.item}: {vote.vote}" + ("" if fresh else " (already stored)")
    if vote.vote == "up":
        done += "; " + maybe_followup(ctx, vote, now)
    return done


def maybe_followup(ctx: Context, vote: Vote, now: datetime) -> str:
    from deal_hunter.db import repository as repo

    if vote.kind == "listing" and vote.item == "0":
        return "test push, no follow-up"
    key = f"test:{vote.item}" if vote.test else vote.item  # a test doesn't use up the real follow-up
    if repo.followup_sent(ctx.conn, vote.kind, key):
        return "follow-up already sent"
    if now - vote.voted_at > FOLLOWUP_MAX_AGE:
        return "vote too old for a follow-up"
    try:
        if vote.kind == "listing":
            return listing_followup(ctx, int(vote.item), vote.source_id, now, vote.test)
        return lot_followup(ctx, vote.item, vote.source_id, now, vote.test)
    except httpx.HTTPError as exc:
        log.warning("follow-up for %s %s failed: %s", vote.kind, vote.item, exc)
        return f"follow-up failed ({exc})"


# --- the stream -----------------------------------------------------------------------------------------


def listen_once(ctx: Context, stop=lambda: False) -> int:
    return listener.listen_once(
        ctx.conn, ctx.ntfy.stream_feedback, lambda event: handle(ctx, event), LAST_ID_KEY, "feedback", stop
    )


def forever(ctx: Context, sleep=time.sleep) -> None:
    listener.forever(lambda: listen_once(ctx), "feedback", sleep)
