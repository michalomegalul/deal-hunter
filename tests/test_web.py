"""Pure formatting/escaping helpers in web.py (no database needed)."""

from deal_hunter import web


def test_page_escapes_the_title():
    html = web.page("<script>alert(1)</script>", "<p>body</p>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<p>body</p>" in html  # body is trusted, pre-rendered HTML


def test_kc_formats_thousands_and_handles_none():
    assert web.kc(123456) == "123 456 Kč"
    assert web.kc(None) == "-"


def test_pct_and_km():
    assert web.pct(0.234) == "23%"
    assert web.pct(None) == "-"
    assert web.km(182.4) == "182 km"
    assert web.km(None) == "-"


def test_matches_category_groups_office():
    assert web._matches_category("office_pc", "office")
    assert web._matches_category("office_laptop", "office")
    assert not web._matches_category("gpu", "office")
    assert web._matches_category("gpu", "gpu")
