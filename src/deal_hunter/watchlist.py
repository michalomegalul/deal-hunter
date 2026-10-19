"""Watchlists (Phase 10 part 1): standing "I want X" searches, managed from a phone over ntfy.

Two independent pieces:

- **Matching** (`scan_and_match` + `push_matches`, called from the worker cycle, `hunt work`):
  every new listing from any source/section is checked against every active watch. A cheap
  keyword/regex prefilter on title+description runs first (`prefilter`); only prefilter hits go
  to the LLM (`check`, a local Ollama model, same pattern as triage.py: JSON-schema output,
  temperature 0, disk cache). Matches are pushed on the `deals` ntfy topic, respecting quiet
  hours and their own daily cap (`HUNTER_WATCH_MAX_PER_DAY`, separate from deal alerts).
- **Management** (`handle`, `listen_once`, `forever`, `hunt watch listen`): a small ntfy listener,
  the same live-stream pattern as feedback.py, that reads commands from its own topic
  (`HUNTER_WATCH_TOPIC`, default `watch`) and replies on the `deals` topic. Only messages on that
  topic are read at all (a separate ntfy subscription), and only text that parses as one of the
  known commands does anything; anything else gets a "didn't understand" reply.

Commands (Czech or English, case-insensitive; see `parse_command`):

    watch ddr4 2x16gb max 1500 km 50 #homelab   (also: sleduj, add, přidej)
    list                                        (also: seznam, ls)
    stop 3                                       (also: zastav, smazat, delete, remove)
    help                                         (also: nápověda, ?)
"""

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime

import httpx

from deal_hunter.triage import LLMUnavailable, chat_cached, parse_answer

log = logging.getLogger(__name__)

LAST_ID_KEY = "watch_last_id"
BACKOFF_START, BACKOFF_MAX = 2.0, 120.0


@dataclass
class Context:
    """What the command listener needs: a connection and the Ntfy client (reply + watch stream)."""

    conn: object
    ntfy: object


# --- text matching ----------------------------------------------------------------------------------


def normalize_text(text: str | None) -> str:
    """Lower case, no accents, 'x'/'gb'/'tb' spacing collapsed so '2 x 16 GB' ~ '2x16gb'."""
    t = unicodedata.normalize("NFKD", (text or "").lower()).replace("×", "x")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9x+]+", " ", t)
    t = re.sub(r"(\d+)\s*x\s*(\d+)", r"\1x\2", t)
    t = re.sub(r"(\d+)\s*(gb|tb|u)\b", r"\1\2", t)
    return " ".join(t.split())


def prefilter(keywords: dict, title: str, description: str | None = None) -> bool:
    """keywords = {"include": [[term, ...], ...], "exclude": [term, ...]}.

    Excluded if any exclude term appears; otherwise matched if every term of at least one
    include group appears (groups are OR'd, terms within a group are AND'd). No include groups
    at all matches anything not excluded.
    """
    text = normalize_text(f"{title} {description or ''}")
    exclude = keywords.get("exclude") or []
    if any(normalize_text(term) in text for term in exclude):
        return False
    include = keywords.get("include") or []
    if not include:
        return True
    for group in include:
        terms = group if isinstance(group, list) else [group]
        if all(normalize_text(term) in text for term in terms):
            return True
    return False


def keywords_from_query(query: str) -> dict:
    """A plain watch created from free text: every word of the query must appear (one AND group)."""
    terms = [normalize_text(w) for w in query.split() if normalize_text(w)]
    return {"include": [terms]} if terms else {}


# --- commands ---------------------------------------------------------------------------------------

ADD_WORDS = {"watch", "sleduj", "add", "pridej", "pridat"}
LIST_WORDS = {"list", "seznam", "ls"}
STOP_WORDS = {"stop", "zastav", "smaz", "smazat", "delete", "remove", "odstran"}
HELP_WORDS = {"help", "napoveda", "?"}

PRICE_RE = re.compile(r"\b(?:max|do)\s*(\d[\d\s]*)\s*(?:kc|czk|korun)?\b", re.IGNORECASE)
KM_RE = re.compile(r"\b(?:km|radius|okruh)\s*(\d+)\b", re.IGNORECASE)
TAG_RE = re.compile(r"#(\w+)")

HELP_TEXT = (
    "Commands: 'watch <words> [max <price>] [km <radius>] [#tag]' (sleduj/add/přidej also work), "
    "'list' (seznam), 'stop <id>' (zastav/smazat), 'help' (nápověda)."
)


def _ascii(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


@dataclass
class Command:
    kind: str  # add | list | stop | help | unknown
    query: str | None = None
    max_price: int | None = None
    radius_km: float | None = None
    tag: str | None = None
    watch_id: int | None = None
    error: str | None = None


def parse_add(rest: str) -> Command:
    tag_m = TAG_RE.search(rest)
    tag = tag_m.group(1).lower() if tag_m else None
    rest = TAG_RE.sub("", rest)
    price_m = PRICE_RE.search(rest)
    max_price = int(re.sub(r"\s", "", price_m.group(1))) if price_m else None
    if price_m:
        rest = PRICE_RE.sub(" ", rest)
    km_m = KM_RE.search(rest)
    radius_km = float(km_m.group(1)) if km_m else None
    if km_m:
        rest = KM_RE.sub(" ", rest)
    query = " ".join(rest.split())
    if not query:
        return Command("unknown", error="watch needs some words to search for, e.g. 'watch ddr4 16gb'")
    return Command("add", query=query, max_price=max_price, radius_km=radius_km, tag=tag)


def parse_command(text: str) -> Command:
    """One line of text from the watch topic as a Command. Unparseable text -> kind='unknown'."""
    text = (text or "").strip()
    if not text:
        return Command("unknown", error="empty message")
    first, _, rest = text.partition(" ")
    word = _ascii(first).rstrip(".,!")
    if word in HELP_WORDS:
        return Command("help")
    if word in LIST_WORDS:
        return Command("list")
    if word in STOP_WORDS:
        m = re.search(r"\d+", rest)
        if not m:
            return Command("unknown", error="stop needs a watch id, e.g. 'stop 3'")
        return Command("stop", watch_id=int(m.group()))
    if word in ADD_WORDS:
        return parse_add(rest)
    return Command("unknown", error=f"didn't understand {text[:60]!r}")


# --- seeds -------------------------------------------------------------------------------------------
# Idempotent defaults for `hunt watch seed` (owner lives in Prague; prices are sensible ceilings
# for a private seller, not shop prices).

SEEDS: tuple[dict, ...] = (
    {
        "query": "DDR4 UDIMM 2x16GB or 2x32GB non-ECC 3200",
        "keywords": {
            "include": [["ddr4", "2x16gb"], ["ddr4", "2x32gb"], ["ddr4", "kit 2x16"], ["ddr4", "kit 2x32"]],
            "exclude": ["ecc", "rdimm", "sodimm", "ddr3", "ddr5"],
        },
        "max_price": 1600,
        "radius_km": 80,
        "tag": "homelab",
    },
    {
        "query": "LSI HBA 9207-8i / 9300-8i / 9211-8i, IT mode",
        "keywords": {
            "include": [["9207 8i"], ["9300 8i"], ["9211 8i"]],
            "exclude": ["raid card"],
        },
        "max_price": 2500,
        "radius_km": 100,
        "tag": "homelab",
    },
    {
        "query": '4U rack case fitting mATX with many 3.5" bays',
        "keywords": {
            "include": [["4u", "rack"], ["4u", "case"], ["rackmount", "3.5"]],
            "exclude": [],
        },
        "max_price": 3000,
        "radius_km": 100,
        "tag": "homelab",
    },
    {
        "query": '19" rack (rozvaděč/rack) >= 12U',
        "keywords": {
            "include": [["rozvadec"], ["19 rack"], ["rackova skrin"], ["rack skrin"]],
            "exclude": [],
        },
        "max_price": 4000,
        "radius_km": 100,
        "tag": "homelab",
    },
    {
        "query": "UniFi gear",
        "keywords": {
            "include": [["unifi"], ["ubiquiti"]],
            "exclude": [],
        },
        "max_price": 5000,
        "radius_km": 100,
        "tag": "homelab",
    },
)


def seed(conn) -> int:
    """Insert the default watches that don't already exist (matched by `query`). Idempotent."""
    from deal_hunter.db import repository as repo

    existing = {w["query"] for w in repo.list_watches(conn)}
    added = 0
    for s in SEEDS:
        if s["query"] in existing:
            continue
        repo.insert_watch(
            conn,
            s["query"],
            s["keywords"],
            categories=[],
            max_price=s.get("max_price"),
            radius_km=s.get("radius_km"),
            tag=s.get("tag"),
            created_by="seed",
        )
        added += 1
    return added


# --- the LLM check -----------------------------------------------------------------------------------

WATCH_SYSTEM_PROMPT = """You check whether a second-hand ad from a Czech classifieds site matches what a \
buyer is looking for. The ad is in Czech; read it carefully.

Decide "matches": true only if the ad is clearly selling the kind of item wanted (right product type, \
and not contradicted by what is wanted, e.g. wrong RAM generation, wrong form factor, wrong interface). \
If the ad is a "looking to buy" post, or sells something unrelated, or you aren't sure, answer false.

evidence: a short exact quote from the ad's own text (title or description) that backs up your answer. \
If matches is true, the quote must show it really is the item. Must be the ad's own words, not a \
paraphrase, and empty only if matches is false and nothing relevant is quotable.

Also extract, from the ad only (null if not stated):
- product_type: what kind of item it is in a few English words (e.g. "RAM kit", "HBA card", "server rack").
- brand: the manufacturer, as named in the ad.
- model: the model/part number, as named in the ad.
- condition: "new", "used", or what the ad says about its state, in a few words; null if not said.
- price_czk: the asking price in Kč as an integer; null if not a fixed price (dohodou, etc.)."""

WATCH_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {"type": "boolean"},
        "product_type": {"type": ["string", "null"]},
        "brand": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "condition": {"type": ["string", "null"]},
        "price_czk": {"type": ["integer", "null"]},
        "evidence": {"type": "string"},
    },
    "required": ["matches", "product_type", "brand", "model", "condition", "price_czk", "evidence"],
}


def build_watch_prompt(query: str, title: str, description: str | None, price_czk: int | None) -> str:
    price = f"{price_czk:,} Kč".replace(",", " ") if price_czk else "not a fixed price"
    desc = (description or "").strip()[:3000] or "(no description)"
    return f"Wanted: {query}\n\nAsking price: {price}\n\nAd title: {title}\nAd description:\n{desc}"


@dataclass
class WatchMatch:
    matches: bool = False
    product_type: str | None = None
    brand: str | None = None
    model: str | None = None
    condition: str | None = None
    price_czk: int | None = None
    evidence: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def clean_watch_answer(data: dict, source: str) -> WatchMatch:
    """Coerce the model's JSON into a WatchMatch; a quote that isn't really in the ad -> no match
    (small models like to say yes with a made-up quote, same risk triage.py guards against)."""
    evidence = " ".join(str(data.get("evidence", "")).split())
    matches = bool(data.get("matches"))
    if matches and (len(_plain(evidence)) < 4 or _plain(evidence) not in _plain(source)):
        matches, evidence = False, evidence + " (quote not found in the ad; treated as no match)"
    price = data.get("price_czk")
    price_czk = int(price) if isinstance(price, int | float) and not isinstance(price, bool) else None

    def _s(key: str) -> str | None:
        v = data.get(key)
        return (str(v).strip() or None) if v else None

    return WatchMatch(
        matches=matches,
        product_type=_s("product_type"),
        brand=_s("brand"),
        model=_s("model"),
        condition=_s("condition"),
        price_czk=price_czk,
        evidence=evidence[:300],
    )


def check(client, watch: dict, listing: dict, cache_dir) -> WatchMatch:
    """Ask the LLM whether `listing` matches `watch`. Raises LLMUnavailable if Ollama is down."""
    source = f"{listing['title']}\n{listing.get('description') or ''}"
    user = build_watch_prompt(
        watch["query"], listing["title"], listing.get("description"), listing.get("price_czk")
    )
    raw, _stats, _cached = chat_cached(client, WATCH_SYSTEM_PROMPT, user, cache_dir, WATCH_RESPONSE_SCHEMA)
    try:
        data, _notes = parse_answer(raw)
        return clean_watch_answer(data, source)
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        log.warning("unreadable watch answer for listing %s: %s", listing["id"], exc)
        return WatchMatch(matches=False, evidence=f"(unreadable LLM answer: {exc})")


# --- matching (worker cycle) -------------------------------------------------------------------------


@dataclass
class ScanStats:
    scanned: int = 0
    prefiltered: int = 0
    llm_checked: int = 0
    matched: int = 0
    pushed: int = 0
    llm_down: bool = False

    def summary(self) -> str:
        return (
            f"watches: {self.scanned} scanned, {self.prefiltered} prefiltered, "
            f"{self.llm_checked} llm-checked, {self.matched} matched, {self.pushed} pushed"
            + (" (LLM unreachable)" if self.llm_down else "")
        )


def scan_and_match(conn, llm, cache_dir, hours: float, limit: int = 300) -> ScanStats:
    """Prefilter + LLM check every active watch against its recent, not-yet-checked listings."""
    from deal_hunter.db import repository as repo

    stats = ScanStats()
    for watch in repo.list_watches(conn, active_only=True):
        for listing in repo.listings_for_watch(conn, watch["id"], hours, limit):
            stats.scanned += 1
            if not prefilter(watch["keywords"], listing["title"], listing.get("description")):
                continue
            stats.prefiltered += 1
            if llm is None:
                continue
            try:
                match = check(llm, watch, listing, cache_dir)
            except LLMUnavailable as exc:
                log.warning("LLM unreachable for watch %s / listing %s: %s", watch["id"], listing["id"], exc)
                stats.llm_down = True
                continue
            stats.llm_checked += 1
            if repo.record_watch_hit(conn, watch["id"], listing["id"], match.matches, match.as_dict()):
                stats.matched += match.matches
    return stats


def _kc(n: int | None) -> str:
    return f"{n:,} Kč".replace(",", " ") if n else "-"


def format_watch_push(hit: dict) -> tuple[str, str]:
    """(title, message) for one watch-hit push."""
    title = f"👀 {hit['query']}"[:90]
    lines = [hit["title"][:200]]
    price = hit["price_czk"]
    lines.append(_kc(price))
    km = hit.get("distance_km")
    where = " ".join(x for x in (hit.get("location"), hit.get("psc")) if x)
    if where or km is not None:
        lines.append((f"📍 {where}" if where else "📍") + (f" · {km:.0f} km" if km is not None else ""))
    result = hit.get("result") or {}
    if result.get("evidence"):
        lines.append(f"🤖 „{result['evidence']}“")
    if hit.get("tag"):
        lines.append(f"#{hit['tag']}")
    source = hit.get("source")
    if source and source != "bazos":
        from deal_hunter.alerts import SOURCE_LABEL

        lines.append(f"via {SOURCE_LABEL.get(source, source)}")
    return title, "\n".join(lines)


def _in_quiet_hours(now: datetime, quiet_start: int, quiet_end: int) -> bool:
    from deal_hunter.alerts import PRAGUE

    hour = now.astimezone(PRAGUE).hour
    if quiet_start > quiet_end:
        return hour >= quiet_start or hour < quiet_end
    return quiet_start <= hour < quiet_end


def push_matches(conn, ntfy, now: datetime, quiet_start: int, quiet_end: int, max_per_day: int) -> int:
    """Push matched-but-not-pushed hits on the deals topic: quiet hours, a separate daily cap."""
    from deal_hunter.alerts import PRAGUE, first_image
    from deal_hunter.db import repository as repo

    if ntfy is None:
        return 0
    if _in_quiet_hours(now, quiet_start, quiet_end):
        return 0
    day_start = now.astimezone(PRAGUE).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = repo.watch_pushes_since(conn, day_start)
    pushed = 0
    for hit in repo.pending_watch_matches(conn):
        if sent_today >= max_per_day:
            break
        title, message = format_watch_push(hit)
        try:
            ntfy.publish(title, message, 3, ["eyes"], hit["url"], attach=first_image(hit))
        except httpx.HTTPError as exc:
            log.warning(
                "watch push failed for %s/%s: %s (will retry)", hit["watch_id"], hit["listing_id"], exc
            )
            break
        repo.mark_watch_pushed(conn, hit["watch_id"], hit["listing_id"])
        sent_today += 1
        pushed += 1
    return pushed


def tick(
    conn,
    llm,
    cache_dir,
    ntfy,
    now: datetime,
    scan_hours: float,
    quiet_start: int,
    quiet_end: int,
    max_per_day: int,
) -> str:
    """One worker-cycle step: scan + push. Returns a one-line summary."""
    stats = scan_and_match(conn, llm, cache_dir, scan_hours)
    pushed = push_matches(conn, ntfy, now, quiet_start, quiet_end, max_per_day)
    stats.pushed = pushed
    return stats.summary()


# --- command listener (ntfy topic `watch`) ------------------------------------------------------------


def execute(conn, command: Command, created_by: str | None = None) -> str:
    """Run a parsed command against the database. Returns the reply text (sent on the deals topic)."""
    from deal_hunter.db import repository as repo

    if command.kind == "help":
        return HELP_TEXT
    if command.kind == "list":
        rows = repo.list_watches(conn)
        if not rows:
            return "No watches yet. " + HELP_TEXT
        lines = []
        for w in rows:
            bits = [f"#{w['id']}", w["query"]]
            if w["max_price"]:
                bits.append(f"max {w['max_price']} Kč")
            if w["radius_km"]:
                bits.append(f"{w['radius_km']:g} km")
            if w["tag"]:
                bits.append(f"#{w['tag']}")
            if not w["active"]:
                bits.append("(stopped)")
            lines.append(" · ".join(bits))
        return "\n".join(lines)
    if command.kind == "stop":
        ok = repo.set_watch_active(conn, command.watch_id, False)
        return f"Stopped watch #{command.watch_id}." if ok else f"No watch #{command.watch_id}."
    if command.kind == "add":
        keywords = keywords_from_query(command.query)
        watch_id = repo.insert_watch(
            conn,
            command.query,
            keywords,
            categories=[],
            max_price=command.max_price,
            radius_km=command.radius_km,
            tag=command.tag,
            created_by=created_by,
        )
        bits = [f"Watching #{watch_id}: {command.query}"]
        if command.max_price:
            bits.append(f"max {command.max_price} Kč")
        if command.radius_km:
            bits.append(f"{command.radius_km:g} km")
        if command.tag:
            bits.append(f"#{command.tag}")
        return " · ".join(bits)
    return f"Didn't understand that ({command.error}). {HELP_TEXT}"


def handle(ctx, event: dict, now: datetime | None = None) -> str | None:
    """One event from the watch topic. `ctx` needs `.conn` and `.ntfy`. None for non-messages."""
    if event.get("event") != "message":
        return None
    text = event.get("message", "")
    command = parse_command(text)
    reply = execute(ctx.conn, command, created_by=event.get("id"))
    ctx.ntfy.publish(f"watch: {text[:60]}", reply, 2, ["mag"])
    return reply


def listen_once(ctx, stop=lambda: False) -> int:
    """Hold the watch-topic stream open until it drops (or `stop()`). Returns messages handled."""
    from deal_hunter.db import repository as repo

    since = repo.kv_get(ctx.conn, LAST_ID_KEY) or "all"
    handled = 0
    for event in ctx.ntfy.stream_watch(since):
        if event.get("event") == "open":
            log.info("watch stream connected (since %s)", since)
        if event.get("event") == "message":
            try:
                result = handle(ctx, event)
            except Exception:
                log.exception("handling watch command %s failed", event.get("id"))
                result = "failed"
            log.info("watch %s: %s", event.get("id"), result)
            repo.kv_set(ctx.conn, LAST_ID_KEY, event["id"])
            handled += 1
        if stop():
            break
    return handled


def forever(ctx, sleep=time.sleep) -> None:
    delay = BACKOFF_START
    while True:
        started = time.monotonic()
        try:
            listen_once(ctx)
            log.info("watch stream closed by the server; reconnecting")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("watch stream: %s; reconnecting in %.0f s", exc, delay)
        except Exception:
            log.exception("watch stream failed; reconnecting in %.0f s", delay)
        if time.monotonic() - started > 60:
            delay = BACKOFF_START
        sleep(delay)
        delay = min(BACKOFF_MAX, delay * 2)
