"""Watchlists: standing "I want X" searches, managed from a phone over ntfy.

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

    watch ddr4 2x16gb -notebook -vadna max 1500 km 50 #homelab   (also: sleduj, add, přidej)
    list                                        (also: seznam, ls)
    stop 3                                       (also: zastav, smazat, delete, remove)
    start 3                                      (also: obnov, spustit)
    mute ddr4 7d                                 (also: mute <watch id> <N>d; also: ztlum, ztlumit)
    vote 3-501 up                                (also: vote 3-501 down; tapped from the push buttons)
    help                                         (also: nápověda, ?)

A `-word` token anywhere in an `add` query is an exclude term (`rtx 3060 -notebook -vadna`), checked
by `prefilter` the same way the seeded watches' own exclude lists are. `mute` snoozes a watch: still
matched and recorded, just not pushed, until the given number of days (`d`), hours (`h`) or weeks
(`w`) from now. A watch-hit push carries "Stop"/"👍"/"👎" action buttons that post `stop <id>` /
`vote <watch_id>-<listing_id> up|down` back to this same topic (see `push_matches`), the same
HTTP-action mechanism as the deals feedback buttons (`alerts.Ntfy.feedback_action`).
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from deal_hunter import listener
from deal_hunter.text import fold, kc
from deal_hunter.triage import LLMUnavailable, chat_cached, parse_answer

log = logging.getLogger(__name__)

LAST_ID_KEY = "watch_last_id"


@dataclass
class Context:
    """What the command listener needs: a connection and the Ntfy client (reply + watch stream)."""

    conn: object
    ntfy: object


# --- text matching ----------------------------------------------------------------------------------


def normalize_text(text: str | None) -> str:
    """Lower case, no accents, 'x'/'gb'/'tb' spacing collapsed so '2 x 16 GB' ~ '2x16gb'."""
    t = fold(text).replace("×", "x")
    t = re.sub(r"[^a-z0-9x+]+", " ", t)
    t = re.sub(r"(\d+)\s*x\s*(\d+)", r"\1x\2", t)
    t = re.sub(r"(\d+)\s*(gb|tb|u)\b", r"\1\2", t)
    # 'SO-DIMM' / 'non-ECC' as one word, so exclude terms 'sodimm' and 'ecc' mean what they say
    t = re.sub(r"\bso\s+dimm", "sodimm", t)
    t = re.sub(r"\bnon\s+ecc", "nonecc", t)
    return " ".join(t.split())


def prefilter(keywords: dict, title: str, description: str | None = None) -> bool:
    """keywords = {"include": [[term, ...], ...], "exclude": [term, ...]}.

    Excluded if any exclude term appears at the start of a word ('ddr3' catches 'ddr3l', but 'ecc'
    doesn't fire inside 'nonecc'); otherwise matched if every term of at least one include group
    appears anywhere (groups are OR'd, terms within a group are AND'd). No include groups at all
    matches anything not excluded.
    """
    text = normalize_text(f"{title} {description or ''}")
    exclude = [normalize_text(term) for term in keywords.get("exclude") or []]
    if any(term and re.search(rf"\b{re.escape(term)}", text) for term in exclude):
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
START_WORDS = {"start", "obnov", "spustit"}
MUTE_WORDS = {"mute", "ztlum", "ztlumit"}
VOTE_WORDS = {"vote"}
HELP_WORDS = {"help", "napoveda", "?"}

PRICE_RE = re.compile(r"\b(?:max|do)\s*(\d[\d\s]*)\s*(?:kc|kč|czk|korun)?\b", re.IGNORECASE)
# The usual form: "km 50" / "radius 50" / "okruh 50" (checked first -- a model number like "3060"
# that happens to sit right before an unrelated "km 40" must not be mistaken for the radius itself).
KM_PREFIX_RE = re.compile(r"\b(?:km|radius|okruh)\s*(\d+)\b", re.IGNORECASE)
# The number-first form: "50 km", optionally led by "do" ("do 50 km" means a radius, not a price,
# even though PRICE_RE also treats a leading "do" as "up to"). Only used when KM_PREFIX_RE found
# nothing, and only when "km" isn't itself immediately followed by another number (which would mean
# it was actually introducing a *different* radius in prefix form).
KM_SUFFIX_RE = re.compile(r"\b(?:do\s*)?(\d+)\s*(?:km|kilometr\w*)\b(?!\s*\d)", re.IGNORECASE)
TAG_RE = re.compile(r"#(\w+)")
# "mute ddr4 7d" / "mute 3 2w" / "mute rozvadec 12h" -- the duration always trails the target.
MUTE_DURATION_RE = re.compile(r"\b(\d+)\s*([dhw])\b", re.IGNORECASE)
MUTE_UNIT_DAYS = {"d": 1.0, "h": 1.0 / 24, "w": 7.0}
# "vote 3-501 up" -- the hit id is "<watch_id>-<listing_id>" (watch_hits has no surrogate key of
# its own; this is the pair that already uniquely identifies a hit, see record_watch_hit).
VOTE_RE = re.compile(r"\b(\d+)-(\d+)\s+(up|down)\b", re.IGNORECASE)

HELP_TEXT = (
    "Commands: 'watch <words> [-exclude] [max <price>] [km <radius>] [#tag]' "
    "(sleduj/add/přidej also work), 'list' (seznam), 'stop <id>' (zastav/smazat), "
    "'start <id>' (obnov), 'mute <words or id> <N>d|h|w' (ztlum), 'help' (nápověda)."
)


@dataclass
class Command:
    kind: str  # add | list | stop | start | mute | vote | help | unknown
    query: str | None = None
    max_price: int | None = None
    radius_km: float | None = None
    tag: str | None = None
    excludes: tuple[str, ...] = ()
    watch_id: int | None = None
    listing_id: int | None = None
    vote: str | None = None
    mute_days: float | None = None
    error: str | None = None


def parse_add(rest: str) -> Command:
    tag_m = TAG_RE.search(rest)
    tag = tag_m.group(1).lower() if tag_m else None
    rest = TAG_RE.sub("", rest)
    # Distance first: "do 50 km" is a radius, not a price, even though it starts with "do" (which
    # PRICE_RE also treats as "up to"); extracting km first keeps PRICE_RE from swallowing the "do".
    km_m, km_re = KM_PREFIX_RE.search(rest), KM_PREFIX_RE
    if km_m is None:
        km_m, km_re = KM_SUFFIX_RE.search(rest), KM_SUFFIX_RE
    radius_km = float(km_m.group(1)) if km_m else None
    if km_m:
        rest = km_re.sub(" ", rest)
    price_m = PRICE_RE.search(rest)
    max_price = int(re.sub(r"\s", "", price_m.group(1))) if price_m else None
    if price_m:
        rest = PRICE_RE.sub(" ", rest)
    # Exclude terms: any "-word" token (not a hyphen inside a word, e.g. "SO-DIMM" survives since
    # there is no space before its "-DIMM").
    words = rest.split()
    excludes = tuple(normalize_text(w[1:]) for w in words if w.startswith("-") and len(w) > 1)
    rest = " ".join(w for w in words if not (w.startswith("-") and len(w) > 1))
    query = " ".join(rest.split())
    if not query:
        return Command("unknown", error="watch needs some words to search for, e.g. 'watch ddr4 16gb'")
    return Command("add", query=query, max_price=max_price, radius_km=radius_km, tag=tag, excludes=excludes)


def parse_mute(rest: str) -> Command:
    dur_m = MUTE_DURATION_RE.search(rest)
    if not dur_m:
        return Command("unknown", error="mute needs a duration, e.g. 'mute ddr4 7d'")
    days = int(dur_m.group(1)) * MUTE_UNIT_DAYS[dur_m.group(2).lower()]
    target = " ".join(MUTE_DURATION_RE.sub(" ", rest).split())
    if not target:
        return Command("unknown", error="mute needs a watch id or words, e.g. 'mute ddr4 7d'")
    if target.isdigit():
        return Command("mute", watch_id=int(target), mute_days=days)
    return Command("mute", query=target, mute_days=days)


def parse_vote(rest: str) -> Command:
    m = VOTE_RE.match(rest.strip())
    if not m:
        return Command("unknown", error="vote needs a hit id and up/down, e.g. 'vote 3-501 up'")
    return Command("vote", watch_id=int(m.group(1)), listing_id=int(m.group(2)), vote=m.group(3).lower())


def parse_command(text: str) -> Command:
    """One line of text from the watch topic as a Command. Unparseable text -> kind='unknown'."""
    text = (text or "").strip()
    if not text:
        return Command("unknown", error="empty message")
    first, _, rest = text.partition(" ")
    word = fold(first).rstrip(".,!")
    if word in HELP_WORDS:
        return Command("help")
    if word in LIST_WORDS:
        return Command("list")
    if word in STOP_WORDS:
        m = re.search(r"\d+", rest)
        if not m:
            return Command("unknown", error="stop needs a watch id, e.g. 'stop 3'")
        return Command("stop", watch_id=int(m.group()))
    if word in START_WORDS:
        m = re.search(r"\d+", rest)
        if not m:
            return Command("unknown", error="start needs a watch id, e.g. 'start 3'")
        return Command("start", watch_id=int(m.group()))
    if word in MUTE_WORDS:
        return parse_mute(rest)
    if word in VOTE_WORDS:
        return parse_vote(rest)
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
- price_czk: the asking price in Kč as an integer; null if not a fixed price (dohodou, etc.).
- accessory_only: true if the ad sells only an accessory, spare part or power supply that goes with \
such an item (mount, bracket, cover, cable, PoE injector or adapter, power supply, transceiver) and not \
the device itself; false if it sells the device (accessories included with it are fine), or if the \
accessory itself is what is wanted."""

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
        "accessory_only": {"type": "boolean"},
    },
    "required": [
        "matches",
        "product_type",
        "brand",
        "model",
        "condition",
        "price_czk",
        "evidence",
        "accessory_only",
    ],
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
    accessory_only: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def clean_watch_answer(data: dict, source: str) -> WatchMatch:
    """Coerce the model's JSON into a WatchMatch; a quote that isn't really in the ad -> no match
    (small models like to say yes with a made-up quote, same risk triage.py guards against).

    An ad selling only an accessory for the item (a mount, a PoE injector) is no match either: asked
    in the `matches` rule alone, gemma3 says yes to those for a broad watch like "UniFi gear", but it
    answers the separate `accessory_only` question well.
    """
    evidence = " ".join(str(data.get("evidence", "")).split())
    matches = bool(data.get("matches"))
    accessory_only = data.get("accessory_only") is True
    if matches and (len(_plain(evidence)) < 4 or _plain(evidence) not in _plain(source)):
        matches, evidence = False, evidence + " (quote not found in the ad; treated as no match)"
    if matches and accessory_only:
        matches = False
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
        accessory_only=accessory_only,
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


def within_limits(watch: dict, listing: dict) -> str | None:
    """Why `listing` is outside the watch's own limits (max price, pickup radius, sections), or None.

    Uses the listing's price and distance, not the LLM's reading of them. An unknown price
    ('dohodou') or distance passes: the push shows what is known.
    """
    price, max_price = listing.get("price_czk"), watch.get("max_price")
    if max_price and price and price > max_price:
        return f"price {kc(price)} > max {kc(max_price)}"
    km, radius = listing.get("distance_km"), watch.get("radius_km")
    if radius and km is not None and km > radius:
        where = listing.get("location") or "distance"
        return f"{where} {km:.0f} km > {radius:g} km"
    categories = watch.get("categories") or []
    if categories and listing.get("section") not in categories:
        return f"section {listing.get('section')} not in {', '.join(categories)}"
    return None


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
            skipped = within_limits(watch, listing)
            if skipped:
                repo.record_watch_hit(conn, watch["id"], listing["id"], False, {"skipped": skipped})
                continue
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


def format_watch_push(hit: dict) -> tuple[str, str]:
    """(title, message) for one watch-hit push."""
    title = f"👀 {hit['query']}"[:90]
    lines = [hit["title"][:200]]
    price = hit["price_czk"]
    lines.append(kc(price or None))
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


def watch_hit_actions(ntfy, watch_id: int, listing_id: int) -> list[dict]:
    """ "Stop"/👍/👎 buttons on a watch-hit push: same HTTP-action mechanism as the deals feedback
    buttons (`alerts.Ntfy.feedback_action`), but posting a plain-text command back to the watch
    topic instead of a JSON body to the feedback topic -- the listener (`handle`) reads it like any
    other message. Needs a write-capable token for that topic (`ntfy.watch_write_token`, separate
    from the read-only `watch_token` the listener subscribes with); no token, no buttons.
    """
    token = getattr(ntfy, "watch_write_token", "")
    if not token or not ntfy.watch_topic:
        return []
    url = f"{ntfy.public_url}/{ntfy.watch_topic}"
    hit_id = f"{watch_id}-{listing_id}"

    def action(label: str, command: str) -> dict:
        return {
            "action": "http",
            "label": label,
            "url": url,
            "method": "POST",
            "headers": {"Authorization": f"Bearer {token}"},
            "body": command,
            "clear": True,
        }

    return [
        action("Stop", f"stop {watch_id}"),
        action("👍", f"vote {hit_id} up"),
        action("👎", f"vote {hit_id} down"),
    ]


def push_matches(conn, ntfy, now: datetime, quiet_start: int, quiet_end: int, max_per_day: int) -> int:
    """Push matched-but-not-pushed hits on the deals topic: quiet hours, a separate daily cap."""
    from deal_hunter.alerts import PRAGUE, first_image, is_quiet
    from deal_hunter.db import repository as repo
    from deal_hunter.db import watch_repo

    if ntfy is None:
        return 0
    if is_quiet(now, quiet_start, quiet_end):
        return 0
    day_start = now.astimezone(PRAGUE).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = repo.watch_pushes_since(conn, day_start)
    muted = watch_repo.muted_watch_ids(conn)
    pushed = 0
    for hit in repo.pending_watch_matches(conn):
        if sent_today >= max_per_day:
            break
        if hit["watch_id"] in muted:
            # still matched, just snoozed -- left pending so it is pushed once the mute lapses
            continue
        # matched earlier, but the price may have gone up since (or the match predates the limits)
        skipped = within_limits(hit, hit)
        if skipped:
            repo.unmatch_watch_hit(conn, hit["watch_id"], hit["listing_id"], skipped)
            continue
        title, message = format_watch_push(hit)
        actions = watch_hit_actions(ntfy, hit["watch_id"], hit["listing_id"])
        try:
            ntfy.publish(title, message, 3, ["eyes"], hit["url"], actions=actions, attach=first_image(hit))
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


def _find_watch(rows: list[dict], command: Command) -> dict | None:
    """The watch a `mute` command targets: by id, or the first whose query contains the words
    (case-/accent-insensitive). Ambiguous free-text targets just hit the first match -- good enough
    for a handful of personal watches, not worth a disambiguation round-trip over ntfy."""
    if command.watch_id is not None:
        return next((w for w in rows if w["id"] == command.watch_id), None)
    target = fold(command.query or "")
    return next((w for w in rows if target in fold(w["query"])), None)


def execute(conn, command: Command, created_by: str | None = None) -> str:
    """Run a parsed command against the database. Returns the reply text (sent on the deals topic)."""
    from deal_hunter.db import repository as repo
    from deal_hunter.db import watch_repo

    if command.kind == "help":
        return HELP_TEXT
    if command.kind == "list":
        rows = repo.list_watches(conn)
        if not rows:
            return "No watches yet. " + HELP_TEXT
        muted = watch_repo.muted_watch_ids(conn)
        lines = []
        for w in rows:
            bits = [f"#{w['id']}", w["query"]]
            if w["max_price"]:
                bits.append(f"max {w['max_price']} Kč")
            if w["radius_km"]:
                bits.append(f"{w['radius_km']:g} km")
            if (w.get("keywords") or {}).get("exclude"):
                bits.append("-" + " -".join(w["keywords"]["exclude"]))
            if w["tag"]:
                bits.append(f"#{w['tag']}")
            if not w["active"]:
                bits.append("(stopped)")
            elif w["id"] in muted:
                bits.append("(muted)")
            lines.append(" · ".join(bits))
        return "\n".join(lines)
    if command.kind == "stop":
        ok = repo.set_watch_active(conn, command.watch_id, False)
        return f"Stopped watch #{command.watch_id}." if ok else f"No watch #{command.watch_id}."
    if command.kind == "start":
        ok = repo.set_watch_active(conn, command.watch_id, True)
        return f"Resumed watch #{command.watch_id}." if ok else f"No watch #{command.watch_id}."
    if command.kind == "mute":
        watch = _find_watch(repo.list_watches(conn), command)
        if watch is None:
            return f"No watch matching {command.query or command.watch_id!r}."
        watch_repo.set_watch_muted(conn, watch["id"], command.mute_days)
        return f"Muted watch #{watch['id']} for {command.mute_days:g} day(s): {watch['query']}."
    if command.kind == "vote":
        ok = watch_repo.record_vote(conn, command.watch_id, command.listing_id, command.vote)
        return "Thanks for the feedback." if ok else f"No watch hit {command.watch_id}-{command.listing_id}."
    if command.kind == "add":
        keywords = keywords_from_query(command.query)
        if command.excludes:
            keywords["exclude"] = list(keywords.get("exclude") or []) + list(command.excludes)
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
        if command.excludes:
            bits.append("-" + " -".join(command.excludes))
        if command.tag:
            bits.append(f"#{command.tag}")
        return " · ".join(bits)
    return f"Didn't understand that ({command.error}). {HELP_TEXT}"


def handle(ctx, event: dict) -> str | None:
    """One event from the watch topic. `ctx` needs `.conn` and `.ntfy`. None for non-messages."""
    if event.get("event") != "message":
        return None
    text = event.get("message", "")
    command = parse_command(text)
    reply = execute(ctx.conn, command, created_by=event.get("id"))
    ctx.ntfy.publish(f"watch: {text[:60]}", reply, 2, ["mag"])
    return reply


def listen_once(ctx, stop=lambda: False) -> int:
    return listener.listen_once(
        ctx.conn, ctx.ntfy.stream_watch, lambda event: handle(ctx, event), LAST_ID_KEY, "watch", stop
    )


def forever(ctx, sleep=time.sleep) -> None:
    listener.forever(lambda: listen_once(ctx), "watch", sleep)
