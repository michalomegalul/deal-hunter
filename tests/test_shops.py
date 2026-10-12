"""Mironet outlet parser tests against listing pages saved from mironet.cz on 2026-10-12
(tests/fixtures/mironet_*.html, trimmed to a few products; the unfiltered page is the plain GPU category)."""

import pytest

from deal_hunter import normalize, shops
from deal_hunter.sources import SHOP_SOURCES, make_id
from tests import robots_txt

GRAFICKA, NOTEBOOK, PC_ROZBALENO, PC_BAZAR, HDD = (shops.CATEGORIES[i] for i in (0, 1, 2, 3, 4))


def test_unboxed_graphics_cards(fixture):
    items = shops.parse_page(fixture("mironet_rozbaleno_graficka.html"), GRAFICKA)
    assert [i.external_id for i in items] == ["839754", "1031826"]
    quadro, arc = items
    assert quadro.source == "mironet" and quadro.id == make_id("mironet", "839754")
    assert quadro.title.startswith("Rozbaleno - PNY NVIDIA Quadro T1000 / nVidia Quadro T1000 4GB")
    assert (quadro.price_czk, quadro.price_kind) == (9999, "retail")
    assert quadro.url == (
        "https://www.mironet.cz/rozbaleno-pny-nvidia-quadro-t1000-nvidia-quadro-t1000-4gb-4gb-gddr6-"
        "pcie-x16-30-4x-mini-dp-50w-rozbaleno+dp839754/"
    )
    assert quadro.images == ["https://www.mironet.cz/Foto/s3/92473465.webp"]
    assert quadro.section == "graficka" and quadro.seller == "Mironet.cz"
    assert quadro.raw_category == "rozbaleno:graficke-karty+fc14402"
    assert quadro.description.startswith("NVIDIA Quadro T - T1000 @ 1065MHz")
    assert quadro.description.endswith(shops.SHIPPING_NOTE)
    assert arc.price_czk == 6433 and "Arc A750" in arc.title


def test_titles_are_unescaped_and_prices_read_with_non_breaking_spaces(fixture):
    (aio,) = shops.parse_page(fixture("mironet_bazar_pc.html"), PC_BAZAR)
    assert aio.title.startswith('Bazar - Lenovo AIO IdeaCentre 24IRH9 šedá / 23.8" FHD')  # &quot; -> "
    assert aio.price_czk == 20595  # '20&nbsp;595&nbsp;Kč'
    assert aio.section == "pc" and aio.raw_category == "bazar:pocitace+fc1658"


def test_a_page_of_another_outlet_yields_nothing(fixture):
    # the bazar listing, read as the unboxed one: the badge does not match, so nothing is taken
    assert shops.parse_page(fixture("mironet_bazar_pc.html"), PC_ROZBALENO) == []
    assert shops.parse_page(fixture("mironet_rozbaleno_graficka.html"), PC_BAZAR) == []


def test_unfiltered_listing_is_not_taken_for_an_outlet(fixture):
    page = fixture("mironet_graficka_unfiltered.html")
    assert shops.parse_page(page, GRAFICKA) == []  # new goods carry no outlet badge
    assert shops.has_products(page)  # ... which is how a caller tells this from an empty outlet


def test_empty_outlet_listing(fixture):
    page = fixture("mironet_bazar_graficka_empty.html")
    assert shops.parse_page(page, GRAFICKA) == [] and not shops.has_products(page)
    assert not shops.has_next_page(page, 1)


def test_paging_links(fixture):
    page = fixture("mironet_graficka_unfiltered.html")  # links PgID=1..5 and 17
    assert shops.has_next_page(page, 1) and shops.has_next_page(page, 4)
    assert not shops.has_next_page(page, 5)
    assert not shops.has_next_page(fixture("mironet_rozbaleno_graficka.html"), 1)


def test_a_product_listed_twice_is_returned_once(fixture):
    page = fixture("mironet_rozbaleno_graficka.html")  # the page text twice, as real pages repeat blocks
    assert [i.external_id for i in shops.parse_page(page + page, GRAFICKA)] == ["839754", "1031826"]


def test_item_without_price_is_kept_as_unknown(fixture):
    page = fixture("mironet_bazar_pc.html").replace("Kč", "")  # a real page with its prices removed
    (aio,) = shops.parse_page(page, PC_BAZAR)
    assert (aio.price_czk, aio.price_kind) == (None, "unknown")


def test_item_without_name_or_link_is_skipped(fixture):
    page = fixture("mironet_bazar_pc.html")
    assert shops.parse_page(page.replace('name="NameItem"', 'name="Other"'), PC_BAZAR) == []
    assert shops.parse_page(page.replace("+dp805529/", "/"), PC_BAZAR) == []


def test_item_without_spec_line_or_image(fixture):
    page = fixture("mironet_bazar_pc.html").replace("item_b_popis", "x").replace("/Foto/s3/", "/Foto/x/")
    (aio,) = shops.parse_page(page, PC_BAZAR)
    assert aio.description == shops.SHIPPING_NOTE and aio.images == []


def test_how_each_outlet_item_is_normalized(fixture):
    """The shop section decides which normalizer reads a title (see the module doc)."""
    seen = {}
    for name, cat in [
        ("mironet_rozbaleno_graficka.html", GRAFICKA),
        ("mironet_rozbaleno_notebook.html", NOTEBOOK),
        ("mironet_rozbaleno_hdd.html", HDD),
    ]:
        for i in shops.parse_page(fixture(name), cat):
            r = normalize.classify(i.title, i.section, i.description)
            seen[i.external_id] = (r.category, r.model, r.excluded_reason)
    assert seen["1031826"] == ("gpu", "arc a750", None)  # an unboxed GPU gets a pricing key
    assert seen["839754"] == ("gpu", None, "out_of_scope")  # a Quadro never does, but is still stored
    assert seen["809984"] == ("hdd", "hdd35 enterprise sas 22tb", None)  # a 22 TB SAS disk
    assert seen["800538"] == ("hdd", None, "accessory")  # an M.2 enclosure in the disk category
    assert seen["894722"][0] == "office" and seen["894722"][2] is None  # a ProBook
    assert seen["848917"][2] == "out_of_scope"  # a MacBook


@pytest.mark.parametrize(
    "section", sorted({c.section for c in shops.CATEGORIES} - {"graficka", "pc", "notebook", "hdd"})
)
def test_watch_only_categories_are_not_priced(section):
    r = normalize.classify("Rozbaleno - Kingston FURY Beast 32GB DDR5", section)
    assert (r.model, r.excluded_reason) == (None, "watch_only")


def test_every_category_maps_onto_a_known_section():
    from deal_hunter.bazos import SECTIONS
    from deal_hunter.sources import CANONICAL_SECTIONS

    for cat in shops.CATEGORIES:
        assert cat.section in SECTIONS or cat.section in CANONICAL_SECTIONS
        assert cat.condition in shops.CONDITIONS
    assert shops.SOURCE in SHOP_SOURCES


# --- robots.txt -----------------------------------------------------------------------------------------

ALLOWED = [
    "https://www.mironet.cz/graficke-karty+fc14402/?EXPT=&pn10658[]=Rozbaleno",
    "https://www.mironet.cz/pocitace+fc1658/?EXPT=&pn10658[]=Bazar",
    "https://www.mironet.cz/pocitace+fc1658/?EXPT=&pn10658[]=Bazar&PgID=2",
]
NOT_ALLOWED = [
    "http://www.mironet.cz/graficke-karty+fc14402/?EXPT=&pn10658[]=Rozbaleno",  # not https
    "https://mironet.cz/graficke-karty+fc14402/?EXPT=&pn10658[]=Rozbaleno",  # another host
    "https://www.mironet.cz/graficke-karty+fc14402/",  # not filtered on an outlet
    "https://www.mironet.cz/graficke-karty+fc14402/?EXPT=&pn10658[]=Rozbaleno&sort=1",  # robots: sort=
    "https://www.mironet.cz/graficke-karty+fc14402/?EXPT=&pn10658[]=Nove",
    "https://www.mironet.cz/graficke-karty+fc14402/?EXPT=&pn10658[]=Rozbaleno&PgID=x",
    "https://www.mironet.cz/ajax/axFilterCount?CatId=14402&type=filter",  # robots: /ajax/
    "https://www.mironet.cz/Ajax/axFilterCount?CatId=14402",
    "https://www.mironet.cz/ProductDetail/HistoryPrice/839754",  # robots: price history
    "https://www.mironet.cz/vyprodej/?v=bazar",  # a landing page, no products on it
    "https://www.mironet.cz/graficke-karty/amd+c24768/?EXPT=&pn10658[]=Rozbaleno",  # not a fc category
]


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_urls(url):
    assert shops.allowed(url)


@pytest.mark.parametrize("url", NOT_ALLOWED)
def test_urls_that_are_not_allowed(url):
    assert not shops.allowed(url)


def test_every_url_we_build_passes_the_real_robots_txt(fixture):
    rules = robots_txt.parse(fixture("mironet_robots.txt"))
    assert rules and robots_txt.blocked(rules, "https://www.mironet.cz/ajax/x")  # the matcher itself
    assert robots_txt.blocked(rules, "https://www.mironet.cz/Ajax/x")
    assert robots_txt.blocked(rules, "https://www.mironet.cz/a+fc1/?EXPT=&sort=1")
    assert robots_txt.blocked(rules, "https://www.mironet.cz/a+fc1/?sort=1")
    for cat in shops.CATEGORIES:
        for page in (1, 2, 3):
            url = shops.category_url(cat, page)
            assert shops.allowed(url), url
            assert not robots_txt.blocked(rules, url), url
    for url in ALLOWED:
        assert not robots_txt.blocked(rules, url)


def test_category_urls():
    assert shops.category_url(GRAFICKA) == (
        "https://www.mironet.cz/graficke-karty+fc14402/?EXPT=&pn10658[]=Rozbaleno"
    )
    assert (
        shops.category_url(PC_BAZAR, 3)
        == "https://www.mironet.cz/pocitace+fc1658/?EXPT=&pn10658[]=Bazar&PgID=3"
    )
