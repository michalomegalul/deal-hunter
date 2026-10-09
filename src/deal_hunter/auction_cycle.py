"""The auction step of the worker: read lots from nms.gov.cz, have the LLM read them, value them, push.

Runs every worker cycle (5 min) but does real work only when something is due:

- the lot lists (active IT lots + the newest ended ones) every HUNTER_AUCTION_LIST_HOURS (2.5 h);
- a detail page for each new lot (plus its item-list PDFs), and once more after a lot's end time to
  record the final price;
- a fresh detail page right before a reminder push, so the push quotes the current price.

Pushes go to the same ntfy topic as Bazoš deals: when a qualifying lot is first found, then ~24 h and
~2 h before it ends if it still qualifies. One of each per lot, at most HUNTER_AUCTION_MAX_PER_DAY a
day (an allowance of their own, so Bazoš deals and auctions can't starve each other).
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from deal_hunter import auctions, nms, triage
from deal_hunter.auctions import Costs, Extraction
from deal_hunter.http import PoliteClient

log = logging.getLogger(__name__)

SOURCE = nms.SOURCE
MAX_ACTIVE_PAGES = 5  # 50 lots a page; the IT category has had ~15 running lots at a time
DAY_HOURS = 24.0
FINAL_HOURS = 2.5
TOO_LATE_HOURS = 0.25  # a reminder this close to the end is pointless (registration, bidding)


@dataclass(frozen=True)
class AuctionRules:
    enabled: bool  # the auction step at all
    alerts: bool  # pushes (HUNTER_ALERTS_ENABLED)
    list_hours: float
    details_per_cycle: int
    extract_per_cycle: int
    max_per_day: int
    costs: Costs


@dataclass
class AuctionStats:
    listed: bool = False
    lots: int = 0
    new: int = 0
    changed: int = 0
    details: int = 0
    pdfs: int = 0
    extracted: int = 0
    llm_down: bool = False
    valued: int = 0
    qualifying: int = 0
    pushed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"auctions: {'lists read, ' if self.listed else ''}{self.new} new, {self.details} details"
            f" ({self.pdfs} PDFs), {self.extracted} extracted"
            + (" (LLM unreachable)" if self.llm_down else "")
            + f", {self.valued} valued, {self.qualifying} qualifying, {self.pushed} pushed"
            + (f", {len(self.errors)} errors" if self.errors else "")
        )


def content_hash(lot: dict) -> str:
    """What the LLM reads (auctions.source_text); a change means the lot is extracted again."""
    return hashlib.sha256(auctions.source_text(lot).encode()).hexdigest()[:20]


# --- scraping -----------------------------------------------------------------------------------------


def read_lists(http: PoliteClient, conn, stats: AuctionStats) -> None:
    """All running IT lots, plus the first page of ended ones (their final prices)."""
    from deal_hunter.db import repository as repo

    for list_type, max_pages in (("active", MAX_ACTIVE_PAGES), ("closed", 1)):
        page = 1
        while page <= max_pages:
            resp = http.post_json(nms.LIST_API, nms.list_body(list_type, page))
            if resp.status != 200:
                stats.errors.append(f"{list_type} list page {page}: HTTP {resp.status}")
                break
            data = resp_json(resp)
            for lot in nms.parse_list(data):
                if lot.category and "výpočetní" not in lot.category.lower():
                    continue  # the API filters by category already; belt and braces
                is_new, changed = repo.upsert_lot_summary(conn, SOURCE, lot)
                stats.lots += 1
                stats.new += is_new
                stats.changed += changed
            if page >= (data.get("PageCount") or 1):
                break
            page += 1
    stats.listed = True


def resp_json(resp) -> dict:
    import json

    return json.loads(resp.text)


def fetch_detail(http: PoliteClient, conn, row: dict, stats: AuctionStats, now: datetime) -> dict | None:
    """One detail page (and, the first time, its item-list PDFs). Returns the stored lot."""
    from deal_hunter.db import repository as repo

    resp = http.get(nms.detail_url(row["lot_id"]))
    stats.details += 1
    if resp.status in (301, 302, 404, 410):
        repo.mark_lot_gone(conn, SOURCE, row["lot_id"])
        return None
    if resp.status != 200:
        stats.errors.append(f"lot {row['lot_id']}: HTTP {resp.status}")
        return None
    lot = nms.parse_detail(resp.text, row["lot_id"])
    text = row.get("attachment_text")
    if text is None and lot.status in ("active", "prepared", "ended"):
        texts = []
        for attachment in nms.list_attachments(lot.attachments):
            try:
                pdf = http.get(attachment["url"])
            except httpx.HTTPError as exc:
                stats.errors.append(f"lot {row['lot_id']} attachment: {exc}")
                continue
            stats.pdfs += 1
            if pdf.status == 200 and "pdf" in (pdf.content_type or "pdf"):
                body = nms.compact_list(nms.pdf_text(pdf.content))
                if body:
                    texts.append(f"[{attachment['name']}]\n{body}")
        text = "\n".join(texts)
    merged = {
        **(repo.get_lot(conn, SOURCE, row["lot_id"]) or {}),
        **_lot_fields(lot),
        "attachment_text": text,
    }
    ended = lot.end_at is not None and now > lot.end_at
    repo.save_lot_detail(
        conn, SOURCE, lot, text, content_hash(merged), final=ended and lot.status != "active"
    )
    return repo.get_lot(conn, SOURCE, row["lot_id"])


def _lot_fields(lot: nms.Lot) -> dict:
    """The parts of a parsed lot that feed source_text, in the database's column names."""
    out = {
        "lot_id": lot.lot_id,
        "title": lot.title,
        "description": lot.description,
        "inspection": lot.inspection,
        "address": lot.address,
        "attachments": lot.attachments,
        "institution": lot.institution,
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def scrape(http: PoliteClient, conn, rules: AuctionRules, stats: AuctionStats, now: datetime, force=False):
    """Read the lists when due, then the detail pages that are due."""
    from deal_hunter.db import repository as repo

    last = repo.kv_get(conn, "auctions_list_at")
    due = force or last is None or now - datetime.fromisoformat(last) >= timedelta(hours=rules.list_hours)
    if due:
        try:
            read_lists(http, conn, stats)
            repo.kv_set(conn, "auctions_list_at", now.isoformat())
        except httpx.HTTPError as exc:
            stats.errors.append(f"lists: {exc}")
            return
    for row in repo.lots_needing_detail(conn, rules.details_per_cycle, now):
        try:
            fetch_detail(http, conn, row, stats, now)
        except httpx.HTTPError as exc:
            stats.errors.append(f"lot {row['lot_id']}: {exc}")


# --- extraction and valuation ------------------------------------------------------------------------


def run_extraction(conn, llm, cache_dir, rules: AuctionRules, stats: AuctionStats) -> None:
    from deal_hunter.db import repository as repo

    for lot in repo.lots_to_extract(conn, llm.model, auctions.PROMPT_VERSION, rules.extract_per_cycle):
        key = (SOURCE, lot["lot_id"], llm.model, auctions.PROMPT_VERSION, lot["content_hash"])
        row = repo.get_extraction(conn, *key)
        attempts = (row["attempts"] if row else 0) + 1
        try:
            outcome = auctions.extract(llm, lot, cache_dir)
        except triage.LLMUnavailable as exc:
            wait = min(6 * 3600, 600 * 2 ** min(attempts, 6))
            repo.save_extraction(
                conn,
                *key,
                status="pending",
                attempts=attempts,
                error=str(exc)[:500],
                next_try_at=datetime.now(UTC) + timedelta(seconds=wait),
            )
            log.warning("LLM unreachable for auction lots (%s)", exc)
            stats.llm_down = True
            return
        repo.save_extraction(
            conn,
            *key,
            status=outcome.status,
            attempts=attempts,
            result=outcome.result.as_dict(),
            raw=outcome.raw,
            notes=outcome.notes,
            stats=outcome.stats,
            error=outcome.error,
        )
        stats.extracted += 1


def extraction_for(conn, lot: dict) -> Extraction | None:
    from deal_hunter.db import repository as repo

    row = repo.latest_extraction(conn, SOURCE, lot["lot_id"])
    return Extraction.from_dict(row["result"]) if row and row.get("result") else None


def value(lot: dict, x: Extraction, prices, costs: Costs) -> auctions.Valuation:
    price = lot.get("current_price") or lot.get("start_price")
    return auctions.value_lot(x, prices, price, lot.get("min_increment"), lot.get("bids") or 0, costs)


def value_running(conn, prices, rules: AuctionRules, stats: AuctionStats) -> None:
    from deal_hunter.db import repository as repo

    for lot in repo.lots_in_status(conn, "active", "prepared"):
        x = extraction_for(conn, lot)
        if x is None:
            continue
        v = value(lot, x, prices, rules.costs)
        repo.save_valuation(conn, SOURCE, lot["lot_id"], v.as_dict(), v.qualifies)
        stats.valued += 1
        stats.qualifying += v.qualifies


# --- pushes -------------------------------------------------------------------------------------------


def due_kind(sent: set[str], hours_left: float) -> str | None:
    """Which push a qualifying lot is due for: first, then day (<= 24 h left), then final (<= 2.5 h)."""
    if hours_left <= TOO_LATE_HOURS:
        return None
    if "first" not in sent:
        return "first"
    if "day" not in sent and hours_left <= DAY_HOURS:
        return "day"
    if "final" not in sent and hours_left <= FINAL_HOURS:
        return "final"
    return None


def covered_by(kind: str, hours_left: float) -> list[str]:
    """Later reminders a push makes redundant ('first' with 10 h left already is the day reminder)."""
    out = []
    if kind == "first" and hours_left <= DAY_HOURS:
        out.append("day")
    if kind in ("first", "day") and hours_left <= FINAL_HOURS:
        out.append("final")
    return out


def send_alerts(conn, http, ntfy, prices, rules: AuctionRules, quiet: bool, now: datetime, stats):
    from deal_hunter.alerts import PRAGUE
    from deal_hunter.db import repository as repo

    if not rules.alerts or ntfy is None:
        return
    day_start = now.astimezone(PRAGUE).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = repo.auction_alerts_since(conn, day_start)
    lots = [lot for lot in repo.lots_in_status(conn, "active") if lot["qualifies"] and lot["end_at"]]
    for lot in sorted(lots, key=lambda r: r["end_at"]):
        hours_left = (lot["end_at"] - now).total_seconds() / 3600
        kind = due_kind(repo.auction_alert_kinds(conn, SOURCE, lot["lot_id"]), hours_left)
        if kind is None or quiet:
            continue  # quiet hours: it waits for the morning (still within its window, or the next one)
        if sent_today >= rules.max_per_day:
            log.info("auction push cap (%d) reached; %s waits", rules.max_per_day, lot["lot_id"])
            return
        x = extraction_for(conn, lot)
        if x is None:
            continue
        if kind != "first":  # quote the price as it is now, not as it was at the last list read
            try:
                fresh = fetch_detail(http, conn, lot, stats, now)
            except httpx.HTTPError as exc:
                stats.errors.append(f"lot {lot['lot_id']} refresh: {exc}")
                continue
            if fresh is None or fresh["status"] != "active":
                continue
            lot = fresh
        v = value(lot, x, prices, rules.costs)
        repo.save_valuation(conn, SOURCE, lot["lot_id"], v.as_dict(), v.qualifies)
        if not v.qualifies:
            continue
        title, message = auctions.format_alert(lot, x, v, kind, lot["end_at"].astimezone(PRAGUE))
        priority = 4 if kind == "final" or (v.profit or 0) >= 3 * rules.costs.min_profit else 3
        try:
            msg_id = ntfy.publish(title, message, priority, ["classical_building"], lot["url"])
        except httpx.HTTPError as exc:
            log.warning("auction push failed for %s: %s", lot["lot_id"], exc)
            return
        repo.record_auction_alert(conn, SOURCE, lot["lot_id"], kind, msg_id, v.price, v.max_bid, v.profit)
        for later in covered_by(kind, hours_left):
            repo.record_auction_alert(conn, SOURCE, lot["lot_id"], later, None, v.price, v.max_bid, v.profit)
        sent_today += 1
        stats.pushed += 1


def tick(
    conn, http, llm, cache_dir, ntfy, prices, rules: AuctionRules, quiet: bool, now=None
) -> AuctionStats:
    """One auction step: scrape what's due, extract, value, push."""
    now = now or datetime.now(UTC)
    stats = AuctionStats()
    if not rules.enabled:
        return stats
    scrape(http, conn, rules, stats, now)
    if llm is not None:
        run_extraction(conn, llm, cache_dir, rules, stats)
    value_running(conn, prices, rules, stats)
    send_alerts(conn, http, ntfy, prices, rules, quiet, now, stats)
    for err in stats.errors:
        log.warning("auctions: %s", err)
    return stats
