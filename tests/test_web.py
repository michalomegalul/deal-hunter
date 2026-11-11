"""Pure formatting/escaping helpers in web.py (no database needed)."""

from collections import Counter

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


def test_kc_range_uses_a_hyphen_not_a_dash():
    assert web.kc_range(5112, 6933) == "5 112 - 6 933 Kč"
    assert "–" not in web.kc_range(5112, 6933)
    assert "—" not in web.kc_range(5112, 6933)
    assert web.kc_range(100, 200, per_tb=True) == "100 - 200 Kč/TB"


def test_cached_prices_reuses_within_ttl_and_reset_forces_recompute(monkeypatch):
    # `_cached_prices` is the TTL cache every pricing.run() page view (`/prices`, `/category/<leaf>`,
    # `/hledat`, `/model/<key>`, `/auctions`) now goes through instead of recomputing every time.
    from deal_hunter import pricing

    calls = []
    monkeypatch.setattr(pricing, "run", lambda *a, **kw: calls.append(1) or {"x": 1})
    web.reset_pricing_cache()

    first = web._cached_prices(None, 10, 25.0)
    second = web._cached_prices(None, 10, 25.0)
    assert len(calls) == 1 and first is second  # the second call reused the cached result

    web.reset_pricing_cache()
    web._cached_prices(None, 10, 25.0)
    assert len(calls) == 2  # reset forces a recompute

    web._cached_prices(None, 10, 30.0)  # a different usd_czk is a different cache key
    assert len(calls) == 3


def test_matches_category_groups_office():
    assert web._matches_category("office_pc", "office")
    assert web._matches_category("office_laptop", "office")
    assert not web._matches_category("gpu", "office")
    assert web._matches_category("gpu", "gpu")


def test_categories_tuple_has_the_priced_leaves_and_the_office_group():
    assert set(web.CATEGORIES) == {
        "gpu",
        "office_pc",
        "office_laptop",
        "hdd",
        "apple",
        "apple_desktop",
        "herni",
        "ostatni",
        "office",
    }


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


# --- the plain shop look: square boxes, source names, thumbnails, GET filters ------------------------


def test_category_grid_lists_square_boxes_with_subtree_totals():
    html = web._category_grid(("gpu", "procesory"), Counter({"gpu": 2}), Counter({"gpu": 3}), {"gpu": 10})
    assert '<div class="catbox"><a href="/category/gpu">Grafické karty</a>' in html
    assert "3 modelů" in html and "10 nabídek" in html and "2 akcí" in html
    assert "0 modelů" in html and "0 nabídek" in html and "0 akcí" in html  # procesory: nothing yet
    assert "border-radius" not in html


def test_source_name_known_and_fallback():
    assert web.source_name("bazos") == "Bazoš"
    assert web.source_name("aukro") == "Aukro"
    assert web.source_name("mironet") == "Mironet"
    assert web.source_name("fb") == "Facebook"
    assert web.source_name("weird") == "Weird"
    assert web.source_name(None) == "-"


def test_thumb_renders_the_ad_image_over_a_grey_placeholder_with_onerror():
    html = web.thumb(["https://example.com/a.jpg"])
    assert html == (
        '<div class="thumb"><img src="https://example.com/a.jpg" loading="lazy" alt="" '
        'onerror="this.remove()"></div>'
    )
    assert 'onerror="this.remove()"' in html  # a renewed/removed ad's broken thumbnail just vanishes
    assert web.thumb([]) == '<div class="thumb empty"></div>'
    assert web.thumb(None) == '<div class="thumb empty"></div>'


def test_row_shows_discount_text_only_when_cheaper_than_fair():
    cheap = web._row(
        listing_id=1,
        url="https://example.com/1",
        title="RTX 3060",
        price=6000,
        fair=8000,
        images=None,
        distance_km=None,
        ships=True,
        source="bazos",
        first_seen=None,
    )
    assert "pod férovou cenou" in cheap
    assert "posílá" in cheap
    assert "Bazoš" in cheap
    fair_priced = web._row(
        listing_id=2,
        url="https://example.com/2",
        title="RTX 3060",
        price=9000,
        fair=8000,
        images=None,
        distance_km=12.3,
        ships=False,
        source="aukro",
        first_seen=None,
    )
    assert "pod férovou cenou" not in fair_priced
    assert "12 km" in fair_priced


def test_row_marks_unknown_location_and_shows_the_town():
    unknown = web._row(
        listing_id=3,
        url="https://example.com/3",
        title="RTX 3060",
        price=5000,
        fair=None,
        images=None,
        distance_km=None,
        ships=False,
        source="bazos",
        first_seen=None,
    )
    assert "místo neznámé" in unknown  # kept in the list, never a bare "-"

    with_town = web._row(
        listing_id=4,
        url="https://example.com/4",
        title="RTX 3060",
        price=5000,
        fair=None,
        images=None,
        distance_km=12.3,
        ships=False,
        source="bazos",
        first_seen=None,
        town="Brno",
    )
    assert "<span>Brno</span>" in with_town  # the town, never a seller name


def test_offer_row_defaults_fair_and_ships_from_the_row_dict():
    # a listing row as repo.listings_in_categories/listings_by_models/search_listings_title return
    # it, with `fair`/`ships` already attached by _apply_price_filters -- the shape _offer_row
    # dedupes the /category, /hledat (both branches) call sites against.
    row = {
        "id": 1,
        "url": "https://example.com/1",
        "title": "RTX 3060",
        "price_czk": 6000,
        "fair": 8000,
        "images": None,
        "distance_km": None,
        "ships": True,
        "source": "bazos",
        "first_seen": None,
    }
    html = web._offer_row(row)
    assert "pod férovou cenou" in html and "posílá" in html and "Bazoš" in html


def test_offer_row_overrides_fair_and_ships_when_given():
    # render_model's offers have neither key (no _apply_price_filters pass): it passes both.
    row = {
        "id": 2,
        "url": "https://example.com/2",
        "title": "RTX 3060",
        "price_czk": 6000,
        "images": None,
        "distance_km": None,
        "source": "bazos",
        "first_seen": None,
    }
    html = web._offer_row(row, fair=8000, ships=True)
    assert "pod férovou cenou" in html and "posílá" in html


def test_offer_row_passes_town_through():
    row = {
        "id": 3,
        "url": "https://example.com/3",
        "title": "RTX 3060",
        "price_czk": 6000,
        "fair": None,
        "images": None,
        "distance_km": None,
        "ships": False,
        "source": "bazos",
        "first_seen": None,
    }
    assert "<span>Brno</span>" in web._offer_row(row, town="Brno")


def test_parse_filters_ignores_bad_values_and_unknown_sources():
    f = web.parse_filters(
        {
            "price_od": ["not a number"],
            "price_do": ["20000"],
            "max_km": ["-5"],
            "source": ["bazos", "madeup"],
            "deals": ["1"],
            "sort": ["cheapest"],
        }
    )
    assert f.price_od is None  # unparseable
    assert f.price_do == 20000
    assert f.max_km is None  # negative distances are rejected
    assert f.sources == ("bazos",)  # the unknown source is dropped
    assert f.deals_only is True
    assert f.sort == "cheapest"


def test_parse_filters_defaults_to_nothing_set():
    f = web.parse_filters({})
    assert f == web.Filters()
    assert f.sort == "newest"
    assert f.sources == ()
    assert f.deals_only is False
    assert f.town is None


def test_parse_filters_reads_mesto_and_strips_it():
    assert web.parse_filters({"mesto": ["  Brno  "]}).town == "Brno"
    assert web.parse_filters({"mesto": [""]}).town is None
    assert web.parse_filters({}).town is None


# --- /hledat: mapping a free-text query to pricing models (normalize.py, no DB needed) ---------------


def test_guess_models_rtx_3060_is_every_vram_variant_plus_the_ambiguous_bare_name():
    assert web._guess_models("rtx 3060") == ["rtx 3060 8gb", "rtx 3060 12gb", "rtx 3060"]


def test_guess_models_with_vram_named_is_just_that_one_key():
    assert web._guess_models("RTX3060 12GB") == ["rtx 3060 12gb"]


def test_guess_models_macbook_air_m2_is_the_apple_model():
    assert web._guess_models("macbook air m2") == ["macbook air m2 13"]


def test_guess_models_thinkpad_t480_is_the_office_laptop_model():
    assert web._guess_models("thinkpad t480") == ["thinkpad t480"]


def test_guess_models_nonsense_text_is_empty_so_the_caller_falls_back():
    assert web._guess_models("asdkjaslkdj nonsense") == []


# --- /hledat and /category: distance text, the "Místo" town resolution and its form field ------------


def test_place_bits_known_distance_unknown_and_shipping():
    assert web._place_bits(12.3, False) == "12 km"
    assert web._place_bits(12.3, True) == "12 km, posílá"
    assert web._place_bits(None, True) == "posílá"
    assert web._place_bits(None, False) == "místo neznámé"


def test_filters_form_shows_a_query_box_only_when_asked():
    with_q = web._filters_form("/hledat", web.Filters(), query="rtx 3060")
    assert 'name="q" value="rtx 3060"' in with_q
    without_q = web._filters_form("/category/gpu", web.Filters())
    assert 'name="q"' not in without_q
    assert 'name="mesto"' in without_q  # Místo is on every filter form, search or category


def test_filters_form_keeps_the_typed_town():
    html = web._filters_form("/category/gpu", web.Filters(town="Brno"))
    assert 'name="mesto" value="Brno"' in html


# --- bad GET input never 500s: ?min_discount= and /listing/<id> go through `_num` --------------------


def test_num_parses_a_plain_listing_id():
    assert web._num("999", int) == 999
    assert web._num("", int) is None
    assert web._num(None, int) is None
    assert web._num("-5", int) is None  # a listing id is never negative


def test_num_rejects_a_superscript_digit_that_isdigit_lets_through_but_int_cannot_parse():
    # str.isdigit() is True for '²' (it is a Unicode digit), but int('²') raises ValueError; the old
    # `/listing/<id>` check used isdigit() and then int(), which 500ed on exactly this input.
    assert "²".isdigit()
    assert web._num("²", int) is None


def test_num_rejects_unparseable_discount_percentage():
    assert web._num("abc", float) is None


def test_db_unreachable_page_has_no_border_radius_emoji_or_dash():
    html = web._db_unreachable()
    assert "border-radius" not in html
    assert "–" not in html and "—" not in html  # en dash, em dash
    assert "nedostupn" in html.lower()
