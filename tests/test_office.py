"""Office normalizer tests: hand-written edge cases plus ~430 real, hand-checked Bazoš titles."""

from collections import Counter
from pathlib import Path

import pytest
import yaml

from deal_hunter import office
from deal_hunter.normalize import Normalized, classify
from deal_hunter.office import OfficeResult, features, normalize, prepare

TITLES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "office_titles.yaml").read_text(encoding="utf-8")
)
FEATURES = ("ram_gb", "ssd_gb", "cpu")


@pytest.mark.parametrize("entry", TITLES, ids=[e["title"][:50] for e in TITLES])
def test_real_titles(entry):
    r = normalize(entry["title"])
    assert (r.model, r.excluded_reason) == (entry.get("model"), entry.get("excluded"))
    assert r.features == {k: entry[k] for k in FEATURES if k in entry}


def test_real_title_set_is_big_and_varied():
    per_section = Counter(e["section"] for e in TITLES)
    assert per_section["pc"] >= 100 and per_section["notebook"] >= 100
    matched = Counter(e["section"] for e in TITLES if "model" in e)
    assert matched["pc"] >= 20 and matched["notebook"] >= 30
    statuses = {OfficeResult(e.get("model"), None, {}, e.get("excluded")).status for e in TITLES}
    assert statuses == {"matched", "excluded", "unmatched"}


@pytest.mark.parametrize(
    "raw, prepared",
    [
        ("Lenovo ThinkPad T14s G.2", "lenovo thinkpad t14s g2"),
        ("PC HP 400 G2.5 - Intel", "pc hp 400 g2.5 intel"),
        ("Dell Latitude E5470 14“/I5", "dell latitude e5470 14 i5"),
    ],
)
def test_prepare(raw, prepared):
    assert prepare(raw) == prepared


@pytest.mark.parametrize(
    "title, model",
    [
        ("Lenovo ThinkPad T480", "thinkpad t480"),
        ("ThinkPad T480s i5 8350U", "thinkpad t480s"),
        ("Lenovo ThinkPad T14 Gen 3", "thinkpad t14 gen 3"),
        ("Lenovo ThinkPad T14 AMD G2", "thinkpad t14 gen 2"),
        ("Lenovo ThinkPad T14", "thinkpad t14"),  # generation unknown: its own, coarser key
        ("Lenovo X1 Carbon 6th Gen", "thinkpad x1 carbon gen 6"),
        ("ThinkPad X13 Yoga Gen 2", "thinkpad x13 yoga gen 2"),
        ("ThinkPad P1 Gen 4", "thinkpad p1 gen 4"),
        ("Lenovo T590 i5/1TB/16GB", "thinkpad t590"),  # a bare 'Lenovo T590' is a ThinkPad
        ("Lenovo ThinkCentre M720q Tiny", "thinkcentre m720q"),
        ("Lenovo M920q i5-8500T", "thinkcentre m920q"),
        ("ThinkCentre M75q Gen 2", "thinkcentre m75q gen 2"),
        ("Lenovo ThinkCentre M900 Tiny", "thinkcentre m900 tiny"),
        ("Dell Latitude 7490", "latitude 7490"),
        ("Dell E5470 i5", "latitude e5470"),
        ("Dell OptiPlex 7060 Micro", "optiplex 7060 micro"),
        ("Dell Optiplex 3040M", "optiplex 3040 micro"),
        ("Dell OptiPlex 5050 SFF", "optiplex 5050 sff"),
        ("Dell OptiPlex 7050", "optiplex 7050"),
        ("Dell OptiPlex Micro 7010", "optiplex 7010 micro"),
        ("HP EliteBook 840 G5", "elitebook 840 g5"),
        ("HP EliteBook x360 1030 G3", "elitebook x360 1030 g3"),
        ("HP 840 G2", "elitebook 840 g2"),
        ("HP ProBook 450 G8", "probook 450 g8"),
        ("HP EliteDesk 800 G4 Mini", "elitedesk 800 g4 mini"),
        ("HP EliteDesk 800 G4 DM", "elitedesk 800 g4 mini"),
        ("HP Elite Mini 800 G9", "elitedesk 800 g9 mini"),
        ("HP ProDesk 600 G3 SFF", "prodesk 600 g3 sff"),
        ("HP 405 G4", "prodesk 405 g4"),
    ],
)
def test_models(title, model):
    assert normalize(title).model == model


@pytest.mark.parametrize(
    "title, reason",
    [
        ("Koupím ThinkPad T480", "wanted"),
        ("2x Lenovo ThinkPad T480", "multiple"),
        ("ThinkPad T480 a T470", "multiple"),
        ("Dokovací stanice Lenovo ThinkPad USB-C", "accessory"),
        ("Napájecí adaptér Lenovo 90W", "out_of_scope"),  # no model at all: not even an accessory of one
        ("Baterie pro Dell Latitude 7490", "accessory"),
        ("SSD disk do ThinkPad T480", "accessory"),
        ("Dell OptiPlex 7050 + monitor 24", "bundle"),
        ("Herní PC Ryzen 5 5600 RTX 3060", "out_of_scope"),
        ("MacBook Air M1", "out_of_scope"),
        ("Lenovo Legion 5 RTX 3060", "out_of_scope"),
        ("Lenovo ThinkBook 14 G4", "out_of_scope"),
        ("Dell Latitude 3510", "out_of_scope"),  # Latitude 3xxx: low-end line, not priced
        ("Dell Latitude E6440", "out_of_scope"),
        ("Dell Precision 5550", "out_of_scope"),
        ("HP ProBook 6460b", "out_of_scope"),
        ("HP 250 G7", "out_of_scope"),
    ],
)
def test_exclusions(title, reason):
    assert normalize(title).excluded_reason == reason


def test_kept_with_extras():
    assert normalize("ThinkPad T480 + dokovací stanice").model == "thinkpad t480"
    assert normalize("Dell Optiplex 5040 SFF, VÍCE KUSŮ").model == "optiplex 5040 sff"  # a reseller


def test_unmatched_business_titles_go_to_review():
    for title in ("Lenovo ThinkPad", "Dell OptiPlex", "Dell 5420, SSD 256GB, RAM 16GB", "HP EliteBook"):
        assert normalize(title).status == "unmatched", title


@pytest.mark.parametrize(
    "title, expected",
    [
        ("HP EliteBook 840 G2 - i5-5300U, 8GB, 256GB SSD", {"ram_gb": 8, "ssd_gb": 256, "cpu": "i5-5300u"}),
        ("Lenovo E330 Intel i5 SSD256gb 8gbRAM Win 11", {"ram_gb": 8, "ssd_gb": 256, "cpu": "i5"}),
        ("Lenovo T590 i5/1TB/16GB", {"ram_gb": 16, "ssd_gb": 1024, "cpu": "i5"}),
        ("T14s 16GB, SSD 512GB", {"ram_gb": 16, "ssd_gb": 512}),
        ("ProDesk 600 G2 - 4GB DDR4, 500GB HDD", {"ram_gb": 4}),  # an HDD is not an SSD
        ("Dell 5420, RAM 8/16GB", {}),  # which one?
        ("ThinkCentre M75q Ryzen 5 PRO 4650GE", {"cpu": "ryzen 5 4650ge"}),
        ("Latitude 7440 Core Ultra 5 135U 16GB", {"ram_gb": 16, "cpu": "ultra 5 135u"}),
    ],
)
def test_features(title, expected):
    assert features(title) == expected


def test_classify_dispatches_by_section():
    assert classify("MSI RTX 3060 12GB", "graficka") == Normalized("gpu", "rtx 3060 12gb", {"vram_gb": 12})
    assert classify("MSI RTX 3070", "graficka").model == "rtx 3070"  # one VRAM size: no suffix
    assert classify("RTX 3060", "graficka").model == "rtx 3060"  # size not given: coarser key
    assert classify("ThinkPad T480 16GB", "notebook") == Normalized("office_laptop", "thinkpad t480", {})
    assert classify("ThinkPad T480 16GB", "notebook").features == {"ram_gb": 16}
    assert classify("Herní PC RTX 3060", "pc") == Normalized("office", None, {}, "out_of_scope")


def test_version_is_positive():
    assert office.VERSION >= 1
