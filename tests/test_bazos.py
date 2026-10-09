"""Parser tests against real pages saved from pc.bazos.cz on 2026-10-09 (tests/fixtures/).

If Bazoš changes its layout, these break in CI instead of the scraper silently saving garbage.
"""

from datetime import date

import pytest

from deal_hunter import bazos


@pytest.mark.parametrize(
    "text, expected",
    [
        ("  11 000 Kč", (11000, "fixed")),
        ("   149 Kč", (149, "fixed")),
        ("134 990 Kč", (134990, "fixed")),
        ("Dohodou", (None, "negotiable")),
        ("V textu", (None, "in_text")),
        ("Zdarma", (None, "free")),
        ("Nabídněte", (None, "offer")),
        ("2 000", (2000, "fixed")),
        ("", (None, "unknown")),
        (None, (None, "unknown")),
    ],
)
def test_parse_price(text, expected):
    assert bazos.parse_price(text) == expected


def test_parse_date():
    assert bazos.parse_date(" - TOP - [9.10. 2026]") == date(2026, 10, 9)
    assert bazos.parse_date(" - [31.12. 2025]") == date(2025, 12, 31)
    assert bazos.parse_date("no date") is None
    assert bazos.parse_date("[31.2. 2026]") is None


def test_category_url():
    assert bazos.category_url(0) == "https://pc.bazos.cz/graficka/"
    assert bazos.category_url(2) == "https://pc.bazos.cz/graficka/40/"


def test_parse_category_page(fixture):
    items = bazos.parse_category(fixture("category_0.html"))
    assert len(items) == 20
    assert len({i.id for i in items}) == 20
    first = items[0]
    assert first.id == 224753094
    assert first.url == "https://pc.bazos.cz/inzerat/224753094/gigabyte-geforce-rtx-5090-gaming-oc-32g.php"
    assert first.title == "GIGABYTE GeForce RTX 5090 GAMING OC 32G"
    assert (first.price_czk, first.price_kind) == (134990, "fixed")
    assert (first.location, first.psc) == ("Pardubice", "535 01")
    assert first.posted_at == date(2026, 10, 9)
    assert first.views == 850
    assert first.seller == "RTX5090"
    assert first.description.startswith("Prodám grafickou kartu GIGABYTE")
    assert first.images == ["https://www.bazos.cz/img/1t/094/224753094.jpg?t=1791322824"]
    # A URL-encoded seller name, and the one ad priced "V textu"
    assert any(i.seller == "Lukáš Koželský" for i in items)
    in_text = [i for i in items if i.price_kind == "in_text"]
    assert len(in_text) == 1 and in_text[0].price_czk is None
    assert all(i.psc and i.location and i.posted_at and i.views is not None for i in items)


def test_parse_second_category_page(fixture):
    items = bazos.parse_category(fixture("category_20.html"))
    assert len(items) == 20
    assert not {i.id for i in items} & {i.id for i in bazos.parse_category(fixture("category_0.html"))}
    assert all(i.price_kind == "fixed" and i.price_czk > 0 for i in items)


def test_parse_rss(fixture):
    items = bazos.parse_rss(fixture("rss.xml"))
    assert len(items) == 50
    first = items[0]
    assert first.id == 224868919
    assert first.title == "Prodám MSI GeForce GTX 1070 Ti ARMOR 8G"
    assert (first.price_czk, first.price_kind) == (2000, "fixed")
    assert first.published.isoformat() == "2026-10-09T17:54:54+02:00"
    assert (items[1].price_czk, items[1].price_kind) == (None, "in_text")
    assert all(i.url.startswith("https://pc.bazos.cz/inzerat/") for i in items)


def test_parse_detail(fixture):
    url = "https://pc.bazos.cz/inzerat/224601918/rtx-3080-grafika-geforce.php"
    ad = bazos.parse_detail(fixture("detail_224601918.html"), url)
    assert ad.id == 224601918
    assert ad.title == "Rtx 3080 - grafika - geforce"
    assert (ad.price_czk, ad.price_kind) == (11000, "fixed")
    assert (ad.location, ad.psc) == ("Praha 4", "140 00")
    assert ad.posted_at == date(2026, 10, 9)
    assert ad.views == 970
    assert ad.seller == "David"
    assert ad.raw_category == "graficka"
    assert ad.description.startswith("Jako nova - EVGA GeForce RTX 3080 XC3 ULTRA LHR")
    assert "\nCena dole" in ad.description  # <br> line breaks survive
    assert ad.description.endswith("d6640446.htm")  # the "similar ads" box below is not included
    assert len(ad.images) == 4
    assert ad.images[1] == "https://www.bazos.cz/img/2/918/224601918.jpg?t=1791396936"


@pytest.mark.parametrize(
    "ad_id, price, kind, seller, n_images",
    [
        (224771935, 12500, "fixed", "Radan", 4),
        (224863611, 149, "fixed", "Aloha", 3),
        (224867845, None, "in_text", "M.A.", 6),
    ],
)
def test_parse_more_details(fixture, ad_id, price, kind, seller, n_images):
    ad = bazos.parse_detail(fixture(f"detail_{ad_id}.html"), f"https://pc.bazos.cz/inzerat/{ad_id}/x.php")
    assert (ad.id, ad.price_czk, ad.price_kind, ad.seller, len(ad.images)) == (
        ad_id,
        price,
        kind,
        seller,
        n_images,
    )
    assert ad.psc and ad.location and ad.description and ad.raw_category == "graficka"


def test_removed_ad_page(fixture):
    html = fixture("detail_gone.html")
    assert bazos.is_removed_page(html)
    assert bazos.parse_detail(html, "https://pc.bazos.cz/inzerat/100000001/neexistuje.php") is None
