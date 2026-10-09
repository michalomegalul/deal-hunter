"""GPU normalizer tests: hand-written edge cases plus ~200 real, hand-checked Bazoš titles."""

from pathlib import Path

import pytest
import yaml

from deal_hunter.normalize import Result, load_catalog, normalize, prepare

TITLES = yaml.safe_load((Path(__file__).parent / "fixtures" / "titles.yaml").read_text(encoding="utf-8"))


def expected(entry: dict) -> Result:
    return Result(entry.get("model"), entry.get("vram"), entry.get("excluded"))


@pytest.mark.parametrize("entry", TITLES, ids=[e["title"][:50] for e in TITLES])
def test_real_titles(entry):
    assert normalize(entry["title"]) == expected(entry)


def test_real_title_set_is_big_and_varied():
    assert len(TITLES) >= 200
    statuses = {expected(e).status for e in TITLES}
    assert statuses == {"matched", "excluded", "unmatched"}


@pytest.mark.parametrize(
    "raw, prepared",
    [
        ("MSI RTX3080Ti 12GB", "msi rtx 3080 ti 12 gb"),
        ("Sapphire 6700XT", "sapphire 6700 xt"),
        ("Intel Arc A770 16G", "intel arc a770 16 g"),
        ("GTX 1660S", "gtx 1660 super"),
        ("RTX 4070TiS", "rtx 4070 ti super"),
        ("Sháním grafiku", "shanim grafiku"),
        ("hraje 1080p i 1440p", "hraje i"),
    ],
)
def test_prepare(raw, prepared):
    assert prepare(raw) == prepared


@pytest.mark.parametrize(
    "title, result",
    [
        ("rtx3080", Result("rtx 3080")),
        ("RTX 3060 12GB", Result("rtx 3060", 12)),
        ("RTX 3060 8 GB", Result("rtx 3060", 8)),
        ("RTX 3060", Result("rtx 3060")),  # two variants, size not given: leave it empty
        ("RTX 4060 Ti 16GB", Result("rtx 4060 ti", 16)),
        ("4060ti 8g", Result("rtx 4060 ti", 8)),
        ("rx 7900xtx", Result("rx 7900 xtx", 24)),
        ("RX 7900 GRE", Result("rx 7900 gre", 16)),
        ("Radeon 6600", Result("rx 6600", 8)),
        ("6700 xt", Result("rx 6700 xt", 12)),
        ("Arc B580", Result("arc b580", 12)),
        ("RTX 3050 Ti", Result()),  # a laptop-only chip: unknown, not silently 'rtx 3050'
        ("Koupím RTX 3080", Result(excluded_reason="wanted")),
        ("Výkup grafických karet", Result(excluded_reason="wanted")),
        ("Herní PC Ryzen 5 + RTX 3060", Result(excluded_reason="bundle")),
        ("Sestava s RTX 2060", Result(excluded_reason="bundle")),
        ("RTX 3060 do PC", Result("rtx 3060")),
        ("Notebook RTX 4060", Result(excluded_reason="laptop")),
        ("2x RTX 3070", Result(excluded_reason="multiple_gpus")),
        ("RTX 3070 a RX 6800", Result(excluded_reason="multiple_gpus")),
        ("MSI RX 6650 XT MECH 2X 8G", Result("rx 6650 xt", 8)),  # 2X = two fans, not two cards
        ("Krabice od RTX 4090", Result(excluded_reason="accessory")),
        ("RTX 4090 + krabice", Result("rtx 4090", 24)),
        ("Chladič na RTX 3080", Result(excluded_reason="accessory")),
        ("Riser PCIe x16", Result(excluded_reason="accessory")),
        ("Mining rig 6x RX 580", Result(excluded_reason="mining")),
        ("RTX 3070 vyměním za RTX 3080", Result("rtx 3070", 8)),
        ("Prodám/vyměním RTX 4090", Result("rtx 4090", 24)),
        ("GTX 970", Result(excluded_reason="out_of_scope")),
        ("Quadro P2000", Result(excluded_reason="out_of_scope")),
        ("Grafická karta", Result()),
        ("Ryzen 7 5700X", Result(excluded_reason="bundle")),  # a CPU, not an RX 5700
    ],
)
def test_rules(title, result):
    assert normalize(title) == result


def test_catalog_is_consistent():
    catalog = load_catalog()
    assert catalog.version >= 1
    assert len(catalog.models) > 60
    for key, model in catalog.models.items():
        assert model.name.endswith(key)
        assert model.vram and all(isinstance(v, int) for v in model.vram)
