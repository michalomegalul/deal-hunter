"""sbazar.cz is blocked by robots.txt (User-agent: * / Disallow: /) -- see sbazar.py's docstring.

These tests cover the parser against a hand-made, clearly-marked-synthetic fixture (we never
fetched a real response), and that `fetch_category` refuses to run at all.
"""

import json

import pytest

from deal_hunter import sbazar
from deal_hunter.sources import make_id


def test_fetch_category_always_refuses():
    with pytest.raises(RuntimeError, match="robots.txt"):
        sbazar.fetch_category()


def test_parse_response_synthetic_fixture(fixture):
    data = json.loads(fixture("sbazar_category_SYNTHETIC.json"))
    listings = sbazar.parse_response(data, "graficka")
    assert len(listings) == 2
    rtx = listings[0]
    assert rtx.source == "sbazar"
    assert rtx.external_id == "123456789"
    assert rtx.id == make_id("sbazar", "123456789")
    assert rtx.price_czk == 8000
    assert rtx.price_kind == "fixed"
    assert rtx.location == "Praha"
    assert rtx.psc == "11000"
    assert rtx.section == "graficka"


def test_parse_response_missing_price(fixture):
    data = {"items": [{"id": "1", "name": "no price ad"}]}
    listings = sbazar.parse_response(data, "pc")
    assert listings[0].price_kind == "unknown"
    assert listings[0].price_czk is None
