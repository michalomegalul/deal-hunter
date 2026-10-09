"""The worker: fair prices -> candidate deals -> LLM triage -> score -> ntfy push, plus feedback and
the weekly digest.

A deal moves through these states (deals.state):

    triage       priced well below fair; waiting for the LLM (queued while Ollama is down)
    dropped      the LLM found a reason to skip it (wanted ad, for parts, scam signs) or failed
    low_score    score below HUNTER_ALERT_MIN_SCORE
    would_alert  good enough, but alerts are off or the model is still gated (not enough data yet)
    queued      good enough and allowed; waiting for quiet hours to end
    alerted      pushed to the phone
    capped       the daily push limit was already used up
    duplicate    a repost of something pushed in the last 14 days
    expired      the ad disappeared or got too old before it could be pushed

The gate: a model only alerts once it has at least HUNTER_ALERT_MIN_SAMPLES priced samples and its
category's data spans at least HUNTER_ALERT_MIN_DAYS days. Until then deals are recorded as
'would_alert', so alerts switch themselves on as data comes in. HUNTER_ALERTS_ENABLED is the
global switch on top.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from deal_hunter import pricing, triage
from deal_hunter.pricing import LEVEL_RANK, FairPrice

log = logging.getLogger(__name__)

PRAGUE = ZoneInfo("Europe/Prague")
MAX_DISCOUNT = 0.9  # "price" under 10 % of fair is a typo or a placeholder, not a deal
FRESH_DAYS = 3  # only ads first seen this recently become candidates
QUEUE_HOURS = 24  # a held (quiet hours) push older than this is dropped
REPOST_DAYS = 14
CATEGORY_SECTIONS = {
    "gpu": ("graficka",),
    "office_pc": ("pc", "notebook"),
    "office_laptop": ("pc", "notebook"),
}
CATEGORY_TAG = {"gpu": "video_game", "office_pc": "desktop_computer", "office_laptop": "computer"}


# --- ntfy ---------------------------------------------------------------------------------------------


class Ntfy:
    """Publishes to the deals topic and reads the feedback topic on the ntfy server (LAN URL).

    Action buttons are tapped on the phone, which may be anywhere, so they point at the public URL.
    """

    def __init__(
        self,
        url: str,
        token: str,
        topic: str,
        public_url: str,
        feedback_topic: str,
        feedback_token: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url, self.topic, self.public_url = url.rstrip("/"), topic, public_url.rstrip("/")
        self.feedback_topic, self.feedback_token = feedback_topic, feedback_token
        self.http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=20, transport=transport
        )

    def vote_actions(self, listing_id: int) -> list[dict]:
        if not self.feedback_token:
            return []
        return [
            {
                "action": "http",
                "label": label,
                "url": f"{self.public_url}/{self.feedback_topic}",
                "method": "POST",
                "headers": {"Authorization": f"Bearer {self.feedback_token}"},
                "body": json.dumps({"ad": listing_id, "vote": vote}),
                "clear": True,
            }
            for label, vote in (("👍 good", "up"), ("👎 meh", "down"))
        ]

    def publish(self, title: str, message: str, priority: int = 3, tags=(), click=None, actions=()) -> str:
        body = {
            "topic": self.topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": list(tags),
        }
        if click:
            body["click"] = click
        if actions:
            body["actions"] = list(actions)
        r = self.http.post(self.url, json=body)
        r.raise_for_status()
        return r.json().get("id", "")

    def poll_feedback(self) -> list[dict]:
        """Every cached message on the feedback topic (ntfy keeps them ~12 h; we poll every few minutes)."""
        r = self.http.get(f"{self.url}/{self.feedback_topic}/json", params={"poll": "1", "since": "all"})
        r.raise_for_status()
        return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def parse_votes(events: list[dict]) -> list[tuple[int, str, str, datetime]]:
    """(listing_id, vote, ntfy message id, time) from feedback topic messages; junk is skipped."""
    out = []
    for e in events:
        if e.get("event") != "message":
            continue
        try:
            body = json.loads(e.get("message", ""))
            listing_id, vote = int(body["ad"]), str(body["vote"])
        except (ValueError, KeyError, TypeError):
            continue
        if vote in ("up", "down"):
            out.append((listing_id, vote, e["id"], datetime.fromtimestamp(e["time"], UTC)))
    return out


# --- the gate and the alert rules ------------------------------------------------------------------------


@dataclass(frozen=True)
class Rules:
    enabled: bool
    min_score: float
    max_per_day: int
    min_samples: int
    min_days: float
    quiet_start: int  # hour, Europe/Prague
    quiet_end: int
    quiet_bypass: float  # scores at least this high are pushed even in quiet hours
    candidate_discount: float


def category_days(data_since: dict, category: str, now: datetime) -> float:
    """How many days of data the category's sections have (the shortest one counts)."""
    starts = [data_since[s] for s in CATEGORY_SECTIONS.get(category, ()) if s in data_since]
    if not starts:
        return 0.0
    return (now - max(starts)).total_seconds() / 86400


def gate(fp: FairPrice | None, data_since: dict, rules: Rules, now: datetime) -> tuple[bool, str]:
    """(open, why not). Opens by itself once the model has enough samples and history."""
    if fp is None:
        return False, "no fair price"
    days = category_days(data_since, fp.category, now)
    missing = []
    if fp.n < rules.min_samples:
        missing.append(f"n={fp.n}<{rules.min_samples}")
    if days < rules.min_days:
        missing.append(f"{days:.1f}<{rules.min_days:g} days of data")
    return (not missing), ", ".join(missing)


def in_quiet_hours(now: datetime, rules: Rules) -> bool:
    hour = now.astimezone(PRAGUE).hour
    if rules.quiet_start > rules.quiet_end:
        return hour >= rules.quiet_start or hour < rules.quiet_end
    return rules.quiet_start <= hour < rules.quiet_end


def priority(discount: float) -> int:
    if discount >= 0.45:
        return 5
    if discount >= 0.33:
        return 4
    return 3


def kc(n: int) -> str:
    return f"{n:,} Kč".replace(",", " ")


def format_alert(deal: dict, fp: FairPrice | None) -> tuple[str, str, list[str]]:
    """(title, message, tags) for one deal push."""
    title = deal["title"][:90]
    sample = f", n={fp.n}, {fp.level} confidence" if fp else ""
    lines = [f"{kc(deal['price'])} vs ~{kc(deal['fair'])} fair (−{deal['discount']:.0%}){sample}"]
    lines.append(f"{deal['model']}")
    if deal.get("verdict"):
        lines.append(f"🤖 {deal['verdict']}")
    where = " ".join(x for x in (deal.get("location"), deal.get("psc")) if x)
    if where:
        lines.append(f"📍 {where}")
    tags = [CATEGORY_TAG.get(deal["category"], "shopping_cart")]
    if deal["discount"] >= 0.33:
        tags.append("moneybag")
    return title, "\n".join(lines), tags


def is_repost(deal: dict, recent: list[dict]) -> bool:
    """Same model, same seller or title, about the same price as something already pushed."""
    title = pricing.norm_title(deal["title"])
    seller = (deal.get("seller") or "").strip().lower()
    for r in recent:
        if r["listing_id"] == deal["listing_id"] or r["model"] != deal["model"]:
            continue
        same_who = (seller and seller == (r.get("seller") or "").strip().lower()) or pricing.norm_title(
            r["title"]
        ) == title
        if same_who and abs(r["price"] - deal["price"]) <= pricing.REPOST_PRICE_TOLERANCE * max(
            r["price"], deal["price"]
        ):
            return True
    return False


# --- the worker cycle ----------------------------------------------------------------------------------


@dataclass
class CycleStats:
    models: int = 0
    candidates: int = 0
    triaged: int = 0
    llm_down: bool = False
    pushed: int = 0
    would_alert: int = 0
    votes: int = 0
    digest: bool = False
    auctions: str = ""

    def summary(self) -> str:
        return (
            f"work: {self.models} priced models, {self.candidates} candidates, {self.triaged} triaged"
            + (" (LLM unreachable, queued)" if self.llm_down else "")
            + f", {self.would_alert} would-alert, {self.pushed} pushed, {self.votes} new votes"
            + (", digest sent" if self.digest else "")
            + (f"; {self.auctions}" if self.auctions else "")
        )


def find_candidates(conn, prices: dict[str, FairPrice], rules: Rules) -> int:
    """Active, fresh ads priced at least `candidate_discount` below a medium/high-confidence fair price."""
    from deal_hunter.db import repository as repo

    found = 0
    for listing_id, model, category, price, features in repo.active_priced(conn, FRESH_DAYS):
        fp = prices.get(model)
        if fp is None or LEVEL_RANK[fp.level] < LEVEL_RANK["medium"]:
            continue
        fair = fp.for_listing(features)
        discount = 1 - price / fair
        if rules.candidate_discount <= discount <= MAX_DISCOUNT:
            repo.upsert_candidate(
                conn, listing_id, model, category, price, fair, round(discount, 4), fp.confidence
            )
            found += 1
    return found


def run_triage(conn, client, cache_dir, limit: int, stats: CycleStats) -> None:
    """Ask the LLM about queued candidates (best first, at most `limit` per cycle)."""
    from deal_hunter.db import repository as repo

    for deal in repo.deals_in_state(conn, "triage"):
        if repo.get_triage(conn, deal["listing_id"], client.model, triage.PROMPT_VERSION) is None:
            repo.queue_triage(conn, deal["listing_id"], client.model, triage.PROMPT_VERSION)
    due = set(repo.due_triage(conn, client.model, triage.PROMPT_VERSION, limit))
    for deal in repo.deals_in_state(conn, "triage"):
        if deal["listing_id"] not in due:
            continue
        row = repo.get_triage(conn, deal["listing_id"], client.model, triage.PROMPT_VERSION)
        try:
            outcome = triage.triage(client, deal, cache_dir)
        except triage.LLMUnavailable as exc:
            attempts = row["attempts"] + 1
            wait = min(6 * 3600, 600 * 2 ** min(attempts, 6))
            repo.save_triage(
                conn,
                deal["listing_id"],
                client.model,
                triage.PROMPT_VERSION,
                attempts=attempts,
                error=str(exc)[:500],
                next_try_at=datetime.now(UTC) + timedelta(seconds=wait),
            )
            log.warning("LLM unreachable (%s); %d candidates stay queued", exc, len(due))
            stats.llm_down = True
            return
        repo.save_triage(
            conn,
            deal["listing_id"],
            client.model,
            triage.PROMPT_VERSION,
            status=outcome.status,
            attempts=row["attempts"] + 1,
            result=outcome.result.as_dict(),
            raw=outcome.raw,
            notes=outcome.notes,
            stats=outcome.stats,
            error=outcome.error,
        )
        stats.triaged += 1


def score_deals(conn, llm_model: str, rules: Rules) -> None:
    """Score candidates whose triage is done: drop, low_score, or 'ready' for the alert step."""
    from deal_hunter.db import repository as repo

    for deal in repo.deals_in_state(conn, "triage"):
        row = repo.get_triage(conn, deal["listing_id"], llm_model, triage.PROMPT_VERSION)
        if row is None or row["status"] == "pending":
            if deal["gone_at"] is not None:
                repo.update_deal(conn, deal["listing_id"], state="expired", reason="ad gone before triage")
            continue
        if row["status"] == "failed":
            repo.update_deal(conn, deal["listing_id"], state="dropped", reason="triage failed")
            continue
        t = triage.Triage(**row["result"])
        value, factor, reason = triage.score(deal["discount"], deal["confidence"], t)
        fields = {
            "score": value,
            "penalty": factor,
            "verdict": t.one_line_verdict,
            "priority": priority(deal["discount"]),
        }
        if reason:
            repo.update_deal(conn, deal["listing_id"], state="dropped", reason=reason, **fields)
        elif value < rules.min_score:
            repo.update_deal(
                conn, deal["listing_id"], state="low_score", reason=f"score {value:.3f}", **fields
            )
        else:
            repo.update_deal(conn, deal["listing_id"], state="ready", reason=None, **fields)


def send_alerts(conn, ntfy: Ntfy | None, prices, data_since, rules: Rules, now: datetime, stats: CycleStats):
    """Gate, quiet hours, daily cap, repost check, push. Best score first."""
    from deal_hunter.db import repository as repo

    for deal in repo.deals_in_state(conn, "ready"):
        fp = prices.get(deal["model"])
        is_open, why = gate(fp, data_since, rules, now)
        if not rules.enabled or ntfy is None:
            repo.update_deal(conn, deal["listing_id"], state="would_alert", reason="alerts disabled")
            stats.would_alert += 1
        elif not is_open:
            repo.update_deal(conn, deal["listing_id"], state="would_alert", reason=f"gated: {why}")
            stats.would_alert += 1
        else:
            repo.update_deal(conn, deal["listing_id"], state="queued", reason=None)

    queued = sorted(repo.deals_in_state(conn, "queued"), key=lambda d: d["score"] or 0, reverse=True)
    if not queued:
        return
    day_start = now.astimezone(PRAGUE).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = repo.alerts_since(conn, day_start)
    recent = repo.recent_alerts(conn, REPOST_DAYS)
    for deal in queued:
        lid = deal["listing_id"]
        if deal["gone_at"] is not None:
            repo.update_deal(conn, lid, state="expired", reason="ad gone")
            continue
        if now - deal["first_flagged"] > timedelta(hours=QUEUE_HOURS):
            repo.update_deal(conn, lid, state="expired", reason="held too long")
            continue
        if is_repost(deal, recent):
            repo.update_deal(conn, lid, state="duplicate", reason="repost of a recent push")
            continue
        if in_quiet_hours(now, rules) and (deal["score"] or 0) < rules.quiet_bypass:
            continue  # stays queued until the morning
        if sent_today >= rules.max_per_day:
            repo.update_deal(conn, lid, state="capped", reason=f"{rules.max_per_day} pushes today already")
            continue
        title, message, tags = format_alert(deal, prices.get(deal["model"]))
        try:
            msg_id = ntfy.publish(
                title, message, deal["priority"] or 3, tags, deal["url"], ntfy.vote_actions(lid)
            )
        except httpx.HTTPError as exc:
            log.warning("ntfy push failed for %s: %s (will retry)", lid, exc)
            return
        repo.update_deal(conn, lid, state="alerted", alerted_at=now, ntfy_id=msg_id)
        sent_today += 1
        stats.pushed += 1
        recent.append({**deal, "alerted_at": now})


def poll_feedback(conn, ntfy: Ntfy | None, stats: CycleStats) -> None:
    from deal_hunter.db import repository as repo

    if ntfy is None:
        return
    try:
        votes = parse_votes(ntfy.poll_feedback())
    except httpx.HTTPError as exc:
        log.warning("reading feedback failed: %s", exc)
        return
    stats.votes = repo.save_feedback(conn, votes)


def gate_status(prices: dict[str, FairPrice], data_since: dict, rules: Rules, now: datetime) -> dict:
    """{'ready': [models], 'waiting': {model: why}, 'days': {category: days}}."""
    ready, waiting = [], {}
    for model, fp in sorted(prices.items(), key=lambda kv: -kv[1].n):
        is_open, why = gate(fp, data_since, rules, now)
        if is_open:
            ready.append(model)
        else:
            waiting[model] = why
    days = {
        name: round(category_days(data_since, c, now), 1)
        for name, c in (("gpu", "gpu"), ("office", "office_pc"))
    }
    return {"ready": ready, "waiting": waiting, "days": days}


def digest_text(
    conn, prices: dict[str, FairPrice], data_since: dict, rules: Rules, now: datetime
) -> tuple[str, str]:
    """(title, message) of the weekly digest."""
    from deal_hunter.db import repository as repo

    week = repo.week_summary(conn)
    status = gate_status(prices, data_since, rules, now)
    deals = week["deals"]
    lines = [
        (
            f"{week['new_listings']} new ads this week. Deals flagged: {sum(deals.values())} "
            f"({deals.get('alerted', 0)} pushed, {deals.get('would_alert', 0)} would-alert, "
            f"{deals.get('dropped', 0)} dropped by the LLM)."
        )
    ]
    if week["new_by_model"]:
        lines.append("Busiest models: " + ", ".join(f"{m} ({n})" for m, n in week["new_by_model"]))
    then = repo.fair_price_on(conn, 7)
    moves = []
    for model, fp in prices.items():
        old = then.get(model)
        if old and LEVEL_RANK[fp.level] >= LEVEL_RANK["medium"] and old[0]:
            change = fp.median / old[0] - 1
            if abs(change) >= 0.05:
                moves.append((abs(change), f"{model} {change:+.0%} ({kc(old[0])} → {kc(fp.median)})"))
    if moves:
        lines.append("Median moves: " + "; ".join(text for _, text in sorted(moves, reverse=True)[:5]))
    if week["votes"]:
        lines.append(f"Your votes: 👍 {week['votes'].get('up', 0)} 👎 {week['votes'].get('down', 0)}")
    if not status["ready"]:
        days = ", ".join(f"{c} {d:g} d" for c, d in status["days"].items())
        lines.insert(
            0,
            f"Still learning: 0 of {len(prices)} priced models ready for alerts (need ≥{rules.min_samples} "
            f"samples and ≥{rules.min_days:g} days; data so far: {days}).",
        )
        return "deal-hunter: still learning", "\n".join(lines)
    lines.insert(0, f"{len(status['ready'])} of {len(prices)} priced models are alert-ready.")
    return "deal-hunter: weekly digest", "\n".join(lines)


def maybe_send_digest(conn, ntfy, prices, data_since, rules: Rules, now: datetime) -> bool:
    """Sunday 18:00 Prague time, once."""
    from deal_hunter.db import repository as repo

    local = now.astimezone(PRAGUE)
    if ntfy is None or not rules.enabled or local.weekday() != 6 or local.hour < 18:
        return False
    if repo.kv_get(conn, "digest_last") == local.date().isoformat():
        return False
    title, message = digest_text(conn, prices, data_since, rules, now)
    try:
        ntfy.publish(title, message, priority=2, tags=["bar_chart"])
    except httpx.HTTPError as exc:
        log.warning("digest push failed: %s", exc)
        return False
    repo.kv_set(conn, "digest_last", local.date().isoformat())
    return True


def work_once(
    conn, rules: Rules, llm, cache_dir, ntfy: Ntfy | None, triage_limit: int, now=None, auction_step=None
) -> CycleStats:
    """One worker cycle. `auction_step(conn, prices, quiet, now)` is the auction part (auction_cycle.tick)."""
    from deal_hunter.db import repository as repo

    now = now or datetime.now(UTC)
    stats = CycleStats()
    prices = pricing.run(conn)
    stats.models = len(prices)
    stats.candidates = find_candidates(conn, prices, rules)
    if llm is not None:
        run_triage(conn, llm, cache_dir, triage_limit, stats)
        score_deals(conn, llm.model, rules)
    data_since = repo.data_since(conn)
    send_alerts(conn, ntfy, prices, data_since, rules, now, stats)
    poll_feedback(conn, ntfy, stats)
    stats.digest = maybe_send_digest(conn, ntfy, prices, data_since, rules, now)
    if auction_step is not None:
        try:
            stats.auctions = auction_step(conn, prices, in_quiet_hours(now, rules), now).summary()
        except Exception:  # the auction site being odd must not stop Bazoš deals
            log.exception("auction step failed")
            stats.auctions = "auctions: failed (see log)"
    return stats


def parse_quiet_hours(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", text)
    if not m:
        raise SystemExit(f"HUNTER_QUIET_HOURS must look like '23-7', not {text!r}")
    return int(m.group(1)), int(m.group(2))
