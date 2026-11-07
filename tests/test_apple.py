"""Apple normalizer tests: hand-written edge cases plus ~170 real/hand-checked Bazoš titles."""

from collections import Counter
from pathlib import Path

import pytest
import yaml

from deal_hunter import apple
from deal_hunter.apple import AppleResult, features, normalize, prepare
from deal_hunter.normalize import Normalized, classify

TITLES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "apple_titles.yaml").read_text(encoding="utf-8")
)
FEATURES = ("ram_gb", "ssd_gb", "cpu")


@pytest.mark.parametrize("entry", TITLES, ids=[e["title"][:50] for e in TITLES])
def test_real_titles(entry):
    r = normalize(entry["title"])
    assert (r.model, r.excluded_reason) == (entry.get("model"), entry.get("excluded"))
    assert r.features == {k: entry[k] for k in FEATURES if k in entry}


def test_real_title_set_is_big_and_varied():
    assert len(TITLES) >= 150
    matched = sum(1 for e in TITLES if "model" in e)
    assert matched >= 80
    statuses = {AppleResult(e.get("model"), None, {}, e.get("excluded")).status for e in TITLES}
    assert statuses == {"matched", "excluded", "unmatched"}
    categories = Counter(e["model"].split()[0] for e in TITLES if "model" in e)
    assert categories["macbook"] >= 40 and categories["imac"] >= 10


@pytest.mark.parametrize(
    "raw, prepared",
    [
        ("MacBook Air 13” M2", "macbook air 13 in m2"),
        ('MacBook Pro 16" M3 Max', "macbook pro 16 in m3 max"),
        ("iMac 21,5 2017", "imac 21.5 2017"),
    ],
)
def test_prepare(raw, prepared):
    assert prepare(raw) == prepared


@pytest.mark.parametrize(
    "title, model",
    [
        ("MacBook Air M2", "macbook air m2 13"),
        ("MacBook Air 15 M2", "macbook air m2 15"),
        ("MacBook Pro M1 Pro", "macbook pro m1 pro 14"),  # no screen stated -> defaults to 14
        ("MacBook Pro 16 M1 Max", "macbook pro m1 max 16"),
        ("MacBook Pro M1", "macbook pro m1 13"),  # base M1 Pro is always 13"
        ("MacBookPro M4Pro 14", "macbook pro m4 pro 14"),  # fused line + fused chip variant
        ("iMac M1", "imac m1 24"),  # the only size Apple Silicon iMacs ship in
        ("iMac 27 2019", "imac 27 2019"),
        ("iMac 21.5 2015", "imac 21.5 2015"),
        ("Mac mini M2 Pro", "mac mini m2 pro"),
        ("Mac Studio M2 Ultra", "mac studio m2 ultra"),
        ("MacBook Pro 13 2015", "macbook pro intel 2015 13"),
        ("MacBook Air 13 i5", "macbook air intel 13"),  # no year, but a screen -> coarser key
    ],
)
def test_models(title, model):
    assert normalize(title).model == model


@pytest.mark.parametrize(
    "title, reason",
    [
        ("Koupím MacBook Pro M2", "wanted"),
        ("2x MacBook Air M1 13", "multiple"),
        ("MacBook Air M1 13 + monitor", "bundle"),
        ("Baterie pro MacBook Air 13 M1", "accessory"),
        ("Klávesnice pro MacBook Pro 13", "accessory"),
        ("MacBook Pro 13 M1 na díly, nefunkční displej", "accessory"),
        ("Napájecí adaptér Apple usb-c 87W", "out_of_scope"),  # no model at all
        ("iPad Pro 11 M2", "out_of_scope"),  # not a Mac, even though it has an M-chip
        ("iPhone 15 Pro", "out_of_scope"),
    ],
)
def test_exclusions(title, reason):
    assert normalize(title).excluded_reason == reason


def test_kept_with_extras():
    assert normalize("MacBook Pro 16 M1 Max + Magic Mouse a klávesnice").model == "macbook pro m1 max 16"


def test_unmatched_lines_go_to_review():
    for title in (
        "Apple MacBook",
        "MacBook Air",
        "MacBook Pro",
        "iMac",
        "Mac mini",
        "Mac Studio - bez chipu",
    ):
        assert normalize(title).status == "unmatched", title


@pytest.mark.parametrize(
    "title, expected",
    [
        ("MacBook Air M1 13 8GB/256GB", {"ram_gb": 8, "ssd_gb": 256}),
        ("Mac Studio M2 Ultra 128GB RAM", {"ram_gb": 128}),  # widened RAM_SIZES (Apple-only configs)
        ("MacBook Pro M3 Max 16 36GB/1TB", {"ram_gb": 36, "ssd_gb": 1024}),
        ("MacBook Pro 13 2015 i5 8GB 256GB", {"ram_gb": 8, "ssd_gb": 256, "cpu": "i5"}),
    ],
)
def test_features(title, expected):
    assert features(title) == expected


def test_classify_dispatches_apple_after_office():
    assert classify("MacBook Air M2 13 8GB 256GB", "notebook") == Normalized(
        "apple", "macbook air m2 13", {"ram_gb": 8, "ssd_gb": 256}
    )
    assert classify("Apple iMac 24 M1 8GB 256GB", "pc") == Normalized(
        "apple_desktop", "imac m1 24", {"ram_gb": 8, "ssd_gb": 256}
    )


def test_version_is_positive():
    assert apple.VERSION >= 1
