"""Auction lots: cleaning the LLM's answer, valuation, the push rules and the push text."""

import json
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from deal_hunter import auction_cycle, auctions, nms
from deal_hunter.auctions import Costs, Extraction, Item
from deal_hunter.pricing import FairPrice
from tests.conftest import FIXTURES
from tests.test_triage import FakeLLM


def lot_dict(lot_id, attachment_text=None) -> dict:
    lot = nms.parse_detail((FIXTURES / f"nms_detail_{lot_id}.html").read_text(encoding="utf-8"), lot_id)
    d = asdict(lot)
    d["attachment_text"] = attachment_text
    return d


NOTEBOOKS_LIST = "[seznam]\n18× PC přenosný ACER Extensa 2540\nNTB ACER ASPIRE E1-731 + brašna"


def item(**kw) -> dict:
    base = {
        "model": "Dell Latitude 5511",
        "kind": "laptop",
        "quantity": 14,
        "condition": "untested",
        "missing": [],
        "ram_gb": None,
        "storage_gb": None,
        "cpu": None,
        "defects": [],
        "quote": "NTB Dell Latitude 5511 (14)",
    }
    return {**base, **kw}


def answer(*items, **kw) -> dict:
    base = {"items": list(items), "vat_deductible": False, "pickup": None, "inspection": None, "fees": None}
    return {**base, "notes": "", **kw}


def clean(lot_id, data, attachment_text=None):
    lot = lot_dict(lot_id, attachment_text)
    return auctions.clean(data, auctions.source_text(lot), lot["title"])


# --- the prompt and the cleanup ---------------------------------------------------------------------


def test_prompt_shows_description_attachments_and_the_item_list():
    text = auctions.build_prompt(lot_dict(62785, NOTEBOOKS_LIST))
    assert "Auction lot 62785 from Generální inspekce bezpečnostních sborů. Current price: 7 600 CZK." in text
    assert "bez paměťových disků" in text
    assert "3-příloha č.1 seznam nab.majetku NTB.pdf" in text
    assert "18× PC přenosný ACER Extensa 2540" in text


def test_clean_maps_models_and_keeps_checked_claims():
    x, notes = clean(62388, answer(item()))
    assert notes == []
    [i] = x.items
    assert (i.key, i.category, i.quantity, i.verified) == ("latitude 5511", "office_laptop", 14, True)
    assert x.stated_units == 14


def test_clean_rejects_inventory_numbers_as_quantities():
    data = answer(
        item(
            model="Dell Optiplex 3050",
            kind="desktop",
            quantity=2863,
            condition="working",
            missing=["os", "charger"],
            quote="Funkční PC Intel I5-6500",
        )
    )
    x, notes = clean(62495, data)
    [i] = x.items
    assert i.quantity == 1  # 'Prodej PC Dell Optiplex 3050 - 2863': 2863 is an inventory number
    assert i.missing == ["os"]  # a desktop has no charger; 'příslušenství' is a keyboard and a cable
    assert i.key == "optiplex 3050"
    assert any("2863" in n for n in notes)
    assert x.stated_units is None  # '4x USB 3.0' is not a unit count


def test_clean_single_item_takes_the_stated_count_when_its_own_is_unsupported():
    x, _ = clean(62388, answer(item(quantity=13)))
    assert x.items[0].quantity == 14


def test_clean_checks_quotes_missing_parts_vat_and_fees():
    data = answer(
        item(
            model="ACER Extensa 2540",
            quantity=18,
            missing=["storage", "battery"],
            quote="18× PC přenosný ACER",
        ),
        item(model="Lenovo ThinkPad T480", quantity=1, quote="ThinkPad T480 v perfektním stavu"),
        vat_deductible=True,
        fees={"percent": 10, "czk": None, "quote": "Majetek je opotřebený"},
    )
    x, notes = clean(62785, data, NOTEBOOKS_LIST)
    acer, thinkpad = x.items
    assert acer.verified and acer.key is None  # a consumer laptop: recognised as nothing we price
    assert acer.missing == ["storage"]  # 'bez paměťových disků' yes, the battery is never mentioned
    assert not thinkpad.verified and thinkpad.key == "thinkpad t480"
    assert x.vat_deductible is False and x.fee_percent == 0
    assert any("battery" in n for n in notes) and any("not in the lot text" in n for n in notes)
    assert any("VAT" in n for n in notes) and any("fee" in n for n in notes)


def test_extract_uses_its_own_schema_and_survives_junk(tmp_path):
    good = json.dumps(answer(item()))
    llm = FakeLLM("not json at all", good)
    outcome = auctions.extract(llm, lot_dict(62388), tmp_path)
    assert outcome.status == "done" and outcome.result.items[0].key == "latitude 5511"
    assert len(llm.calls) == 2
    again = auctions.extract(FakeLLM(), lot_dict(62388), tmp_path)  # from the disk cache, no LLM call
    assert again.stats["cached"]
    failed = auctions.extract(FakeLLM("x", "y"), lot_dict(62222), None)
    assert failed.status == "failed" and failed.result.items == []


def test_extraction_round_trips_through_json():
    x, _ = clean(62388, answer(item(missing=["charger"])))
    back = Extraction.from_dict(json.loads(json.dumps(x.as_dict())))
    assert back == x


# --- valuation -------------------------------------------------------------------------------------


def fair(model="latitude 5511", median=7000, level="medium", n=12, category="office_laptop"):
    return FairPrice(model, category, n, n, median, median - 500, median + 500, 0.5, level)


def extraction(*items, stated=None, **kw):
    return Extraction(items=list(items), stated_units=stated, **kw)


def test_value_lot_the_readme_example():
    x = extraction(Item("Dell Latitude 5511", "laptop", 14, "untested", key="latitude 5511"), stated=14)
    v = auctions.value_lot(x, {"latitude 5511": fair()}, 63000, 500, 1, Costs())
    # 14 x 7000 x 0.9 = 88 200, minus 1.4 % for ~2.8 weeks of selling = 86 965
    assert v.revenue == 86965 and v.weeks_to_sell == 2.8
    assert v.hours == pytest.approx(11.3, abs=0.05) and v.labour == 2267 and v.fixed == 500
    assert v.max_bid == 58100  # 86 965 x 0.7 - 2 267 - 500
    assert v.next_bid == 63500 and v.profit == 20698
    assert v.profit_per_hour == round(20698 / 11.333)
    assert v.confidence == "medium" and not v.qualifies
    assert "above the max bid" in v.reasons[0]


def test_value_lot_refurb_costs_and_spec():
    fp = fair(median=6000)
    fp.ref, fp.slopes = {"ssd_gb": 256}, {"ssd_gb": 0.06}
    no_disk = Item(
        "Dell Latitude 5511", "laptop", 10, "working", missing=["storage", "charger"], key="latitude 5511"
    )
    x = extraction(no_disk)
    v = auctions.value_lot(x, {"latitude 5511": fp}, 20000, 300, 0, Costs())
    line = v.lines[0]
    assert line.fair == 6000  # priced as if it had the 256 GB SSD we put in
    assert line.parts == 750 and v.parts == 7500
    assert v.next_bid == 20000  # no bids yet: the starting price is a valid first bid
    assert v.qualifies, v.reasons


def test_value_lot_broken_and_unpriced_units():
    x = extraction(
        Item("Dell Latitude 5511", "laptop", 2, "broken", missing=["storage"], key="latitude 5511"),
        Item("ACER Extensa 2540", "laptop", 18, "untested"),
    )
    v = auctions.value_lot(x, {"latitude 5511": fair()}, 1000, 100, 0, Costs())
    assert v.lines[0].parts == 0 and v.lines[0].resale == round(7000 * 0.9 * 0.25)
    assert v.lines[1].fair is None and v.priced_units == 2 and v.units == 20
    assert v.confidence == "low"  # 90 % of the units have no price


def test_value_lot_without_any_fair_price():
    x = extraction(Item("ACER Extensa 2540", "laptop", 18, "untested"))
    v = auctions.value_lot(x, {}, 7600, 300, 1, Costs())
    assert v.confidence == "none" and v.max_bid is None and not v.qualifies


def test_low_confidence_needs_a_huge_margin():
    x = extraction(Item("Dell Latitude 5511", "laptop", 10, "working", key="latitude 5511"))
    prices = {"latitude 5511": fair(level="low", n=5)}
    modest = auctions.value_lot(x, prices, 30000, 500, 0, Costs())
    assert modest.max_bid >= modest.next_bid and not modest.qualifies
    assert "low confidence" in modest.reasons[-1]
    cheap = auctions.value_lot(x, prices, 5000, 500, 0, Costs())
    assert cheap.qualifies  # max bid ~38 000 vs 5 000, profit ~51 000


def test_fees_and_unverified_items_count_against_the_lot():
    x = extraction(
        Item("Dell Latitude 5511", "laptop", 10, "working", key="latitude 5511"), fee_percent=10, fee_czk=300
    )
    v = auctions.value_lot(x, {"latitude 5511": fair()}, 20000, 500, 0, Costs())
    plain = auctions.value_lot(
        extraction(Item("Dell Latitude 5511", "laptop", 10, "working", key="latitude 5511")),
        {"latitude 5511": fair()},
        20000,
        500,
        0,
        Costs(),
    )
    assert v.fixed == plain.fixed + 300 and v.profit == pytest.approx(plain.profit - 300 - 2000, abs=1)
    unverified = Item("Dell Latitude 5511", "laptop", 10, "working", key="latitude 5511", verified=False)
    assert (
        auctions.value_lot(extraction(unverified), {"latitude 5511": fair()}, 1, 1, 0, Costs()).confidence
        == "none"
    )


# --- push timing and text --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sent, hours, kind",
    [
        (set(), 200, "first"),
        ({"first"}, 100, None),
        ({"first"}, 23, "day"),
        ({"first", "day"}, 10, None),
        ({"first", "day"}, 2.4, "final"),
        ({"first", "day", "final"}, 1, None),
        (set(), 0.2, None),  # too late to bother
    ],
)
def test_due_kind(sent, hours, kind):
    assert auction_cycle.due_kind(sent, hours) == kind


def test_covered_by():
    assert auction_cycle.covered_by("first", 100) == []
    assert auction_cycle.covered_by("first", 20) == ["day"]
    assert auction_cycle.covered_by("first", 2) == ["day", "final"]
    assert auction_cycle.covered_by("day", 2) == ["final"]


def test_format_alert():
    x = extraction(
        Item("HP ProBook 645 G4", "laptop", 22, "untested", key="probook 645 g4"),
        Item("HP EliteBook 840 G5", "laptop", 3, "untested", key="elitebook 840 g5"),
        vat_deductible=True,
    )
    prices = {
        "probook 645 g4": fair("probook 645 g4", 2000),
        "elitebook 840 g5": fair("elitebook 840 g5", 4000),
    }
    v = auctions.value_lot(x, prices, 3300, 100, 2, Costs())
    lot = {"title": "Soubor 25 ks notebooků", "bids": 2, "address": "Praha"}
    end = datetime(2026, 10, 15, 12, 0, tzinfo=UTC).astimezone(nms.PRAGUE)
    title, message = auctions.format_alert(lot, x, v, "first", end)
    assert title == "🏛 Aukce: 22× HP ProBook 645 G4 + 1 more model"
    assert message.startswith("ends Thu 14:00, now 3 300 Kč (2 bids)")
    assert "est. resale 22× ~1 800 Kč, 3× ~3 600 Kč" in message
    assert f"max bid ~{auctions.kc(v.max_bid)}" in message and "profit/h ~" in message
    assert "VAT deductible" in message and "📍 Praha" in message
    _, final = auctions.format_alert(lot, x, v, "final", end)
    assert final.startswith("⏰ ends in ~2 h · ends Thu 14:00")
