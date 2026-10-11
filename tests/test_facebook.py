"""facebook.py: cookie conversion, search URLs, checkpoint detection and the GraphQL parser.

`run_once` (the actual Playwright automation) isn't covered here -- it needs a real browser and a
logged-in session, neither of which belongs in a unit test. Everything it delegates to is pure and
is covered instead.
"""

import json

import pytest

from deal_hunter import facebook
from deal_hunter.sources import make_id


def test_search_url():
    url = facebook.search_url("rtx 3060", city="prague", radius_km=40)
    assert url == (
        "https://www.facebook.com/marketplace/prague/search/"
        "?query=rtx%203060&sortBy=creation_time_descend&radius=40"
    )


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.facebook.com/marketplace/prague/search/?query=gpu", False),
        ("https://www.facebook.com/checkpoint/1501092823525282/", True),
        ("https://www.facebook.com/login/?next=%2Fmarketplace%2F", True),
        ("https://www.facebook.com/login.php?next=foo", True),
    ],
)
def test_is_checkpoint(url, expected):
    assert facebook.is_checkpoint(url) == expected


def test_convert_cookie_persistent():
    raw = {
        "domain": ".facebook.com",
        "expirationDate": 1830000000,
        "httpOnly": True,
        "name": "c_user",
        "path": "/",
        "sameSite": "no_restriction",
        "secure": True,
        "session": False,
        "value": "0000000000000",
    }
    cookie = facebook.convert_cookie(raw)
    assert cookie == {
        "name": "c_user",
        "value": "0000000000000",
        "domain": ".facebook.com",
        "path": "/",
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
        "expires": 1830000000.0,
    }


def test_convert_cookie_session():
    raw = {"name": "fr", "value": "x", "session": True, "sameSite": "lax"}
    cookie = facebook.convert_cookie(raw)
    assert cookie["expires"] == -1
    assert cookie["sameSite"] == "Lax"


def test_load_cookies(fixture, tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(fixture("cookie_editor_export_SYNTHETIC.json"), encoding="utf-8")
    cookies = facebook.load_cookies(str(path))
    assert len(cookies) == 2
    assert {c["name"] for c in cookies} == {"c_user", "fr"}


def test_parse_graphql_response_synthetic_fixture(fixture):
    payload = json.loads(fixture("fb_graphql_SYNTHETIC.json"))
    listings = facebook.parse_graphql_response(payload, "graficka")
    assert len(listings) == 2
    rtx = listings[0]
    assert rtx.source == "fb"
    assert rtx.external_id == "1234567890123456"
    assert rtx.id == make_id("fb", "1234567890123456")
    assert rtx.price_czk == 5500
    assert rtx.price_kind == "fixed"
    assert rtx.location == "Praha"
    assert rtx.images == ["https://scontent.xx.fbcdn.net/synthetic-1.jpg"]
    assert rtx.url == "https://www.facebook.com/marketplace/item/1234567890123456/"


def test_parse_graphql_response_empty_payload():
    assert facebook.parse_graphql_response({}, "pc") == []


def test_available_without_playwright_installed():
    # Playwright is an optional extra; the test environment doesn't install it, which is itself
    # the behaviour run_once relies on to degrade gracefully.
    assert facebook.available() is False
