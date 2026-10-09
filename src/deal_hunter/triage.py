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
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx

PROMPT_VERSION = 2  # bump when the prompt or schema changes: candidates are triaged again

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
- is_wanted_ad: the author wants to BUY (koupím, sháním, hledám), not sell.
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
        "is_wanted_ad": {"type": "boolean"},
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
        "is_wanted_ad",
        "specs",
        "one_line_verdict",
    ],
}

KIND = {"gpu": "graphics card", "office_pc": "ex-office PC", "office_laptop": "business laptop"}
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
    is_wanted_ad: bool = False
    specs: dict = field(default_factory=dict)
    one_line_verdict: str = ""

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


def _ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def check_signal(signal: str, quote: str, notes: list[str]) -> str | None:
    """Keep, relabel ('other') or reject a hard scam signal by what its quote says."""
    q = _ascii(quote)
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
    ):
        setattr(t, name, _bool(data.get(name, False)))
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
    specs = data.get("specs") if isinstance(data.get("specs"), dict) else {}
    t.specs = {
        "ram_gb": _int(specs.get("ram_gb")),
        "ssd_gb": _int(specs.get("ssd_gb")),
        "cpu": (str(specs["cpu"]).strip() or None) if specs.get("cpu") else None,
    }
    t.one_line_verdict = " ".join(str(data.get("one_line_verdict", "")).split())[:200]
    return t, notes


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
    raw, stats, error = "", {}, None
    for attempt in range(2):
        system = SYSTEM_PROMPT if attempt == 0 else SYSTEM_PROMPT + "\nAnswer with the JSON object only."
        raw, stats, cached = chat_cached(client, system, user, cache_dir)
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


def penalty(t: Triage) -> tuple[float, str | None]:
    """(multiplier, reason to drop). See README "Deal score" for the table."""
    if t.is_wanted_ad:
        return 0.0, "wanted ad"
    if t.for_parts or t.condition == "broken":
        return 0.0, "for parts / broken"
    hard = HARD_SCAM & set(t.scam_signals)
    if hard:
        return 0.0, "scam signals: " + ", ".join(sorted(hard))
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
    return round(factor, 3), None


def score(discount: float, confidence: float, t: Triage) -> tuple[float, float, str | None]:
    """(score, penalty, drop reason): score = discount x confidence x penalty."""
    factor, reason = penalty(t)
    return round(discount * confidence * factor, 4), factor, reason
