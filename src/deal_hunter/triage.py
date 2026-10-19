"""LLM triage of candidate deals: a local model reads the (Czech) ad and fills in a fixed form.

Only candidates get here (priced well below fair, with a trustworthy fair price), so the GPU box
answers a handful of questions an hour, not every ad. The model runs in Ollama with a JSON schema as
the response `format`, temperature 0, and every answer is cached on disk by a hash of
model + prompt: re-running costs nothing and gives the same answer.

If Ollama is unreachable the ad stays queued and is retried later (with backoff); scraping never
waits for it. If the model answers but the answer can't be read twice in a row, the ad gets a safe
fallback (status 'failed', nothing pushed).
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from deal_hunter.text import fold

# Bump when the prompt or schema changes: candidates are triaged again.
PROMPT_VERSION = 4  # v4: price_mismatch (the asking price belongs to another item in the ad)

CONDITIONS = ("new", "like_new", "used", "worn", "broken")
SCAM_SIGNALS = ("shipping_only", "deposit_or_prepay", "too_good", "copied_text", "other")

SYSTEM_PROMPT = """You review second-hand ads from Bazos.cz (Czech classifieds) for a buyer who resells \
graphics cards and ex-office PCs and laptops. The ad is in Czech. Read it carefully and fill in the form.
Use only what the ad says; when it says nothing about a point, answer false / an empty list / null.

- condition: "new" (sealed or unused), "like_new" (barely used, no marks), "used" (normal wear), \
"worn" (heavy wear, cosmetic damage), "broken" (faulty or not working).
- for_parts: true if sold as not working, untested, for parts or repair ("na díly", "nefunkční", \
"na opravu", "neotestováno").
- defects: short English phrases for any defect the ad mentions (artefacts, noisy fan, dead pixel, \
weak battery, missing parts...). Empty if none.
- mining_suspected: the card was used for crypto mining (mentions mining/těžba/rig, or sells many \
identical cards). Always false for PCs and laptops.
- warranty_or_receipt: there is remaining warranty (záruka) or a receipt/invoice (účtenka, faktura, doklad).
- urgent_sale: the seller is in a hurry (spěchá, rychlé jednání, stěhování, nutno prodat).
- scam_signals: a list of {"signal", "quote"} for clear warning signs only. "quote" must be the exact \
words from the ad that show it; no quote, no signal. Most ads have none: an empty list is the normal answer.
  "shipping_only": the seller refuses personal pickup and only ships ("pouze zaslání", "osobní předání \
není možné"). "Pouze osobní odběr" means pickup ONLY: the opposite, never a signal.
  "deposit_or_prepay": the seller wants the price or a deposit paid before the buyer gets or sees the \
item ("záloha", "platba předem na účet"). Cash on delivery (dobírka), paying postage up front, or \
a shop invoice are normal and never a signal.
  "too_good": the price is far below the typical price AND something else is off. Never the price alone.
  "copied_text": the text is clearly copied from a shop or another ad, not the seller's own words.
  "other": another clear scam sign, e.g. contact only via WhatsApp/SMS on a foreign number, "don't call \
the number in the ad", links to outside payment sites.
- is_bundle: the ad sells more than one main item (a whole PC with the card, several cards, a laptop \
together with a monitor...). A charger, dock or box that comes with the item does not count.
- price_mismatch: {"mismatch", "quote"}. mismatch is true only when the ad lists several items or \
variants with their own prices and the asking price above belongs to a different (usually cheaper) one \
than the Item, e.g. "GTX 1060 3GB za 1500, 6GB za 2500" for a 6GB card. "quote" must be the exact words \
from the ad with those prices. Otherwise false and an empty quote: one item at one price is the normal case.
- is_wanted_ad: the author wants to BUY (koupím, sháním, hledám), not sell.
- ships: the seller offers to send the item (Zásilkovna, Balíkovna, PPL, poštou, dobírka, "pošlu", \
"zašlu"). False when the ad says pickup only, or says nothing about sending it.
- specs: for PCs and laptops, the RAM in GB, SSD size in GB and CPU model when stated; null otherwise.
- one_line_verdict: one short English sentence (max 15 words) telling the buyer whether this is worth \
a look and why."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "condition": {"type": "string", "enum": list(CONDITIONS)},
        "for_parts": {"type": "boolean"},
        "defects": {"type": "array", "items": {"type": "string"}},
        "mining_suspected": {"type": "boolean"},
        "warranty_or_receipt": {"type": "boolean"},
        "urgent_sale": {"type": "boolean"},
        "scam_signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "signal": {"type": "string", "enum": list(SCAM_SIGNALS)},
                    "quote": {"type": "string"},
                },
                "required": ["signal", "quote"],
            },
        },
        "is_bundle": {"type": "boolean"},
        "price_mismatch": {
            "type": "object",
            "properties": {"mismatch": {"type": "boolean"}, "quote": {"type": "string"}},
            "required": ["mismatch", "quote"],
        },
        "is_wanted_ad": {"type": "boolean"},
        "ships": {"type": "boolean"},
        "specs": {
            "type": "object",
            "properties": {
                "ram_gb": {"type": ["integer", "null"]},
                "ssd_gb": {"type": ["integer", "null"]},
                "cpu": {"type": ["string", "null"]},
            },
            "required": ["ram_gb", "ssd_gb", "cpu"],
        },
        "one_line_verdict": {"type": "string"},
    },
    "required": [
        "condition",
        "for_parts",
        "defects",
        "mining_suspected",
        "warranty_or_receipt",
        "urgent_sale",
        "scam_signals",
        "is_bundle",
        "price_mismatch",
        "is_wanted_ad",
        "ships",
        "specs",
        "one_line_verdict",
    ],
}

KIND = {
    "gpu": "graphics card",
    "office_pc": "ex-office PC",
    "office_laptop": "business laptop",
    "hdd": "hard disk / SSD",
}

# Hard disks get extra questions: what the seller says about the drive's health. Only for that
# category, so the GPU/office prompt and schema stay as they were (apart from `ships`).
DISK_PROMPT = """
This ad sells a hard disk (or SSD). Also fill in "disk" from what the ad says about the drive itself:
- power_on_hours: power-on hours from SMART ("Power On Hours", "naběháno", "spuštěno 100 hodin") as a \
number; null if not stated. Not the age in years.
- reallocated_or_pending: reallocated / pending / bad sectors ("přemapované", "realokované", "vadné \
sektory", "pending"): the number if given, true if the ad says there are some without a number, 0 if it \
says there are none ("0 vadných sektorů", "bez vadných sektorů"), null if it says nothing.
- health_pct: the health percentage a tool shows ("zdraví 98 %", "health 100%"); null if not stated.
- smart_screenshot_mentioned: the ad says a SMART readout / CrystalDiskInfo / HD Sentinel screenshot is \
attached or available.
- tested: the seller says the drive was tested (test, otestováno, prověřeno).
- warranty_until: the warranty end date as written ("4. 2. 2027", "03/2028"); null if none.
- from_nas_or_server: the drive ran in a NAS or server (NAS, server, TrueNAS, RAID, datacentrum)."""

DISK_SCHEMA = {
    "type": "object",
    "properties": {
        "power_on_hours": {"type": ["integer", "null"]},
        "reallocated_or_pending": {"type": ["integer", "boolean", "null"]},
        "health_pct": {"type": ["integer", "null"]},
        "smart_screenshot_mentioned": {"type": "boolean"},
        "tested": {"type": "boolean"},
        "warranty_until": {"type": ["string", "null"]},
        "from_nas_or_server": {"type": "boolean"},
    },
    "required": [
        "power_on_hours",
        "reallocated_or_pending",
        "health_pct",
        "smart_screenshot_mentioned",
        "tested",
        "warranty_until",
        "from_nas_or_server",
    ],
}


def schema_for(category: str | None) -> dict:
    """The triage form: the common one, plus "disk" for hard disks."""
    if category != "hdd":
        return RESPONSE_SCHEMA
    return {
        **RESPONSE_SCHEMA,
        "properties": {**RESPONSE_SCHEMA["properties"], "disk": DISK_SCHEMA},
        "required": [*RESPONSE_SCHEMA["required"], "disk"],
    }


def system_prompt(category: str | None) -> str:
    return SYSTEM_PROMPT + (DISK_PROMPT if category == "hdd" else "")


MAX_DESCRIPTION = 3000  # characters; long ads are mostly copy-pasted spec sheets


class LLMUnavailable(Exception):
    """Ollama can't be reached (down, CT stopped, model loading timed out): try again later."""


class OllamaClient:
    """A local model served by Ollama, forced into JSON by the response schema (temperature 0)."""

    def __init__(self, model: str, base_url: str, num_ctx: int = 8192, timeout: float = 300):
        self.model, self.base_url, self.num_ctx, self.timeout = model, base_url.rstrip("/"), num_ctx, timeout

    @property
    def cache_id(self) -> str:
        return self.model

    def chat(self, system: str, user: str, schema: dict) -> tuple[str, dict]:
        started = time.perf_counter()
        try:
            response = httpx.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "format": schema,
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0, "num_ctx": self.num_ctx},
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
        body = response.json()
        stats = {
            "prompt_tokens": body.get("prompt_eval_count", 0),
            "output_tokens": body.get("eval_count", 0),
            "seconds": round(time.perf_counter() - started, 2),
        }
        return body["message"]["content"], stats


def chat_cached(
    client, system: str, user: str, cache_dir: Path | None, schema: dict | None = None
) -> tuple[str, dict, bool]:
    """Same model + same prompt -> same answer from disk. `schema` defaults to the triage form."""
    model_id = getattr(client, "cache_id", client.model)
    key = hashlib.sha256(json.dumps([model_id, system, user]).encode()).hexdigest()[:24]
    path = cache_dir / f"{key}.json" if cache_dir else None
    if path and path.exists():
        hit = json.loads(path.read_text())
        return hit["raw"], hit["stats"], True
    raw, stats = client.chat(system, user, schema or RESPONSE_SCHEMA)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": client.model, "raw": raw, "stats": stats}))
    return raw, stats, False


def build_prompt(deal: dict) -> str:
    """The user message: what we know about the ad, then the ad itself."""
    price = f"{deal['price']:,} Kč".replace(",", " ")
    fair = f"{deal['fair']:,} Kč".replace(",", " ")
    where = " ".join(x for x in (deal.get("location"), deal.get("psc")) if x) or "not given"
    description = (deal.get("description") or "").strip() or "(no description)"
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION] + " [...]"
    return "\n".join(
        [
            f"Item: {deal['model']} ({KIND.get(deal['category'], deal['category'])})",
            f"Asking price: {price}. Typical asking price for this model on Bazos: about {fair}.",
            f"Location: {where}",
            "",
            f"Title: {deal['title']}",
            "Description:",
            description,
        ]
    )


@dataclass
class Triage:
    condition: str = "used"
    for_parts: bool = False
    defects: list[str] = field(default_factory=list)
    mining_suspected: bool = False
    warranty_or_receipt: bool = False
    urgent_sale: bool = False
    scam_signals: list[str] = field(default_factory=list)
    scam_quotes: dict = field(default_factory=dict)  # signal -> the ad's own words that show it
    is_bundle: bool = False
    price_mismatch: str = ""  # the ad's own words showing the asking price is another item's; "" = none
    is_wanted_ad: bool = False
    ships: bool = False  # the LLM's reading; alerts.score_deals asks a regex first (geo.shipping_offer)
    specs: dict = field(default_factory=dict)
    one_line_verdict: str = ""
    disk: dict = field(default_factory=dict)  # hard disks only: SMART and the like (DISK_PROMPT)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def parse_answer(raw: str) -> tuple[dict, list[str]]:
    """The model's JSON, even if wrapped in a code block or surrounded by text. Every fix is noted."""
    notes, text = [], raw.strip()
    if text.startswith("```"):
        notes.append("answer was wrapped in a Markdown code block")
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.partition("```")[0]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
        notes.append("answer had extra text around the JSON")
    if not isinstance(data, dict):
        raise TypeError(f"answer is not a JSON object: {data!r}")
    return data, notes


def _bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "ano", "1")
    return bool(value)


def _int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return int(value) if value > 0 else None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


NOTHING = {"none", "null", "n/a", "na", "no defects", "no defects mentioned", "-", "nothing", "unknown"}


def _strings(value) -> list[str]:
    """A clean list of strings; 'none'-style placeholders some models put in empty lists are dropped."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [str(v).strip() for v in value if v is not None]
    return [v for v in out if v and v.lower().rstrip(".") not in NOTHING]


CONDITION_SYNONYMS = {"likenew": "like_new", "like new": "like_new", "excellent": "like_new", "good": "used"}


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


# The two signals that drop a deal outright must be backed by words that actually say so.
SHIPPING_WORDS = re.compile(r"zasl|posl|post|balik|dobirk|doprav|ship|odesl|kuryr|zasil")
PICKUP_WORDS = re.compile(r"osobni (odber|prevzeti|predani|vyzvednuti)|osobne")
NEGATION = re.compile(r"\b(ne|neni|nelze|nemozn\w*|bez)\b")
PAYMENT_WORDS = re.compile(r"zaloh|predem|ucet|platb|zaplat|prevod|prepay|deposit|penize")


def check_signal(signal: str, quote: str, notes: list[str]) -> str | None:
    """Keep, relabel ('other') or reject a hard scam signal by what its quote says."""
    q = fold(quote)
    if signal == "shipping_only":
        if PICKUP_WORDS.search(q):
            if NEGATION.search(q):
                return signal  # 'osobní předání není možné': pickup refused
            notes.append(f"ignored 'shipping_only': {quote[:60]!r} offers personal pickup")
            return None
        if not SHIPPING_WORDS.search(q):
            notes.append(f"'shipping_only' relabelled 'other': {quote[:60]!r} says nothing about shipping")
            return "other"
    if signal == "deposit_or_prepay" and not PAYMENT_WORDS.search(q):
        notes.append(f"'deposit_or_prepay' relabelled 'other': {quote[:60]!r} says nothing about paying")
        return "other"
    return signal


def clean(data: dict, source: str = "") -> tuple[Triage, list[str]]:
    """Coerce whatever the model said into a Triage, writing down every fix.

    A scam signal only counts when its quote really appears in the ad (`source`): small models
    like to tick a box, and a made-up quote is easy to catch.
    """
    notes = []
    t = Triage()
    condition = str(data.get("condition", "")).strip().lower().replace("-", "_")
    condition = CONDITION_SYNONYMS.get(condition, condition)
    if condition in CONDITIONS:
        t.condition = condition
    else:
        notes.append(f"unknown condition {data.get('condition')!r}, assumed 'used'")
    for name in (
        "for_parts",
        "mining_suspected",
        "warranty_or_receipt",
        "urgent_sale",
        "is_bundle",
        "is_wanted_ad",
        "ships",
    ):
        setattr(t, name, _bool(data.get(name, False)))
    if t.ships and source and not SHIPPING_WORDS.search(fold(source)):
        t.ships = False  # nothing in the ad is about sending it
        notes.append("ignored 'ships': the ad says nothing about shipping")
    t.defects = _strings(data.get("defects"))[:10]
    plain_source = _plain(source)
    raw_signals = data.get("scam_signals") or []
    if not isinstance(raw_signals, list):
        raw_signals = [raw_signals]
    for item in raw_signals:
        signal, quote = (item.get("signal"), item.get("quote")) if isinstance(item, dict) else (item, "")
        signal = str(signal or "").strip().lower().replace(" ", "_")
        if signal not in SCAM_SIGNALS:
            notes.append(f"unknown scam signal {signal!r} -> 'other'")
            signal = "other"
        quote = " ".join(str(quote or "").split())
        if source and (len(_plain(quote)) < 4 or _plain(quote) not in plain_source):
            notes.append(f"ignored {signal!r}: quote {quote[:60]!r} is not in the ad")
            continue
        if source:
            signal = check_signal(signal, quote, notes)
            if signal is None:
                continue
        t.scam_signals.append(signal)
        t.scam_quotes.setdefault(signal, quote[:200])
    t.scam_signals = sorted(set(t.scam_signals))
    t.price_mismatch = check_price_mismatch(data.get("price_mismatch"), source, notes)
    specs = data.get("specs") if isinstance(data.get("specs"), dict) else {}
    t.specs = {
        "ram_gb": _int(specs.get("ram_gb")),
        "ssd_gb": _int(specs.get("ssd_gb")),
        "cpu": (str(specs["cpu"]).strip() or None) if specs.get("cpu") else None,
    }
    t.one_line_verdict = " ".join(str(data.get("one_line_verdict", "")).split())[:200]
    if isinstance(data.get("disk"), dict):
        t.disk = clean_disk(data["disk"], source, notes)
    return t, notes


PRICE_IN_QUOTE = re.compile(r"\d{1,3}(?:[ .\u00a0]\d{3})+|\d{3,}")


def check_price_mismatch(raw, source: str, notes: list[str]) -> str:
    """The quote, if the model's 'price is for another item' claim holds up: it must be in the ad and show
    at least two prices. Otherwise ""."""
    if not isinstance(raw, dict) or not _bool(raw.get("mismatch", False)):
        return ""
    quote = " ".join(str(raw.get("quote") or "").split())
    if source and (len(_plain(quote)) < 4 or _plain(quote) not in _plain(source)):
        notes.append(f"ignored price_mismatch: quote {quote[:60]!r} is not in the ad")
        return ""
    if len(PRICE_IN_QUOTE.findall(quote)) < 2:
        notes.append(f"ignored price_mismatch: quote {quote[:60]!r} doesn't show two prices")
        return ""
    return quote[:200]


# What the ad has to talk about for a disk answer to count (small models fill in plausible numbers).
SECTOR_WORDS = re.compile(r"sektor|realok|premapov|realloc|pending|bad block|vadn")
SMART_WORDS = re.compile(r"smart|crystal|sentinel|screen|vypis|cdi\b")
TESTED_WORDS = re.compile(r"test|prover")
WARRANTY_WORDS = re.compile(r"zaruk|warranty|garanc")
SERVER_WORDS = re.compile(r"\bnas\b|server|truenas|unraid|raid|datacent|synology|qnap")


def numbers(text: str) -> set[int]:
    """Every number in a text, with Czech thousands separators ('12 000', '12.000') joined."""
    t = re.sub(r"(?<=\d)[\s.](?=\d{3}\b)", "", text)
    return {int(n) for n in re.findall(r"\d+", t)}


def clean_disk(raw: dict, source: str, notes: list[str]) -> dict:
    """The disk answer, with every claim checked against the ad: numbers must be in it, flags need words."""
    text = fold(source)
    found = numbers(source)
    out: dict = {}
    hours = _int(raw.get("power_on_hours"))
    if hours is not None and source and hours not in found:
        notes.append(f"ignored power_on_hours {hours}: not in the ad")
        hours = None
    out["power_on_hours"] = hours
    realloc = raw.get("reallocated_or_pending")
    if isinstance(realloc, str):
        realloc = _bool(realloc) if not re.search(r"\d", realloc) else _int(realloc) or 0
    if realloc is not None and source and not SECTOR_WORDS.search(text):
        notes.append(f"ignored reallocated_or_pending {realloc!r}: the ad says nothing about sectors")
        realloc = None
    if (
        isinstance(realloc, int)
        and not isinstance(realloc, bool)
        and realloc > 0
        and source
        and realloc not in found
    ):
        notes.append(f"reallocated_or_pending {realloc} is not in the ad: kept as 'some'")
        realloc = True
    out["reallocated_or_pending"] = realloc
    health = _int(raw.get("health_pct"))
    if health is not None and (health > 100 or (source and health not in found)):
        notes.append(f"ignored health_pct {health}: not in the ad")
        health = None
    out["health_pct"] = health
    for name, words in (
        ("smart_screenshot_mentioned", SMART_WORDS),
        ("tested", TESTED_WORDS),
        ("from_nas_or_server", SERVER_WORDS),
    ):
        value = _bool(raw.get(name, False))
        if value and source and not words.search(text):
            notes.append(f"ignored {name!r}: nothing in the ad says so")
            value = False
        out[name] = value
    warranty = raw.get("warranty_until")
    warranty = " ".join(str(warranty).split())[:40] if warranty else None
    if warranty and source and not WARRANTY_WORDS.search(text):
        notes.append("ignored warranty_until: the ad doesn't mention a warranty")
        warranty = None
    out["warranty_until"] = warranty
    return out


FALLBACK = Triage(condition="used", one_line_verdict="(LLM triage failed; not checked)")


@dataclass
class Outcome:
    status: str  # done | failed
    result: Triage
    raw: str = ""
    notes: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    error: str | None = None


def triage(client, deal: dict, cache_dir: Path | None) -> Outcome:
    """Ask the model about one ad. Two attempts at a readable answer, then the safe fallback.

    Raises LLMUnavailable when Ollama can't be reached, so the caller can queue the ad for later.
    """
    user = build_prompt(deal)
    category = deal.get("category")
    base, schema = system_prompt(category), schema_for(category)
    raw, stats, error = "", {}, None
    for attempt in range(2):
        system = base if attempt == 0 else base + "\nAnswer with the JSON object only."
        raw, stats, cached = chat_cached(client, system, user, cache_dir, schema)
        stats = {**stats, "cached": cached}
        try:
            data, notes = parse_answer(raw)
            result, fixes = clean(data, f"{deal['title']}\n{deal.get('description') or ''}")
            return Outcome("done", result, raw, notes + fixes, stats)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            error = f"attempt {attempt + 1}: {type(exc).__name__}: {exc}"
    return Outcome("failed", FALLBACK, raw, [error or "unreadable answer"], stats, error)


# --- the deal score -----------------------------------------------------------------------------------

HARD_SCAM = {"shipping_only", "deposit_or_prepay"}


def penalty(t: Triage, features: dict | None = None) -> tuple[float, str | None]:
    """(multiplier, reason to drop). See docs/scoring.md for the table; `features` are the
    normalizer's (disks: interface, SMR)."""
    if t.is_wanted_ad:
        return 0.0, "wanted ad"
    if t.for_parts or t.condition == "broken":
        return 0.0, "for parts / broken"
    hard = HARD_SCAM & set(t.scam_signals)
    if hard:
        return 0.0, "scam signals: " + ", ".join(sorted(hard))
    if t.price_mismatch:
        return 0.0, f"the price is for another item in the ad: {t.price_mismatch!r}"
    factor = 1.0
    soft = set(t.scam_signals) - HARD_SCAM
    if soft:
        factor *= 0.6  # too_good / copied_text / other: suspicious, not damning (resellers copy text)
    if t.is_bundle:
        factor *= 0.5  # the fair price is for the item alone, so the discount is not comparable
    if t.condition == "worn":
        factor *= 0.7
    if t.defects:
        factor *= 0.8
    if t.mining_suspected:
        factor *= 0.8
    if t.warranty_or_receipt:
        factor *= 1.1
    if features is not None and features.get("capacity_tb") and "kind" in features:
        disk_factor, reason = disk_penalty(t.disk or {}, features)
        if reason:
            return 0.0, reason
        factor *= disk_factor
    return round(factor, 3), None


POWER_ON_LIMIT = 50_000  # hours (~5.7 years running): an old drive, whatever SMART says otherwise


def smart_known(disk: dict) -> bool:
    return (
        any(
            disk.get(k) not in (None, False)
            for k in ("power_on_hours", "health_pct", "smart_screenshot_mentioned")
        )
        or disk.get("reallocated_or_pending") is not None
    )


def disk_penalty(disk: dict, features: dict) -> tuple[float, str | None]:
    """Hard disks on top of the common penalties: (multiplier, reason to drop)."""
    if features.get("interface") == "sas":
        return 0.0, "SAS drive: not usable on this server (SATA controller, no SAS HBA)"
    realloc = disk.get("reallocated_or_pending")
    if realloc is True or (isinstance(realloc, int) and not isinstance(realloc, bool) and realloc > 0):
        return 0.0, "reallocated/pending sectors"
    factor = 1.0
    hours = disk.get("power_on_hours")
    if hours is not None and hours > POWER_ON_LIMIT:
        factor *= 0.6
    if not smart_known(disk):
        factor *= 0.85
    if features.get("smr"):
        factor *= 0.7
    return factor, None


def score(
    discount: float, confidence: float, t: Triage, features: dict | None = None
) -> tuple[float, float, str | None]:
    """(score, penalty, drop reason): score = discount x confidence x penalty."""
    factor, reason = penalty(t, features)
    return round(discount * confidence * factor, 4), factor, reason
