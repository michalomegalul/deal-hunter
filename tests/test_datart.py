"""Datart parser tests against category pages saved from datart.cz on 2026-10-12
(tests/fixtures/datart_graficke_karty.html, datart_interni_disky.html: the JSON-LD product list
and breadcrumb of the real pages, trimmed to a few products)."""

import json

import pytest

from deal_hunter import datart, pricing
from tests import robots_txt

GPU, DISK = datart.CATEGORIES


def test_gpu_category_products(fixture):
    products = datart.parse_products(fixture("datart_graficke_karty.html"))
    assert len(products) == 5
    by_name = {p.name: p for p in products}
    msi = next(p for n, p in by_name.items() if "RTX 5080 16G GAMING TRIO" in n)
    assert msi.price == 43990
    assert msi.url.startswith("https://www.datart.cz/graficka-karta-msi-geforce-rtx-5080-16g-gaming-trio-oc")
    assert all(p.url.startswith("https://www.datart.cz/") and p.price >= datart.MIN_PRICE for p in products)


def test_disk_names_are_unescaped(fixture):
    products = datart.parse_products(fixture("datart_interni_disky.html"))
    assert 'Pevný disk 3,5" Western Digital Red Plus 6TB (WD60EFPX)' in {
        p.name for p in products
    }  # &quot; -> "


def key_of(products, section, fragment):
    (product,) = [p for p in products if fragment in p.name]
    return datart.pricing_key(product, section)


def test_pricing_keys_of_graphics_cards(fixture):
    products = datart.parse_products(fixture("datart_graficke_karty.html"))
    assert key_of(products, GPU.section, "MSI GeForce RTX 5080 16G GAMING TRIO OC") == "rtx 5080"
    assert (
        key_of(products, GPU.section, "Sapphire Pulse AMD Radeon RX 9060 XT GAMING 16GB") == "rx 9060 xt 16gb"
    )
    assert key_of(products, GPU.section, "MSI GeForce RTX 5050 8G GAMING OC") == "rtx 5050"
    assert key_of(products, GPU.section, "GT 710 2GD3H LP") is None  # a card our catalog does not price


def test_pricing_keys_of_disks(fixture):
    products = datart.parse_products(fixture("datart_interni_disky.html"))
    assert key_of(products, DISK.section, "Red Plus 6TB") == "hdd35 nas 6tb"
    assert key_of(products, DISK.section, "IronWolf Pro 8TB") == "hdd35 nas 8tb"
    assert (
        key_of(products, DISK.section, "Red Plus 8TB") == "hdd35 nas 8tb"
    )  # same class and capacity: one key
    assert key_of(products, DISK.section, "Blue 2TB") == "hdd35 desktop 2tb"
    # a 2.5" drive is not pooled, so it has no new price in this feed
    assert key_of(products, DISK.section, 'Pevný disk 2,5" Seagate Barracuda 2TB') is None


def test_disk_key_without_capacity_is_refused():
    product = datart.Product('Pevný disk 3,5" Seagate IronWolf', 5000, "https://www.datart.cz/x.html")
    assert datart.pricing_key(product, DISK.section) is None


def test_keys_match_what_the_disk_prior_reads(fixture):
    """The stored key feeds pricing.disk_new_per_tb, which splits it with storage.split_capacity_key."""
    products = datart.parse_products(fixture("datart_interni_disky.html"))
    offers = {}
    for p in products:
        key = datart.pricing_key(p, DISK.section)
        if key:
            offers[key] = (min(p.price, offers.get(key, (10**9,))[0]), 1)
    assert (
        offers["hdd35 nas 8tb"][0] == 9499
    )  # the cheaper of IronWolf Pro 8TB (9 499) and Red Plus 8TB (9 590)
    per_tb = pricing.disk_new_per_tb(offers, {k: "datart" for k in offers})
    assert per_tb["hdd35 nas"].source == "datart" and per_tb["hdd35 nas"].czk > 0
    assert per_tb["hdd35 desktop"].czk == round(offers["hdd35 desktop 2tb"][0] / 2)


def test_page_without_a_product_list_or_with_a_broken_one():
    assert datart.parse_products("<html><body>Stránka je nedostupná</body></html>") == []
    broken = (
        '<script type="application/ld+json" id="category-product-list-json">{"itemListElement": [</script>'
    )
    assert datart.parse_products(broken) == []
    assert datart.parse_products(broken.replace('{"itemListElement": [', '{"other": 1}')) == []


def _page(*entries):
    data = {"@type": "ItemList", "itemListElement": list(entries)}
    return f'<script type="application/ld+json" id="category-product-list-json">{json.dumps(data)}</script>'


def _entry(name="Grafická karta X", price=9000, url="/x.html"):
    item = {"@type": "Product", "name": name, "offers": {"price": price}, "url": url}
    return {"@type": "ListItem", "item": item}


def test_entries_that_are_not_usable_products_are_skipped():
    """Hand-made JSON-LD (not a saved page): each kind of unusable entry next to one good one."""
    good = _entry(price="9000.4", url="https://www.datart.cz/a.html")
    page = _page(
        "not an entry",
        {"@type": "ListItem"},  # no item
        _entry(name=""),
        _entry(url=""),
        _entry(price=None),
        _entry(price="n/a"),
        _entry(price=499),  # below MIN_PRICE: an accessory
        good,
    )
    assert datart.parse_products(page) == [
        datart.Product("Grafická karta X", 9000, "https://www.datart.cz/a.html")
    ]


def test_relative_urls_are_made_absolute():
    (p,) = datart.parse_products(_page(_entry(url="/relative.html")))
    assert p.url == "https://www.datart.cz/relative.html"


ALLOWED = [
    "https://www.datart.cz/graficke-karty.html",
    "https://www.datart.cz/graficke-karty.html?page=2",
    "https://www.datart.cz/interni-pevne-disky.html?page=4",
]
NOT_ALLOWED = [
    "http://www.datart.cz/graficke-karty.html",
    "https://datart.cz/graficke-karty.html",
    "https://www.datart.cz/vyhledavani?q=rtx",  # robots: /vyhledavani
    "https://www.datart.cz/graficke-karty.html?tab=2",  # robots: ?tab=
    "https://www.datart.cz/graficke-karty.html?listType=list",
    "https://www.datart.cz/graficke-karty.html?orderBY=price",
    "https://www.datart.cz/graficke-karty.html?do=x",
    "https://www.datart.cz/graficke-karty.html?page=2&tab=1",
    "https://www.datart.cz/graficke-karty.html?page=x",
    "https://www.datart.cz/graficke-karty/filter/o:1.html",  # robots: /filter/ (except a few sort orders)
    "https://www.datart.cz/kosik/",
    "https://www.datart.cz/vyprodej-poslednich-kusu.html",  # looked at, not used (see the module doc)
]


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_urls(url):
    assert datart.allowed(url)


@pytest.mark.parametrize("url", NOT_ALLOWED)
def test_urls_that_are_not_allowed(url):
    assert not datart.allowed(url)


def test_every_url_we_build_passes_the_real_robots_txt(fixture):
    rules = robots_txt.parse(fixture("datart_robots.txt"))
    named_by_robots = ("/vyhledavani", "?tab=", "?listType=", "?orderBY", "?do=", "/filter/", "/kosik")
    for url in NOT_ALLOWED:
        if any(x in url for x in named_by_robots):
            assert robots_txt.blocked(rules, url), url  # the matcher agrees with robots.txt
    assert not robots_txt.blocked(rules, "https://www.datart.cz/graficke-karty/filter/o:3")  # its Allow line
    for cat in datart.CATEGORIES:
        for page in (1, 2, 3, 4):
            url = datart.category_url(cat, page)
            assert datart.allowed(url), url
            assert not robots_txt.blocked(rules, url), url
