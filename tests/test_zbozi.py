"""Zboží.cz product pages: parsing saved real pages, the robots-safe URL check, the curated page list."""

import pytest

from deal_hunter import normalize, storage, zbozi
from deal_hunter.zbozi import Product, allowed, check, parse_product


def test_product_page_with_offers(fixture):
    p = parse_product(fixture("zbozi_rtx3060_ventus.html"))
    assert p == Product(
        "MSI GeForce RTX 3060 Ventus 2X 12G OC", "pocitace/pocitacove-komponenty/graficke-karty", 10490, 1
    )
    assert check(p, "rtx 3060 12gb") is None


def test_product_page_without_offers(fixture):
    p = parse_product(fixture("zbozi_rtx4060_no_offers.html"))
    assert p.name == "MSI GEFORCE RTX 4060 VENTUS 2X BLACK 8G OC"
    assert p.min_price is None and p.shops == 0
    assert check(p, "rtx 4060") is None


def test_pages_without_product_data(fixture):
    assert parse_product(fixture("zbozi_empty.html")) is None  # a static shell (product gone)
    assert parse_product("<html>nothing</html>") is None
    assert parse_product('<script id="__NEXT_DATA__" type="application/json">{oops</script>') is None


def test_wrong_product_or_category_is_rejected():
    p = Product("MSI GeForce RTX 3060 Ventus 2X 12G OC", zbozi.GPU_CATEGORY, 10490, 1)
    assert "normalizes to 'rtx 3060 12gb', not 'rtx 3060 ti'" in check(p, "rtx 3060 ti")
    assert "not graphics cards" in check(
        Product("Jablotron GDP-02", "telefony-navigace", 1999, 3), "rtx 3060 12gb"
    )


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://www.zbozi.cz/vyrobek/msi-geforce-rtx-3060-ventus-2x-12g-oc/", True),
        ("https://www.zbozi.cz/vyrobek/msi-geforce-rtx-3060-ventus-2x-12g-oc/?varianta=x", False),
        ("https://www.zbozi.cz/hledani/?q=rtx+3060", False),
        ("https://www.zbozi.cz/pocitace/pocitacove-komponenty/graficke-karty/", False),
        ("https://www.zbozi.cz/vyrobek/a,b/", False),
        ("http://www.zbozi.cz/vyrobek/msi/", False),
        ("https://www.zbozi.cz/action/123/", False),
    ],
)
def test_only_plain_product_pages_are_allowed(url, ok):
    assert allowed(url) is ok


def test_curated_pages_are_allowed_and_keyed_by_real_models():
    pages = zbozi.pages()
    assert len(pages) >= 15
    catalog = {m.name for m in normalize.load_catalog().models.values()}
    for model, urls in pages.items():
        assert urls and all(allowed(u) for u in urls)
        if model.startswith(zbozi.DISK_KEYS):
            want_model, want_tb = storage.split_capacity_key(model) or (model, None)
            for url in urls:  # disk slugs carry the product name: it must give the pooled key...
                slug = url.rstrip("/").rsplit("/", 1)[1].replace("-", " ")
                norm = normalize.classify(slug, "hdd")
                assert norm.model == want_model, url
                if want_tb is not None:  # ...and the capacity the key itself claims
                    got_tb = (norm.features or {}).get("capacity_tb")
                    assert got_tb is not None and abs(got_tb - want_tb) <= 0.05 * want_tb, url
            continue
        base = model.rsplit(" ", 1)[0] if model.endswith("gb") else model
        assert base in catalog, model
        # the slug must not name another card (part numbers in slugs can confuse the normalizer, so
        # None is fine here; the product *name* is checked against the model on every read)
        for url in urls:
            slug = url.rstrip("/").rsplit("/", 1)[1].replace("-", " ")
            assert normalize.classify(slug, "graficka").model in (model, base, None), url


def test_disk_product_page(fixture):
    p = parse_product(fixture("zbozi_ironwolf_8tb.html"))
    assert p == Product("Seagate IronWolf 8 TB (ST8000VN004)", zbozi.DISK_CATEGORY, 8290, 29)
    assert check(p, "hdd35 nas 8tb") is None
    assert "not 12 TB" in check(p, "hdd35 nas 12tb")  # same class, wrong capacity
    assert "not 'hdd35 enterprise'" in check(p, "hdd35 enterprise 8tb")  # wrong class
    assert "not internal hard disks" in check(Product(p.name, zbozi.GPU_CATEGORY, 1, 1), "hdd35 nas 8tb")
    disks = [m for m in zbozi.pages() if m.startswith(zbozi.DISK_KEYS)]
    assert len(disks) >= 7 and sum(len(zbozi.pages()[m]) for m in disks) >= 10
