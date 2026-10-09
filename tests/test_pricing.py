"""Fair price v1 on synthetic data: dedup, sold weighting, outliers, confidence, RAM/SSD adjustment."""

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from deal_hunter import pricing
from deal_hunter.pricing import Sample, confidence, dedupe, estimate_adjustment, estimate_all, fair_price

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def s(
    i,
    price,
    model="rtx 3080 10gb",
    category="gpu",
    seller=None,
    title=None,
    age_h=24,
    gone_h=None,
    **features,
):
    first = NOW - timedelta(hours=age_h)
    return Sample(
        listing_id=i,
        model=model,
        category=category,
        price=price,
        title=title or f"ad {i}",
        seller=seller or f"seller {i}",
        first_seen=first,
        gone_at=first + timedelta(hours=gone_h) if gone_h is not None else None,
        gone_reason="http_410" if gone_h is not None else None,
        features=features,
    )


def test_weighted_quantile():
    assert pricing.weighted_quantile([1, 2, 3], [1, 1, 1], 0.5) == 2
    assert pricing.weighted_quantile([10, 20], [1, 1], 0.5) == 15
    assert pricing.weighted_quantile([10, 20], [1, 3], 0.5) == pytest.approx(17.5)  # midpoints 0.125, 0.625
    assert pricing.weighted_quantile([5], [1], 0.9) == 5
    assert pricing.weighted_quantile([1, 2, 3, 4], [1, 1, 1, 1], 0.0) == 1  # clamped to the ends


def test_median_and_iqr():
    fp = fair_price([s(i, p) for i, p in enumerate([9000, 10000, 10500, 11000, 12000])])
    assert fp.median == 10500 and fp.n == 5 and fp.n_listings == 5
    assert fp.q25 < fp.median < fp.q75


def test_too_few_samples():
    assert fair_price([s(i, 10000) for i in range(3)]) is None
    assert pricing.level(0.9, 3) == "none"


def test_outliers_dropped():
    prices = [10000, 10200, 9800, 10100, 9900, 500, 99999]  # '500' = typo, '99999' = whole PC
    fp = fair_price([s(i, p) for i, p in enumerate(prices)])
    assert fp.n == 5 and fp.n_listings == 7
    assert 9800 <= fp.median <= 10200


def test_reposts_collapse():
    same_seller = [s(i, 10000 + i * 50, seller="Pepa", age_h=i) for i in range(5)]  # one ad, reposted
    others = [s(10 + i, p) for i, p in enumerate([8000, 8200, 8400])]
    kept = dedupe(same_seller + others)
    assert len(kept) == 4
    assert next(k for k in kept if k.seller == "Pepa").listing_id == 0  # the most recent one
    # same title from different seller names, same price -> one; a different price -> a real second ad
    a = s(1, 5000, title="RTX 3080 Gigabyte Vision")
    b = s(2, 5000, title="RTX 3080  gigabyte vision!")
    c = s(3, 6000, title="RTX 3080 Gigabyte Vision")
    assert len(dedupe([a, b, c])) == 2


def test_fast_sales_weigh_double():
    slow = [s(i, 12000) for i in range(4)]
    fast = [s(10 + i, 9000, gone_h=20) for i in range(3)]
    gone_late = [s(20, 9000, gone_h=200)]
    assert all(x.sold_fast for x in fast) and not gone_late[0].sold_fast
    fp = fair_price(slow + fast)
    assert fp.sold == 3
    assert fp.median < 12000  # 3 sold x 2 = 6 votes vs 4 asking-only votes
    unweighted = fair_price(slow + [s(10 + i, 9000) for i in range(3)])
    assert unweighted.median == 12000


def test_confidence_grows_with_n_and_shrinks_with_spread():
    assert confidence(20, 0.1) > confidence(5, 0.1)
    assert confidence(20, 0.1) > confidence(20, 0.5)
    assert pricing.level(confidence(30, 0.1), 30) == "high"
    assert pricing.level(confidence(8, 0.2), 8) == "medium"
    assert pricing.level(confidence(5, 0.6), 5) == "low"
    assert pricing.level(0.9, 5) == "low" and pricing.level(0.9, 6) == "medium"  # few samples cap the level
    assert pricing.level(0.9, 9) == "medium" and pricing.level(0.9, 10) == "high"
    tight = fair_price([s(i, 10000 + 100 * (i % 3)) for i in range(20)])
    loose = fair_price([s(i, 5000 + 1000 * (i % 10)) for i in range(20)])
    assert tight.level == "high" and loose.confidence < tight.confidence


def test_models_are_priced_separately():
    samples = [s(i, 10000, model="rtx 3080 10gb") for i in range(5)] + [
        s(10 + i, 5000, model="rtx 3060 12gb") for i in range(5)
    ]
    prices = estimate_all(samples)
    assert {m: p.median for m, p in prices.items()} == {"rtx 3080 10gb": 10000, "rtx 3060 12gb": 5000}
    assert all(not p.slopes for p in prices.values())  # no RAM/SSD adjustment for GPUs


def synthetic_office(seed=1, n=60, ram_slope=0.15, ssd_slope=0.05):
    """T480s and X280s whose prices really do rise ~16 % per RAM doubling and ~5 % per SSD doubling."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        model, base = ("thinkpad t480", 6000) if i % 2 else ("thinkpad x280", 5000)
        ram, ssd = rng.choice([8, 16, 32]), rng.choice([256, 512, 1024])
        price = base * math.exp(ram_slope * math.log2(ram / 8) + ssd_slope * math.log2(ssd / 256))
        price *= math.exp(rng.gauss(0, 0.05))
        out.append(s(i, round(price), model=model, category="office_laptop", ram_gb=ram, ssd_gb=ssd))
    return out


def test_office_adjustment_recovers_slopes():
    adj = estimate_adjustment(synthetic_office())
    assert adj.pairs == 60
    # shrunk toward the prior (0.12 / 0.06) with the weight of 20 samples, so close but not exact
    assert 0.13 < adj.slopes["ram_gb"] < 0.16
    assert 0.04 < adj.slopes["ssd_gb"] < 0.07


def test_office_adjustment_without_data_is_the_prior():
    adj = estimate_adjustment([s(1, 5000, model="thinkpad t480", category="office_laptop")])
    assert adj.slopes == pricing.PRIOR_SLOPE and adj.pairs == 0


def test_office_adjustment_is_clipped():
    adj = estimate_adjustment(synthetic_office(ram_slope=1.5, n=200))
    assert adj.slopes["ram_gb"] == pricing.MAX_SLOPE
    adj = estimate_adjustment(synthetic_office(ram_slope=-0.5, n=200))
    assert adj.slopes["ram_gb"] == 0.0


def test_office_fair_price_per_spec():
    prices = estimate_all(synthetic_office())
    t480 = prices["thinkpad t480"]
    ram, ssd = t480.ref["ram_gb"], t480.ref["ssd_gb"]
    assert ram in (8, 16, 32) and ssd in (256, 512, 1024)
    # at the reference spec, the fair price is the base price moved to that RAM/SSD
    expected = 6000 * math.exp(0.15 * math.log2(ram / 8) + 0.05 * math.log2(ssd / 256))
    assert abs(t480.median - expected) / expected < 0.06
    # adjusted samples are tighter than the raw spread would be
    assert t480.iqr_rel < 0.12 and t480.level in ("medium", "high")
    small, big = (
        t480.for_listing({"ram_gb": 8, "ssd_gb": 256}),
        t480.for_listing({"ram_gb": 32, "ssd_gb": 1024}),
    )
    assert small < t480.median < big
    assert t480.for_listing({}) == t480.median  # spec unknown: no adjustment
