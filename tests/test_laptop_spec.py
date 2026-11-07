"""Consumer/gaming laptop normalizer tests: hand-written edge cases plus ~200 real/hand-checked
Bazoš titles."""

from collections import Counter
from pathlib import Path

import pytest
import yaml

from deal_hunter import laptop_spec as ls
from deal_hunter.laptop_spec import LaptopResult, cpu_bucket, features, gpu_bucket, normalize, prepare
from deal_hunter.normalize import Normalized, classify

TITLES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "laptop_titles.yaml").read_text(encoding="utf-8")
)
FEATURES = ("ram_gb", "ssd_gb", "cpu")


@pytest.mark.parametrize("entry", TITLES, ids=[e["title"][:50] for e in TITLES])
def test_real_titles(entry):
    r = normalize(entry["title"], entry["section"])
    assert (r.model, r.category, r.excluded_reason) == (
        entry.get("model"),
        entry.get("category"),
        entry.get("excluded"),
    )
    assert r.features == {k: entry[k] for k in FEATURES if k in entry}


def test_real_title_set_is_big_and_varied():
    assert len(TITLES) >= 150
    per_section = Counter(e["section"] for e in TITLES)
    assert per_section["notebook"] >= 130
    matched = [e for e in TITLES if "model" in e]
    assert len(matched) >= 60
    cats = Counter(e["category"] for e in matched)
    assert cats["herni"] >= 25 and cats["ostatni"] >= 20
    statuses = {LaptopResult(e.get("model"), e.get("category"), {}, e.get("excluded")).status for e in TITLES}
    assert statuses == {"matched", "excluded", "unmatched"}


def test_desktop_ads_in_pc_section_stay_unmatched():
    """Gaming desktops are a later phase: a 'pc' section ad with no explicit laptop word must not
    be bucketed here even when it has a laptop-shaped CPU/GPU spec."""
    for title in (
        "Herní PC Ryzen 7 5800X / RTX 4070 / 32GB RAM / 1TB SSD",
        "Sestava Ryzen 5 5600 / RTX 4060 / 16GB / 1TB NVMe",
    ):
        assert normalize(title, "pc").status == "unmatched", title


def test_a_laptop_mislabeled_in_the_pc_section_is_still_found():
    r = normalize("Notebook Acer Nitro 5, i5-10300H, RTX 3050, 8GB RAM", "pc")
    assert r.model == "laptop i5 g10 rtx 3050"
    assert r.category == "herni"


@pytest.mark.parametrize(
    "title, bucket",
    [
        ("i7-12700H", "i7 g12"),
        ("i5-1135G7", "i5 g11"),
        ("i3-8130U", "i3 g8"),
        ("i9-13900HX", "i9 g13"),
        ("Core Ultra 5 125U", "ultra5 s100"),
        ("Core Ultra 9 285H", "ultra9 s200"),
        ("Ryzen 5 5600H", "r5 5000"),
        ("Ryzen 7 4800H", "r7 4000"),
        ("Ryzen AI 9 365", "rai9 300"),
        ("Ryzen 5", "r5"),  # bare tier, no model number -> coarser
        ("i7", "i7"),
    ],
)
def test_cpu_bucket(title, bucket):
    assert cpu_bucket(prepare(title)) == bucket


@pytest.mark.parametrize(
    "title, gpu, gaming_line",
    [
        ("RTX 4060", "rtx 4060", False),
        ("RTX4070", "rtx 4070", False),
        ("RTX 3050 Ti", "rtx 3050 ti", False),
        ("GTX 1650 Ti", "gtx 1650 ti", False),
        ("Lenovo Legion 5", "igpu", True),
        ("ASUS ROG Strix", "igpu", True),
        ("Acer Aspire 5", "igpu", False),
    ],
)
def test_gpu_bucket(title, gpu, gaming_line):
    assert gpu_bucket(title) == (gpu, gaming_line)


@pytest.mark.parametrize(
    "title, reason",
    [
        ("Koupím herní notebook s RTX 3060", "wanted"),
        ("2x Lenovo Legion 5, RTX 3060, 16GB", "multiple"),
        ("ASUS ROG Strix G15, RTX 3070, 16GB + monitor 27", "bundle"),
        ("Baterie pro Acer Nitro 5", "accessory"),
        ("Nabíječka 150W pro herní notebook Asus", "accessory"),
    ],
)
def test_exclusions(title, reason):
    assert normalize(title, "notebook").excluded_reason == reason


def test_kept_with_extras():
    r = normalize("Lenovo Legion 5, RTX 4060, 16GB + nová klávesnice, nová myš", "notebook")
    assert r.model == "laptop rtx 4060"


def test_spec_less_titles_stay_unmatched():
    for title in (
        "Notebook Asus Vivobook 15 x507m",
        "Lenovo IdeaPad 3",
        "Herní notebook HP Omen 17-an136ng",  # a gaming line alone, no CPU/GPU, isn't enough
    ):
        assert normalize(title, "notebook").status == "unmatched", title


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Lenovo IdeaPad 3, i5-1135G7, 8GB, 256GB SSD", {"ram_gb": 8, "ssd_gb": 256, "cpu": "i5-1135g7"}),
        (
            "Acer Nitro 5, Ryzen 5 5600H, 16GB, 512GB SSD, RTX 3050",
            {"ram_gb": 16, "ssd_gb": 512, "cpu": "ryzen 5 5600h"},
        ),
    ],
)
def test_features(title, expected):
    assert features(title) == expected


def test_classify_dispatches_laptop_after_office_and_apple():
    r = classify("Lenovo Legion 5, i7-12700H, 16GB, 1TB SSD, RTX 4060", "notebook")
    assert r == Normalized(
        "herni", "laptop i7 g12 rtx 4060", {"ram_gb": 16, "ssd_gb": 1024, "cpu": "i7-12700h"}
    )


def test_version_is_positive():
    assert ls.VERSION >= 1
