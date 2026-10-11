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


# --- Phase 8: new prices, priors, the sanity flag ----------------------------------------------------


def thin(model="rtx 3060 12gb", prices=(5000, 5200, 5400, 5600, 5800), category="gpu"):
    return estimate_all([s(i, p, model=model, category=category) for i, p in enumerate(prices)])


def test_shrink_is_a_weighted_geometric_mean():
    assert pricing.shrink(4000, 5, 9000, 5) == pytest.approx(6000)  # w = 0.5: sqrt(4000 x 9000)
    assert pricing.shrink(4000, 15, 9000, 5) == pytest.approx(4000**0.75 * 9000**0.25)
    assert pricing.shrink(4000, 0, 9000, 5) == pytest.approx(9000)


def test_msrp_and_zbozi_new_prices():
    assert pricing.msrp_czk(329, 23.5) == round(329 * 23.5 * 1.21)
    new = pricing.new_prices_for(
        ["rtx 3060 12gb", "rtx 3070", "rtx 3060", "thinkpad t480"], {"rtx 3070": (9990, 2)}, 23.5
    )
    assert new["rtx 3070"] == pricing.NewPrice(9990, "zbozi", "2 shops")  # a current offer beats the MSRP
    assert new["rtx 3060 12gb"].source == "msrp" and new["rtx 3060 12gb"].detail == "$329 in 2021"
    assert "rtx 3060" not in new and "thinkpad t480" not in new  # ambiguous VRAM / not a GPU


def test_thin_model_is_shrunk_toward_new_times_the_fallback_ratio():
    prices = thin()
    raw = prices["rtx 3060 12gb"].median
    new = {"rtx 3060 12gb": pricing.NewPrice(10000, "zbozi", "3 shops")}
    ratio = pricing.apply_priors(prices, new)
    fp = prices["rtx 3060 12gb"]
    assert ratio == pricing.NEW_RATIO_FALLBACK  # no model has both a new price and 15 samples
    assert fp.raw_median == raw and fp.prior == 7500 and fp.prior_source == "new"  # 10 000 x 0.75
    assert fp.median == round(pricing.shrink(raw, 5, 7500, pricing.PRIOR_N))
    assert raw < fp.median < 7500
    assert fp.n == 5 and fp.n_eff == 10  # the prior counts as 5 samples for the gate
    assert fp.new_price == 10000 and not fp.over_new


def test_prior_does_not_raise_confidence():
    before = thin()["rtx 3060 12gb"]
    prices = thin()
    pricing.apply_priors(prices, {"rtx 3060 12gb": pricing.NewPrice(10000, "zbozi", "3 shops")})
    fp = prices["rtx 3060 12gb"]
    assert fp.prior is not None
    assert fp.confidence == before.confidence and fp.level == before.level  # data-only


def test_no_prior_when_the_ads_are_already_near_new():
    prices = thin()  # median 5 400
    pricing.apply_priors(prices, {"rtx 3060 12gb": pricing.NewPrice(5500, "zbozi")})
    fp = prices["rtx 3060 12gb"]
    assert fp.over_new and fp.prior is None and fp.median == 5400 and "not used" in fp.prior_note


def test_launch_msrp_is_no_prior():
    prices = thin()
    pricing.apply_priors(prices, {"rtx 3060 12gb": pricing.NewPrice(9300, "msrp", "$329 in 2021")})
    fp = prices["rtx 3060 12gb"]
    assert fp.prior is None and fp.raw_median is None and fp.new_price == 9300  # shown, not used


def test_confident_models_teach_the_ratio_and_keep_their_median():
    samples = []
    for k, (model, base) in enumerate([("rtx 3070", 7000), ("rtx 4060", 5000), ("rtx 3080 10gb", 10000)]):
        samples += [s(100 * k + i, base + 10 * i, model=model) for i in range(16)]
    samples += [s(900 + i, p, model="rtx 4070") for i, p in enumerate([9000, 9200, 9400, 9600])]
    prices = estimate_all(samples)
    new = {
        "rtx 3070": pricing.NewPrice(10000, "zbozi"),  # ratio ~0.71
        "rtx 4060": pricing.NewPrice(7000, "zbozi"),  # ~0.72
        "rtx 3080 10gb": pricing.NewPrice(14000, "zbozi"),  # ~0.72
        "rtx 4070": pricing.NewPrice(14000, "zbozi"),
    }
    before = prices["rtx 3070"].median
    ratio = pricing.apply_priors(prices, new)
    assert 0.70 < ratio < 0.73
    assert prices["rtx 3070"].median == before and prices["rtx 3070"].prior is None  # n >= 15
    assert prices["rtx 4070"].prior == round(14000 * ratio)


def test_ratio_is_clipped():
    samples = []
    for k, model in enumerate(["rtx 3070", "rtx 4060", "rtx 3080 10gb"]):
        samples += [s(100 * k + i, 9900 + i, model=model) for i in range(16)]
    prices = estimate_all(samples)
    new = {m: pricing.NewPrice(10000, "zbozi") for m in prices}
    assert pricing.apply_priors(prices, new) == pricing.NEW_RATIO_CLIP[1]


def test_prior_far_from_the_data_is_ignored():
    prices = thin()
    pricing.apply_priors(prices, {"rtx 3060 12gb": pricing.NewPrice(40000, "zbozi")})  # prior 24 000
    fp = prices["rtx 3060 12gb"]
    assert fp.prior is None and fp.median == 5400 and "ignored" in fp.prior_note


def test_sanity_flag_when_used_is_as_dear_as_new():
    prices = thin(model="rtx 5090", prices=(130000, 134000, 135000, 136000, 140000))
    pricing.apply_priors(prices, pricing.new_prices_for(prices, {}, 23.5))  # MSRP $1999 ~ 56 800 Kč
    assert prices["rtx 5090"].over_new and prices["rtx 5090"].new_source == "msrp"
    prices = thin()
    pricing.apply_priors(
        prices, {"rtx 3060 12gb": pricing.NewPrice(5500, "zbozi")}
    )  # used 5 400 vs new 5 500
    assert prices["rtx 3060 12gb"].over_new


def test_auction_units_are_a_lower_anchor_for_office():
    prices = thin(model="optiplex 3050 sff", prices=(1500, 1700, 1800, 2000, 2200), category="office_pc")
    units = {"optiplex 3050 sff": [1400.0, 1500.0, 1600.0]}
    pricing.apply_priors(prices, auction_units=units)
    fp = prices["optiplex 3050 sff"]
    assert fp.prior_source == "auction" and fp.prior == 2250 and fp.prior_weight == 3  # 1 500 x 1.5, 3 lots
    assert fp.median == round(pricing.shrink(1800, 5, 2250, 3))
    # a Bazoš median below what the state auction fetched as-is gets raised to that floor
    prices = thin(model="optiplex 3050 sff", prices=(700, 750, 800, 850, 900), category="office_pc")
    pricing.apply_priors(prices, auction_units={"optiplex 3050 sff": [1400.0, 1500.0]})
    fp = prices["optiplex 3050 sff"]
    assert fp.median == 1450 and fp.raw_median == 800 and "floor" in fp.prior_note


# --- Phase 9 review fix: disks pooled per class in Kč/TB ----------------------------------------------


def test_fair_price_per_tb_pools_capacities():
    samples = [s(i, 980 * 8, model="hdd35 nas", category="hdd", capacity_tb=8.0) for i in range(3)] + [
        s(10 + i, 1000 * 12, model="hdd35 nas", category="hdd", capacity_tb=12.0) for i in range(3)
    ]
    fp = fair_price(samples, per_tb=True)
    assert fp.per_tb and 980 <= fp.median <= 1000  # a Kč/TB figure, not a Kč one
    assert fp.for_listing({"capacity_tb": 8.0}) == round(fp.median * 8)
    assert fp.for_listing({"capacity_tb": 16.0}) == round(fp.median * 16)
    assert fp.for_listing({}) == 0  # no capacity: nothing to scale by


def test_fair_price_per_tb_drops_samples_without_capacity():
    samples = [s(i, 8000, model="hdd35 nas", category="hdd", capacity_tb=8.0) for i in range(4)]
    samples.append(s(99, 9000, model="hdd35 nas", category="hdd"))  # no capacity_tb: unusable per-TB
    fp = fair_price(samples, per_tb=True)
    assert fp.n == 4 and fp.n_listings == 5


def test_estimate_all_pools_hdd35_classes_not_ssd_or_sas():
    samples = [s(i, 980 * 8, model="hdd35 nas", category="hdd", capacity_tb=8.0) for i in range(4)] + [
        s(10 + i, 2000, model="ssd sata 1tb", category="hdd", capacity_tb=1.0) for i in range(4)
    ]
    prices = estimate_all(samples)
    assert prices["hdd35 nas"].per_tb and prices["hdd35 nas"].median == pytest.approx(980, abs=1)
    assert not prices["ssd sata 1tb"].per_tb and prices["ssd sata 1tb"].median == 2000


def test_disk_new_per_tb_groups_by_class_and_takes_the_median():
    zbozi = {
        "hdd35 nas 8tb": (8000, 10),  # 1000 Kč/TB
        "hdd35 nas 12tb": (13200, 5),  # 1100 Kč/TB
        "hdd35 enterprise 16tb": (16000, 2),  # 1000 Kč/TB, different class: ignored for 'hdd35 nas'
        "rtx 3070": (9990, 2),  # not a disk key: ignored
    }
    new = pricing.disk_new_per_tb(zbozi)
    assert new["hdd35 nas"] == pricing.NewPrice(1050, "zbozi", "2 keys, 15 shops")
    assert new["hdd35 enterprise"] == pricing.NewPrice(1000, "zbozi", "1 key, 2 shops")
    assert "rtx 3070" not in new


def test_pooled_disk_class_shrinks_toward_a_per_tb_prior():
    samples = [
        s(i, p, model="hdd35 nas", category="hdd", capacity_tb=8.0)
        for i, p in enumerate([700 * 8, 720 * 8, 740 * 8, 760 * 8, 780 * 8])
    ]
    prices = estimate_all(samples)
    fp = prices["hdd35 nas"]
    assert fp.per_tb and 700 <= fp.median <= 780  # already Kč/TB
    new = {"hdd35 nas": pricing.NewPrice(1200, "zbozi", "2 keys, 10 shops")}
    pricing.apply_priors(prices, new, confident_n=15)
    # the hdd category fallback ratio (0.6, no qualifying model yet), not the GPU one (0.75)
    assert (
        fp.prior == round(1200 * pricing.NEW_RATIO_FALLBACK_BY_CATEGORY["hdd"]) and fp.prior_source == "new"
    )
    assert fp.for_listing({"capacity_tb": 8.0}) == round(fp.median * 8)
    assert not fp.over_new


def test_auction_unit_prices_only_from_clean_single_model_lots():
    def item(key, q, **kw):
        return {"key": key, "quantity": q, "verified": True, "condition": "untested", **kw}

    lots = [
        (63000, {"items": [item("latitude 5511", 14)], "stated_units": 14}),
        (900, {"items": [item("optiplex 3010", 1)]}),
        (5000, {"items": [item("latitude 5511", 3), item("thinkpad t480", 2)]}),  # mixed: skipped
        (5000, {"items": [item(None, 5)]}),  # unpriced model
        (5000, {"items": [item("latitude 5511", 4)], "stated_units": 6}),  # counts don't add up
        (5000, {"items": [item("latitude 5511", 4, verified=False)]}),
        (5000, {"items": [item("latitude 5511", 4, condition="broken")]}),
        (0, {"items": [item("latitude 5511", 4)]}),
    ]
    assert pricing.auction_unit_prices(lots) == {"latitude 5511": [4500.0], "optiplex 3010": [900.0]}
