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

Disk pricing (Phase 9 review fix). 3.5" HDDs priced by class + capacity ('hdd35 enterprise 16tb')
rarely got the 4 ads a fair price needs: most of a class's listings are spread across 6-8 different
capacities. For a 3.5" HDD with a known class (nas / enterprise / desktop / surveillance) and desktop
externals (also 3.5"; storage.pooled_key), the pricing key drops the capacity ('hdd35 enterprise') and
the estimate is run on **Kč/TB** (each sample's price / capacity_tb) instead of Kč: same dedupe,
weighting, outliers, median/IQR/confidence as above, just on a per-TB figure (`fair_price(...,
per_tb=True)`). A listing's fair price is that Kč/TB median x its own capacity_tb (`FairPrice.
for_listing`); quantity is handled as before (storage.unit_price splits a lot price per drive first).
SAS drives (a market of their own, and rarer now that the interface call is stricter, see storage.py)
and 3.5" drives with no known class keep their old capacity-specific key, unpooled - as do 2.5" and SSD.

Whether bigger drives cost more per TB: checked against the curated Zboží.cz new-price pages
(new_price_pages.yaml) on 2026-10-10 - nas class 8/12/16 TB averaged ~1 135 / 1 037 / 1 082 Kč/TB,
enterprise 12/16/18/20 TB ~1 114 / 947 / 972 / 1 066 Kč/TB: noisy and not monotonic (16 TB enterprise
was the *cheapest* per TB, not the dearest). That doesn't support a capacity slope, so disks are priced
**flat per class**: no adjustment, unlike the RAM/SSD slopes above. If real data later shows a
consistent per-TB trend, the same slope machinery (PRIOR_SLOPE, estimate_adjustment) could be pointed
at capacity_tb instead of being left out; for now a slope fit to this shape of data would just be noise
dressed up as a model.

Priors for thin data (Phase 8). A model with fewer than `confident_n` samples (the alert gate's
HUNTER_ALERT_MIN_SAMPLES, 15) is shrunk toward a prior, in log space, with the prior counting as
PRIOR_N (5) samples:

    w = n / (n + PRIOR_N)          fair = exp(w * ln(median) + (1 - w) * ln(prior))

- GPUs and disks: prior = current new price (the lowest of Zboží.cz and Datart.cz, fetched in the last 30 days) x the typical
  used/new ratio of that category (disks: 0.6 until there is data, see NEW_RATIO_FALLBACK_BY_CATEGORY). The ratio is the median of median/new over models that have both a new price and at least
  `confident_n` samples (so no model's prior is built from its own shrunk value); with fewer than 3
  such models it is 0.75 (current cards hold their value; 0.6 pulled fresh models 12-16 % too low).
  Clipped to [0.35, 0.9]. For a pooled disk class the "new price" is itself a Kč/TB figure: the median
  Kč/TB across that class's Zboží keys (disk_new_per_tb), one per capacity - same units as the class's
  median, so the ratio and the shrink both just work. No confidence boost, and no prior at all when
  the class is already `over_new`: the same rules as GPUs, just in Kč/TB instead of Kč.
- Office hardware: prior = the median per-unit final price of ended state auction lots of that model
  (wholesale, as-is) x AUCTION_UPLIFT (1.5: tested, cleaned, sold one by one). Each auction lot counts
  as one sample, at most 3 (instead of PRIOR_N): a few lots are weak evidence. With at least 2 lots the
  per-unit auction median is also a floor: a Bazoš asking price below what a state auction fetches as-is
  is not a fair price for a working unit.
- A prior that disagrees with the data by more than 2x (either way) is ignored and noted: the new
  price page is probably the wrong product, or the market is odd.
- With a prior, the gate counts n + the prior's weight (the "effective n"), so a model with 10 ads
  and a new price passes the 15-sample gate. The 7-days-of-data rule is not touched. The confidence
  level is NOT raised: it stays what the ads alone support, since a prior moves the estimate but is
  not evidence about this market. At `confident_n` and above the median stands alone.
- A model whose own ads already sit at >= 95 % of new (the sanity flag below) gets no prior: the
  data or the mapping is off, and shrinking toward the new price would only hide that.

Sanity check: a used fair price (or the raw median, before a prior) at 95 % or more of the new price (Zboží, else the launch MSRP converted
with HUNTER_USD_CZK and 21 % VAT) means the estimate or the mapping is off; candidates of that model are
dropped (FairPrice.over_new).

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

CONFIDENT_N = 15  # below this many samples a prior is used (the alert gate's default)
PRIOR_N = 5.0  # a new-price prior counts as this many samples
NEW_RATIO_FALLBACK = 0.75  # used/new before enough models have both (GPUs and anything not listed below)
# Used disks lose more against new than graphics cards do (no warranty, unknown hours): a guess until
# there are 3 disk keys with 15 samples and a Zboží price.
NEW_RATIO_FALLBACK_BY_CATEGORY = {"hdd": 0.6}
NEW_RATIO_CLIP = (0.35, 0.9)
NEW_RATIO_MIN_MODELS = 3
AUCTION_UPLIFT = 1.5  # Bazoš retail of a working unit vs the state auction's as-is wholesale price
AUCTION_MAX_WEIGHT = 3.0
MAX_PRIOR_GAP = math.log(2)  # a prior more than 2x away from the data is ignored
SANITY_NEW_SHARE = 0.95  # used fair >= 95 % of new: something is wrong
CZ_VAT = 1.21


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
    raw_median: int | None = None  # the data's own median when a prior moved `median`
    prior: int | None = None  # the prior the median was shrunk toward
    prior_source: str | None = None  # 'new' | 'auction'
    prior_weight: float = 0.0  # in samples
    prior_note: str | None = None  # why a prior was not used
    new_price: int | None = None  # Kč, lowest current new offer (Zboží, Datart) or the launch MSRP in Kč
    new_source: str | None = None  # 'zbozi' | 'datart' | 'msrp'
    new_detail: str | None = None  # '3 shops' / '$499 in 2020'
    over_new: bool = False  # the sanity flag: used fair >= 95 % of new
    per_tb: bool = False  # median/q25/q75/prior/new_price are Kč/TB, not Kč (pooled 3.5" HDD classes)

    @property
    def n_eff(self) -> float:
        """Samples plus the prior's weight: what the gate and the level count."""
        return self.n + self.prior_weight

    @property
    def iqr_rel(self) -> float:
        return (self.q75 - self.q25) / self.median if self.median else 0.0

    def for_listing(self, features: dict | None) -> int:
        """This model's fair price moved to one ad's RAM/SSD (when known), x capacity_tb if per_tb."""
        features = features or {}
        price = self.median * math.exp(spec_shift(features, self.ref, self.slopes))
        if self.per_tb:
            price *= features.get("capacity_tb") or 0
        return round(price)

    def new_for_listing(self, capacity_tb: float | None) -> int | None:
        """new_price as a Kč figure for a disk of this capacity (per_tb models quote it per TB)."""
        if not self.new_price:
            return None
        return round(self.new_price * capacity_tb) if self.per_tb and capacity_tb else self.new_price

    def params(self) -> dict:
        out = {"sold": self.sold, "ref": self.ref, "slopes": self.slopes}
        if self.prior:
            out.update(raw_median=self.raw_median, prior=self.prior, prior_source=self.prior_source)
            out["prior_weight"] = self.prior_weight
        if self.new_price:
            out.update(new_price=self.new_price, new_source=self.new_source)
        return out


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


def fair_price(
    samples: list[Sample], adjustment: Adjustment | None = None, per_tb: bool = False
) -> FairPrice | None:
    """The fair price of one model from its samples, or None with fewer than MIN_SAMPLES.

    `per_tb`: each sample's price is divided by its own capacity_tb first, so the result (median,
    q25, q75) is a Kč/TB figure for a pooled 3.5" HDD class, not a Kč figure for one capacity (see
    the module doc, "Disk pricing"). A sample without capacity_tb can't be put in Kč/TB and is
    dropped.
    """
    if not samples:
        return None
    first = samples[0]
    kept = dedupe(samples)
    if per_tb:
        kept = [x for x in kept if x.features.get("capacity_tb")]
    slopes = adjustment.slopes if adjustment else {}
    ref = reference_spec(kept) if slopes else {}
    base = [x.price / x.features["capacity_tb"] if per_tb else x.price for x in kept]
    adjusted = [p * math.exp(-spec_shift(x.features, ref, slopes)) for p, x in zip(base, kept)]
    weights = [x.weight for x in kept]
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
        per_tb=per_tb,
    )


def estimate_all(samples: list[Sample]) -> dict[str, FairPrice]:
    """Fair prices for every model with enough samples."""
    from deal_hunter import storage

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
            per_tb = category == "hdd" and storage.pooled_key(model)
            fp = fair_price(model_samples, adjustment, per_tb=per_tb)
            if fp is not None:
                out[model] = fp
    return out


# --- new prices and priors (Phase 8) ----------------------------------------------------------------

RETAIL_SOURCES = ("zbozi", "datart")  # NewPrice.source values that are live shop offers (priors apply)


@dataclass(frozen=True)
class NewPrice:
    """What a model costs new: the lowest current offer (Zboží.cz or Datart.cz), else the launch MSRP."""

    czk: int
    source: str  # zbozi | datart | msrp (the first two are RETAIL_SOURCES: live shop offers)
    detail: str = ""  # '3 shops' / '$499 in 2020'


def msrp_czk(usd: int, usd_czk: float) -> int:
    """A US launch MSRP (before sales tax) as a Czech shelf price: converted, plus 21 % VAT."""
    return round(usd * usd_czk * CZ_VAT)


def new_prices_for(
    models, zbozi: dict[str, tuple[int, int]], usd_czk: float, shop: dict[str, str] | None = None
) -> dict[str, NewPrice]:
    """{model: NewPrice} from fresh shop offers ({model: (Kč, shops)}; `shop` says who quoted each,
    Zboží if absent), else the GPU catalog MSRP."""
    from deal_hunter.normalize import launch_msrp

    out = {}
    for model in models:
        if model in zbozi:
            czk, shops = zbozi[model]
            out[model] = NewPrice(
                czk, (shop or {}).get(model, "zbozi"), f"{shops} shop{'s' if shops != 1 else ''}"
            )
        elif (m := launch_msrp(model)) is not None:
            usd, year = m
            out[model] = NewPrice(msrp_czk(usd, usd_czk), "msrp", f"${usd} in {year}")
    return out


def disk_new_per_tb(
    zbozi: dict[str, tuple[int, int]], shop: dict[str, str] | None = None
) -> dict[str, NewPrice]:
    """Per-class Kč/TB new price, keyed by the pooled pricing key ('hdd35 nas').

    Each key in `zbozi` (a Zboží page or a Datart row, see `shop`) prices one capacity ('hdd35 nas 8tb', 'hdd35 nas 12tb', ...);
    dividing by that capacity and taking the median across a class's keys gives the new Kč/TB that
    matches the pooled class's own Kč/TB median (see the module doc, "Disk pricing").
    """
    from deal_hunter import storage

    by_class: dict[str, list[tuple[float, int]]] = defaultdict(list)
    quoted_by: dict[str, set[str]] = defaultdict(set)
    for key, (czk, shops) in zbozi.items():
        split = storage.split_capacity_key(key)
        if split is None:
            continue
        base, tb = split
        if tb and storage.pooled_key(base):
            by_class[base].append((czk / tb, shops))
            quoted_by[base].add((shop or {}).get(key, "zbozi"))
    out = {}
    for base, rows in by_class.items():
        shops = sum(sh for _, sh in rows)
        out[base] = NewPrice(
            round(_median([v for v, _ in rows])),
            quoted_by[base].pop() if len(quoted_by[base]) == 1 else "zbozi",  # a mix is shown as Zboží
            f"{len(rows)} key{'s' if len(rows) != 1 else ''}, {shops} shop{'s' if shops != 1 else ''}",
        )
    return out


def used_new_ratio(
    prices: dict[str, FairPrice], new: dict[str, NewPrice], confident_n: int, category: str | None = None
) -> float:
    """Typical used/new price ratio from models with enough samples and a current (Zboží) new price.

    Per category (GPUs and disks age differently); `category=None` pools everything.
    """
    ratios = sorted(
        fp.median / new[m].czk
        for m, fp in prices.items()
        if m in new
        and new[m].source in RETAIL_SOURCES
        and fp.n >= confident_n
        and fp.prior is None
        and (category is None or fp.category == category)
    )
    if len(ratios) < NEW_RATIO_MIN_MODELS:
        return NEW_RATIO_FALLBACK_BY_CATEGORY.get(category or "", NEW_RATIO_FALLBACK)
    mid = (
        ratios[len(ratios) // 2]
        if len(ratios) % 2
        else (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2
    )
    return min(NEW_RATIO_CLIP[1], max(NEW_RATIO_CLIP[0], mid))


def _median(values: list[float]) -> float:
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def shrink(median: float, n: int, prior: float, weight: float) -> float:
    """The median moved toward the prior in log space; the prior counts as `weight` samples."""
    w = n / (n + weight)
    return math.exp(w * math.log(median) + (1 - w) * math.log(prior))


def apply_priors(
    prices: dict[str, FairPrice],
    new: dict[str, NewPrice] | None = None,
    auction_units: dict[str, list[float]] | None = None,
    confident_n: int = CONFIDENT_N,
) -> float:
    """Shrink thin models toward a prior, attach new prices and the sanity flag (in place).

    `auction_units`: {model: [per-unit final prices of ended auction lots]}. Returns the GPU used/new ratio
    (each category gets its own, see used_new_ratio).
    See the module docstring for the rules.
    """
    new, auction_units = new or {}, auction_units or {}
    ratios = {c: used_new_ratio(prices, new, confident_n, c) for c in {fp.category for fp in prices.values()}}
    for model, fp in prices.items():
        ratio = ratios[fp.category]
        np_ = new.get(model)
        if np_ is not None:
            fp.new_price, fp.new_source, fp.new_detail = np_.czk, np_.source, np_.detail
        prior = weight = None
        source = None
        units = auction_units.get(model) or []
        if np_ is not None and np_.source in RETAIL_SOURCES:
            prior, weight, source = np_.czk * ratio, PRIOR_N, "new"
        elif units:
            anchor = _median(units)
            prior, weight, source = (
                anchor * AUCTION_UPLIFT,
                min(float(len(units)), AUCTION_MAX_WEIGHT),
                "auction",
            )
        over_new_raw = bool(fp.new_price and fp.median >= SANITY_NEW_SHARE * fp.new_price)
        if prior and fp.n < confident_n and over_new_raw:
            fp.prior_note = f"{source} prior not used: the ads are already >= 95 % of new"
        elif prior and fp.n < confident_n:
            if abs(math.log(fp.median / prior)) > MAX_PRIOR_GAP:
                fp.prior_note = f"{source} prior {round(prior)} ignored: more than 2x away from the data"
            else:
                fp.raw_median, fp.prior, fp.prior_source, fp.prior_weight = (
                    fp.median,
                    round(prior),
                    source,
                    weight,
                )
                fp.median = round(shrink(fp.median, fp.n, prior, weight))
                scale = fp.median / fp.raw_median
                fp.q25, fp.q75 = round(fp.q25 * scale), round(fp.q75 * scale)
                # confidence and level stay data-only: a prior is not evidence about this market
        if len(units) >= 2:
            floor = _median(units)
            if fp.median < floor:
                fp.raw_median = fp.raw_median or fp.median
                fp.median = round(floor)
                fp.prior_note = f"raised to the auction floor {round(floor)}"
        data_median = fp.raw_median or fp.median  # what the ads say, before any prior pulled it down
        if fp.new_price and max(fp.median, data_median) >= SANITY_NEW_SHARE * fp.new_price:
            fp.over_new = True
    return ratios.get("gpu", NEW_RATIO_FALLBACK)


def auction_unit_prices(lots: list[tuple[int, dict]]) -> dict[str, list[float]]:
    """{model: [per-unit final price]} from ended lots that hold one priced model only.

    `lots`: (final price, extraction dict). Lots mixing models, with unverified items, or with an
    item count that doesn't add up are skipped: their per-unit price can't be attributed.
    """
    out: dict[str, list[float]] = defaultdict(list)
    for final, x in lots:
        items = x.get("items") or []
        keys = {i.get("key") for i in items}
        if not final or not items or len(keys) != 1 or None in keys:
            continue
        if not all(i.get("verified", True) for i in items):
            continue
        units = sum(int(i.get("quantity") or 0) for i in items)
        stated = x.get("stated_units")
        if units <= 0 or (stated and stated != units):
            continue
        if any(i.get("condition") in ("broken", "partly_broken") for i in items):
            continue
        out[keys.pop()].append(final / units)
    return dict(out)


def run(
    conn, confident_n: int = CONFIDENT_N, usd_czk: float = 23.5, save: bool = True
) -> dict[str, FairPrice]:
    """Estimate every model from the database, apply the priors, store today's snapshot."""
    from deal_hunter.db import repository as repo

    prices = estimate_all(repo.price_samples(conn, WINDOW_DAYS))
    with_priors(conn, prices, confident_n, usd_czk)
    if save:
        repo.save_fair_prices(conn, list(prices.values()))
    return prices


def with_priors(conn, prices: dict[str, FairPrice], confident_n: int = CONFIDENT_N, usd_czk: float = 23.5):
    """New prices (Zboží, Datart, MSRP) and auction anchors from the database, applied to `prices`."""
    from deal_hunter import auctions
    from deal_hunter.db import repository as repo

    offers = repo.latest_new_offers(conn)
    zbozi_raw = {m: (czk, shops) for m, (czk, shops, _) in offers.items()}
    shop = {m: who for m, (_, _, who) in offers.items()}
    new = new_prices_for(prices, zbozi_raw, usd_czk, shop)
    new.update(disk_new_per_tb(zbozi_raw, shop))
    lots = []
    for final, result in repo.sold_lot_extractions(conn):
        for item in result.get("items") or []:  # map again: normalizer improvements apply
            item["key"], item["category"] = auctions.map_model(
                item.get("model") or "", item.get("kind") or ""
            )
        lots.append((final, result))
    return apply_priors(prices, new, auction_unit_prices(lots), confident_n)
