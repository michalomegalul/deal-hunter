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


def test_categories_tuple_has_the_four_priced_leaves_and_the_office_group():
    assert set(web.CATEGORIES) == {"gpu", "office_pc", "office_laptop", "hdd", "office"}


def test_breadcrumb_links_every_ancestor_but_the_last():
    html = web._breadcrumb("office_laptop")
    assert '<a href="/category/pocitace">Počítače</a>' in html
    assert '<a href="/category/notebooky">Notebooky</a>' in html
    assert "Kancelářské</p>" in html  # the node itself: text, not a link
    assert '<a href="/category/office_laptop">' not in html


def test_breadcrumb_of_a_group_has_no_trail():
    assert web._breadcrumb("office") == "Kancelářské (PC i notebooky)"


def test_breadcrumb_of_an_unknown_id_falls_back_to_the_id():
    assert web._breadcrumb("nonexistent") == "nonexistent"


def test_render_category_index_lists_the_top_level_roots():
    html = web.render_category_index()
    for name in ("Počítače", "Komponenty", "Úložiště", "Síťové prvky"):
        assert name in html
    assert '<a href="/category/pocitace">' in html
