"""Parser tests against real responses saved from aukro.cz's searchItemsCommon API on 2026-10-11
(tests/fixtures/aukro_graficka.json, aukro_hdd.json).
"""

import json
from datetime import date, datetime

from deal_hunter import aukro
from deal_hunter.sources import make_id


def load(fixture, name):
    return json.loads(fixture(name))


def test_pure_auction_excluded_from_pricing(fixture):
    data = load(fixture, "aukro_graficka.json")
    listings = aukro.parse_response(data, "graficka")
    quadro = next(l for l in listings if l.external_id == "7136499281")
    assert quadro.price_kind == "auction"
    assert quadro.price_czk == 2  # the current bid, not a sale price
    assert quadro.auction_end_at == datetime.fromisoformat("2026-10-14T21:00:06+02:00")
    assert quadro.source == "aukro"
    assert quadro.id == make_id("aukro", "7136499281")
    assert quadro.url == "https://aukro.cz/grafika-nvidia-quadro-7136499281"


def test_hybrid_auction_uses_buy_now_price(fixture):
    data = load(fixture, "aukro_graficka.json")
    listings = aukro.parse_response(data, "graficka")
    radeon = next(l for l in listings if l.external_id == "7136827900")
    assert radeon.price_kind == "fixed"
    assert radeon.price_czk == 450  # buyNowPrice, not the 399 current bid
    assert radeon.auction_end_at is not None
    assert radeon.location == "Orlová"
    assert radeon.psc == "73514"
    assert radeon.seller == "janz1545"


def test_plain_fixed_price_listing(fixture):
    data = load(fixture, "aukro_graficka.json")
    listings = aukro.parse_response(data, "graficka")
    rtx = next(l for l in listings if l.external_id == "7132266578")
    assert rtx.price_kind == "fixed"
    assert rtx.price_czk == 7499
    assert rtx.auction_end_at is None  # not an auction, no meaningful end time
    assert rtx.posted_at == date(2026, 10, 3)
    assert rtx.raw_category == "pc-graficke-karty"
    assert rtx.title == "ASUS RTX 3070 ROG STRIX OC - 8GB"


def test_hdd_category_section(fixture):
    data = load(fixture, "aukro_hdd.json")
    listings = aukro.parse_response(data, "hdd")
    assert len(listings) == 3
    assert all(l.section == "hdd" for l in listings)
    assert all(l.source == "aukro" for l in listings)


def test_ids_unique_per_source_even_on_collision():
    # A sbazar listing that happens to reuse the same native id as an aukro one must not collide.
    assert make_id("aukro", "123") != make_id("sbazar", "123")
    assert make_id("bazos", 123) == 123  # unchanged: existing rows keep their literal ad id


def test_category_url():
    assert aukro.category_url("pc-graficke-karty") == (
        "https://aukro.cz/backend-web/api/offers/searchItemsCommon?page=0&size=60"
    )
