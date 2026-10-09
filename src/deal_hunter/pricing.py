"""Fair price v1: a robust, weighted median of asking prices per canonical model.

For every model (the normalizer's pricing key: 'rtx 3080 10gb', 'thinkpad t480', ...):

1. Samples: matched ads with a numeric price (`price_kind = 'fixed'`) seen in the last 60 days.
2. Reposts are collapsed to one sample: same model and either the same seller or the same title,
   at a price within 5 %. A seller who deletes and re-posts an ad every few days doesn't get to
   vote five times.
3. Weights: an ad that disappeared within 72 h of first being seen probably sold at about its
   asking price, so it counts double. Everything else counts once.
4. Outliers: anything below 10 % or above 300 % of the first-pass median is dropped (typos,
   "1 Kč", a whole PC listed as a GPU, a box without the card...).
5. Estimate: the weighted median of what is left, with the weighted 25th/75th percentiles as the
   spread (IQR).
6. Confidence (0..1) = n / (n + 5) * 1 / (1 + (IQR / median / 0.25)^2): more samples and a tighter
   spread mean more trust. Levels: high >= 0.6 (and n >= 10), medium >= 0.35 (and n >= 6), low
   otherwise; fewer than 4 samples gives no estimate at all.

Office hardware also varies with RAM and SSD size. Per category (office PCs, office laptops) one
pooled slope per doubling of RAM and of SSD is estimated from within-model differences:
log(price) - model mean against log2(GB) - model mean, a two-variable least squares fit through the
origin. It is shrunk toward a prior (RAM +12 %, SSD +6 % per doubling) with the weight of 20
samples, and clipped to [0, 0.35]. Each sample is moved to its model's reference spec (the median
RAM/SSD of that model's ads) before taking the median. A specific ad's fair price is then moved
back to its own spec. Missing specs are left unadjusted.

These are asking prices, not sale prices; see the README.
"""

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

WINDOW_DAYS = 60
SOLD_HOURS = 72  # gone within this long after first seen = probably sold
SOLD_WEIGHT = 2.0
LOW_CUT, HIGH_CUT = 0.10, 3.0  # outliers, relative to the first-pass median
MIN_SAMPLES = 4
REPOST_PRICE_TOLERANCE = 0.05
LEVELS = (("high", 0.6), ("medium", 0.35), ("low", 0.0))
LEVEL_MIN_N = {"high": 10, "medium": 6, "low": MIN_SAMPLES}  # a tight spread from 4 ads is still luck
LEVEL_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}

PRIOR_SLOPE = {"ram_gb": 0.12, "ssd_gb": 0.06}  # log-price change per doubling, before any data
PRIOR_WEIGHT = 20.0
MAX_SLOPE = 0.35
SPEC_KEYS = ("ram_gb", "ssd_gb")


@dataclass
class Sample:
    listing_id: int
    model: str
    category: str
    price: int
    title: str
    seller: str | None
    first_seen: datetime
    gone_at: datetime | None = None
    gone_reason: str | None = None
    features: dict = field(default_factory=dict)

    @property
    def sold_fast(self) -> bool:
        return (
            self.gone_at is not None
            and self.gone_reason is not None
            and self.gone_at - self.first_seen <= timedelta(hours=SOLD_HOURS)
        )

    @property
    def weight(self) -> float:
        return SOLD_WEIGHT if self.sold_fast else 1.0


@dataclass
class Adjustment:
    """Log-price slope per doubling of RAM / SSD for one category (empty for GPUs)."""

    slopes: dict[str, float] = field(default_factory=dict)
    pairs: int = 0  # samples the slopes were estimated from


@dataclass
class FairPrice:
    model: str
    category: str
    n: int  # samples used (after dedup and outlier removal)
    n_listings: int  # ads before dedup/outliers
    median: int
    q25: int
    q75: int
    confidence: float
    level: str
    sold: int = 0  # samples that went fast (weighted double)
    ref: dict = field(default_factory=dict)  # reference spec the median is quoted at (office)
    slopes: dict = field(default_factory=dict)

    @property
    def iqr_rel(self) -> float:
        return (self.q75 - self.q25) / self.median if self.median else 0.0

    def for_listing(self, features: dict | None) -> int:
        """This model's fair price moved to one ad's RAM/SSD (when known)."""
        return round(self.median * math.exp(spec_shift(features or {}, self.ref, self.slopes)))

    def params(self) -> dict:
        return {"sold": self.sold, "ref": self.ref, "slopes": self.slopes}


def norm_title(title: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", title.lower()).strip()


def dedupe(samples: list[Sample]) -> list[Sample]:
    """One sample per repost group (same model; same seller or same title; price within 5 %).

    The most recently seen ad of a group is kept.
    """
    kept: list[Sample] = []
    for s in sorted(samples, key=lambda s: s.first_seen, reverse=True):
        title = norm_title(s.title)
        seller = (s.seller or "").strip().lower()
        duplicate = any(
            k.model == s.model
            and ((seller and seller == (k.seller or "").strip().lower()) or norm_title(k.title) == title)
            and abs(k.price - s.price) <= REPOST_PRICE_TOLERANCE * max(k.price, s.price)
            for k in kept
        )
        if not duplicate:
            kept.append(s)
    return kept


def weighted_quantile(values: list[float], weights: list[float], q: float) -> float:
    """Quantile of a weighted sample, interpolating between the weight midpoints."""
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    if not pairs or total <= 0:
        raise ValueError("empty sample")
    if len(pairs) == 1:
        return pairs[0][0]
    positions, acc = [], 0.0
    for _, w in pairs:
        positions.append((acc + w / 2) / total)
        acc += w
    target = min(max(q, positions[0]), positions[-1])
    for i in range(1, len(pairs)):
        if positions[i] >= target:
            lo, hi = positions[i - 1], positions[i]
            frac = 0.0 if hi == lo else (target - lo) / (hi - lo)
            return pairs[i - 1][0] + frac * (pairs[i][0] - pairs[i - 1][0])
    return pairs[-1][0]


def confidence(n: int, iqr_rel: float) -> float:
    return (n / (n + 5)) * (1 / (1 + (iqr_rel / 0.25) ** 2))


def level(conf: float, n: int) -> str:
    if n < MIN_SAMPLES:
        return "none"
    for name, cut in LEVELS:
        if conf >= cut and n >= LEVEL_MIN_N[name]:
            return name
    return "low"


def spec_shift(features: dict, ref: dict, slopes: dict) -> float:
    """Log-price difference between `features` and the reference spec."""
    shift = 0.0
    for key, slope in slopes.items():
        if features.get(key) and ref.get(key):
            shift += slope * (math.log2(features[key]) - math.log2(ref[key]))
    return shift


def reference_spec(samples: list[Sample]) -> dict:
    """The median RAM/SSD among a model's ads, rounded to a power of two."""
    ref = {}
    for key in SPEC_KEYS:
        values = sorted(s.features[key] for s in samples if s.features.get(key))
        if values:
            mid = values[len(values) // 2]
            ref[key] = 2 ** round(math.log2(mid))
    return ref


def estimate_adjustment(samples: list[Sample]) -> Adjustment:
    """Pooled RAM/SSD slopes for one category, from within-model differences (see module doc)."""
    by_model: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        if all(s.features.get(k) for k in SPEC_KEYS):
            by_model[s.model].append(s)
    rows = []  # (d_ram, d_ssd, d_logprice)
    for group in by_model.values():
        if len(group) < 3:
            continue
        logs = [math.log(s.price) for s in group]
        rams = [math.log2(s.features["ram_gb"]) for s in group]
        ssds = [math.log2(s.features["ssd_gb"]) for s in group]
        mp, mr, ms = sum(logs) / len(logs), sum(rams) / len(rams), sum(ssds) / len(ssds)
        rows += [(r - mr, d - ms, p - mp) for r, d, p in zip(rams, ssds, logs)]
    n = len(rows)
    est = {"ram_gb": PRIOR_SLOPE["ram_gb"], "ssd_gb": PRIOR_SLOPE["ssd_gb"]}
    if n >= 6:
        a = sum(r * r for r, _, _ in rows)
        b = sum(r * d for r, d, _ in rows)
        c = sum(d * d for _, d, _ in rows)
        y1 = sum(r * p for r, _, p in rows)
        y2 = sum(d * p for _, d, p in rows)
        det = a * c - b * b
        if det > 1e-9:
            est = {"ram_gb": (c * y1 - b * y2) / det, "ssd_gb": (a * y2 - b * y1) / det}
    w = n / (n + PRIOR_WEIGHT)
    slopes = {k: round(min(MAX_SLOPE, max(0.0, w * est[k] + (1 - w) * PRIOR_SLOPE[k])), 4) for k in SPEC_KEYS}
    return Adjustment(slopes, n)


def fair_price(samples: list[Sample], adjustment: Adjustment | None = None) -> FairPrice | None:
    """The fair price of one model from its samples, or None with fewer than MIN_SAMPLES."""
    if not samples:
        return None
    first = samples[0]
    kept = dedupe(samples)
    slopes = adjustment.slopes if adjustment else {}
    ref = reference_spec(kept) if slopes else {}
    adjusted = [s.price * math.exp(-spec_shift(s.features, ref, slopes)) for s in kept]
    weights = [s.weight for s in kept]
    if len(kept) < MIN_SAMPLES:
        return None
    rough = weighted_quantile(adjusted, weights, 0.5)
    inliers = [
        (p, w, s) for p, w, s in zip(adjusted, weights, kept) if LOW_CUT * rough <= p <= HIGH_CUT * rough
    ]
    if len(inliers) < MIN_SAMPLES:
        return None
    prices = [p for p, _, _ in inliers]
    ws = [w for _, w, _ in inliers]
    median = weighted_quantile(prices, ws, 0.5)
    q25, q75 = weighted_quantile(prices, ws, 0.25), weighted_quantile(prices, ws, 0.75)
    n = len(inliers)
    conf = confidence(n, (q75 - q25) / median if median else 1.0)
    return FairPrice(
        model=first.model,
        category=first.category,
        n=n,
        n_listings=len(samples),
        median=round(median),
        q25=round(q25),
        q75=round(q75),
        confidence=round(conf, 3),
        level=level(conf, n),
        sold=sum(1 for _, _, s in inliers if s.sold_fast),
        ref=ref,
        slopes=slopes,
    )


def estimate_all(samples: list[Sample]) -> dict[str, FairPrice]:
    """Fair prices for every model with enough samples."""
    by_category: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by_category[s.category].append(s)
    out = {}
    for category, group in by_category.items():
        adjustment = estimate_adjustment(dedupe(group)) if category.startswith("office") else None
        by_model: dict[str, list[Sample]] = defaultdict(list)
        for s in group:
            by_model[s.model].append(s)
        for model, model_samples in by_model.items():
            fp = fair_price(model_samples, adjustment)
            if fp is not None:
                out[model] = fp
    return out


def run(conn) -> dict[str, FairPrice]:
    """Estimate every model from the database and store today's snapshot (for trends/digests)."""
    from deal_hunter.db import repository as repo

    prices = estimate_all(repo.price_samples(conn, WINDOW_DAYS))
    repo.save_fair_prices(conn, list(prices.values()))
    return prices
