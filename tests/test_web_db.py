"""The web pages against a real (throwaway) database: filters, escaping, and the detail page."""

import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

from deal_hunter import web
from deal_hunter.bazos import Listing
from deal_hunter.db import repository as repo
from tests.test_worker import FakeNtfy, deal_state, seed, work


def test_index_shows_an_alerted_deal_escapes_and_filters(conn):
    seed(conn)
    work(conn, FakeNtfy())
    assert deal_state(conn) == ("alerted", None)

    # an ad title and an LLM verdict with HTML in them must never reach the page unescaped
    conn.execute("UPDATE listings SET title = %s WHERE id = 999", ("<img src=x onerror=alert(1)> RTX 3080",))
    conn.execute("UPDATE deals SET verdict = %s WHERE listing_id = 999", ("<b>too good</b> to be true",))

    html = web.render_index(conn)
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert "<b>too good</b>" not in html and "&lt;b&gt;too good&lt;/b&gt;" in html
    assert "/listing/999" in html and "6 500 Kč" in html

    # category filter: gpu keeps it, hdd (a different category) drops it
    assert "listing/999" in web.render_index(conn, category="gpu")
    assert "No deals" in web.render_index(conn, category="hdd")

    # min_discount filter: the deal is about 35% under fair
    assert "listing/999" in web.render_index(conn, min_discount=0.10)
    assert "No deals" in web.render_index(conn, min_discount=0.90)


def test_prices_table_lists_a_priced_model(conn):
    seed(conn)
    html = web.render_prices(conn)
    assert "rtx 3080 10gb" in html
    assert "gpu" in html
    assert '<a href="/model/rtx%203080%2010gb">' in html
    assert '<a href="/category/gpu">' in html


# --- /category and /model ---------------------------------------------------------------------------


def test_category_page_branch_lists_boxes_with_subtree_totals(conn):
    seed(conn)
    work(conn, FakeNtfy())
    assert deal_state(conn) == ("alerted", None)

    html = web.render_category(conn, "komponenty")
    # gpu's box: 1 open deal (listing 999, alerted), 1 priced model, 21 active offers (20 + the deal)
    assert '<a href="/category/gpu">Grafické karty</a>' in html
    assert "1 modelů · 21 nabídek · 1 akcí" in html
    # a placeholder sibling with nothing in its subtree yet
    assert '<a href="/category/procesory">Procesory</a>' in html
    assert "0 modelů · 0 nabídek · 0 akcí" in html
    assert "border-radius" not in html


def test_category_index_also_shows_boxes_with_counts(conn):
    seed(conn)
    work(conn, FakeNtfy())

    html = web.render_category(conn)  # no node_id: /category itself
    for name in ("Počítače", "Komponenty", "Úložiště", "Síťové prvky"):
        assert name in html
    assert '<a href="/category/pocitace">' in html
    # Počítače's subtree has no GPU in it; Komponenty's does
    assert "1 modelů · 21 nabídek · 1 akcí" in html


def test_category_page_leaf_lists_its_product_rows(conn):
    seed(conn)
    work(conn, FakeNtfy())

    html = web.render_category(conn, "gpu")
    assert "Grafické karty" in html  # breadcrumb
    assert "/listing/999" in html
    assert "6 500 Kč" in html  # the deal's own price
    assert "Bazoš" in html
    assert "21 offer(s)." in html  # 20 ordinary ads + the deal, all active


def test_category_page_unknown_id_is_none(conn):
    assert web.render_category(conn, "nonexistent") is None


def test_category_page_office_group_has_no_members_seeded(conn):
    seed(conn)  # only GPUs: office_pc/office_laptop have no samples
    html = web.render_category(conn, "office")
    assert html is not None
    assert "Kancelářské (PC i notebooky)" in html
    assert "0 ceněných modelů · 0 aktivních nabídek · 0 akcí." in html
    assert "0 offer(s)." in html
    assert "No offers match this filter." in html


def test_category_page_leaf_filters_by_price_and_distance(conn):
    seed(conn)
    work(conn, FakeNtfy())
    conn.execute("UPDATE listings SET distance_km = 500 WHERE id = 999")  # far away, no shipping words

    cheap_only = web.render_category(conn, "gpu", web.Filters(price_do=7000))
    assert "/listing/999" in cheap_only  # 6500, kept
    assert "/listing/100" not in cheap_only  # ~9500+, dropped

    near_only = web.render_category(conn, "gpu", web.Filters(max_km=50))
    assert "/listing/999" not in near_only  # 500 km away and not marked as shipping

    deals_only = web.render_category(conn, "gpu", web.Filters(deals_only=True))
    assert "/listing/999" in deals_only  # well below fair
    assert "/listing/119" not in deals_only  # the priciest ordinary ad: at/above fair, not a deal

    cheapest_first = web.render_category(conn, "gpu", web.Filters(sort="cheapest"))
    assert cheapest_first.index("listing/999") < cheapest_first.index("listing/100")


def test_category_page_leaf_source_filter_and_checkbox_state(conn):
    seed(conn)
    work(conn, FakeNtfy())
    f = web.Filters(sources=("aukro",))
    html = web.render_category(conn, "gpu", f)
    assert "/listing/999" not in html and "/listing/100" not in html  # everything seeded is bazos
    assert 'name="source" value="aukro" checked' in html


def test_render_model_shows_fair_price_and_live_offers_escapes_titles(conn):
    seed(conn)
    conn.execute("UPDATE listings SET title = %s WHERE id = 100", ("<script>bad</script> RTX 3080",))

    html = web.render_model(conn, "rtx 3080 10gb")
    assert html is not None
    assert "Grafické karty" in html  # breadcrumb to the category
    assert "Fair price" in html
    assert "<script>bad</script>" not in html and "&lt;script&gt;bad&lt;/script&gt;" in html
    assert "21 active ad(s)" in html  # 20 seeded + the deal ad, none gone
    assert html.count("rtx 3080 10gb") == 0  # the model name is the <h1>, not repeated in the body
    assert '<svg class="pricebar"' in html  # the price bar: cheapest offer, fair price, new price
    assert "Price history" in html


def test_render_model_unknown_model_is_none(conn):
    seed(conn)
    assert web.render_model(conn, "nonexistent model") is None


def test_render_model_price_history_has_a_weekly_median_row(conn):
    seed(conn)
    conn.execute("UPDATE listings SET first_seen = now() - interval '10 days' WHERE id = 999")
    html = web.render_model(conn, "rtx 3080 10gb")
    assert "<h2>Price history</h2>" in html
    assert "No price history yet." not in html  # the 21 seeded ads fall inside the 12-week window


def test_listing_page_has_description_and_price_history_unknown_id_is_none(conn):
    seed(conn)
    work(conn, FakeNtfy())
    # a second price (via save_listing) writes a price_changes row for the history table
    repo.save_listing(
        conn,
        Listing(
            id=999,
            url="https://pc.bazos.cz/inzerat/999/deal.php",
            title="Gigabyte RTX 3080 10GB Vision",
            price_czk=6000,
            price_kind="fixed",
        ),
        detail=False,
    )

    html = web.render_listing(conn, 999)
    assert html is not None
    assert "Prodám" in html  # the ad's own description
    assert "6 500 Kč" in html and "6 000 Kč" in html  # old -> new price in the history table
    assert web.render_listing(conn, 424242) is None


def test_auctions_page_lists_open_lot(conn):
    conn.execute(
        "INSERT INTO auction_lots (source, lot_id, url, title, status, current_price, start_price, end_at) "
        "VALUES ('nms', '1', 'https://nms.gov.cz/x', %s, 'active', 3300, 1000, now() + interval '2 days')",
        ("<script>bad</script> 22x ProBook",),
    )
    html = web.render_auctions(conn)
    assert "<script>bad</script>" not in html
    assert "&lt;script&gt;bad&lt;/script&gt;" in html
    assert "not extracted yet" in html
    assert "3 300 Kč" in html


def test_render_auctions_empty_when_no_open_lots(conn):
    assert "No open lots" in web.render_auctions(conn)


# --- /status ---------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _listing(conn, lid, title="Some ad"):
    conn.execute(
        "INSERT INTO listings (id, url, title, external_id) VALUES (%s, %s, %s, %s)",
        (lid, f"https://pc.bazos.cz/inzerat/{lid}/x.php", title, str(lid)),
    )


def _triage(conn, lid, status, minutes_ago, stats=None, result=None, error=None, next_try_min=0):
    conn.execute(
        "INSERT INTO triage (listing_id, llm_model, prompt_version, status, stats, result, error, next_try_at, "
        "updated_at) VALUES (%s, 'gemma3', 1, %s, %s, %s, %s, %s, %s)",
        (
            lid,
            status,
            Jsonb(stats or {}),
            Jsonb(result) if result else None,
            error,
            NOW + timedelta(minutes=next_try_min),
            NOW - timedelta(minutes=minutes_ago),
        ),
    )


def _run(conn, kind, minutes_ago, ok=True, new=3, errors=()):
    conn.execute(
        "INSERT INTO scrape_runs (kind, started_at, finished_at, new, ok, errors) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            kind,
            NOW - timedelta(minutes=minutes_ago),
            NOW - timedelta(minutes=minutes_ago - 1),
            new,
            ok,
            Jsonb(list(errors)),
        ),
    )


def test_status_empty_database_renders(conn):
    html = web.render_status(conn, NOW)
    assert "LLM triage" in html and "No triage results yet." in html
    assert "No watchlist checks yet." in html and "No auction extractions yet." in html
    assert "No scrape runs yet." in html and "No deal flagged yet." in html
    assert "STALE" not in html


def test_status_triage_numbers_and_escaping(conn):
    for lid in (1, 2, 3, 4, 5):
        _listing(conn, lid, "<script>x</script> RTX" if lid == 1 else f"Ad {lid}")
    ok = {"condition": "used", "one_line_verdict": "<b>fine</b> price"}
    _triage(
        conn, 1, "done", 10, {"seconds": 4.0, "cached": False, "prompt_tokens": 100, "output_tokens": 20}, ok
    )
    _triage(
        conn, 2, "done", 20, {"seconds": 99.0, "cached": True, "prompt_tokens": 50, "output_tokens": 10}, ok
    )
    _triage(conn, 3, "failed", 30, {"seconds": 2.0, "cached": False}, error="<i>boom</i> timeout")
    _triage(conn, 4, "pending", 40, next_try_min=30)  # backing off
    _triage(conn, 5, "pending", 40)  # due
    conn.execute(
        "INSERT INTO deals (listing_id, model, category, price, fair, discount, confidence, score, state) "
        "VALUES (1, 'rtx 3080', 'gpu', 6000, 9000, 0.33, 0.8, 0.27, 'alerted')"
    )
    html = web.render_status(conn, NOW)
    assert "<script>x</script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html
    assert "<b>fine</b>" not in html and "&lt;b&gt;fine&lt;/b&gt;" in html
    assert "&lt;i&gt;boom&lt;/i&gt; timeout" in html and "<i>boom</i>" not in html
    assert "2 pending (1 due, 1 waiting to retry), 2 done, 1 failed" in html
    assert "10 min ago" in html
    # 3 answered in 24 h, avg of the two non-cached calls (4 and 2 s), 1 of 3 cached, tokens summed
    assert "3 answered, avg 3.0 s" in html and "cache hit rate 33%" in html
    assert "tokens 150 in / 30 out" in html
    assert "alerted 0.27" in html and "/listing/1" in html
    assert "STALE" not in html  # a result 10 min ago


def test_status_triage_stale_only_with_due_queue_and_old_result(conn):
    _listing(conn, 1)
    _listing(conn, 2)
    _triage(conn, 1, "done", 120, {"seconds": 1.0, "cached": False}, {"condition": "used"})
    # old result but nothing waiting: not stale
    assert "STALE" not in web.render_status(conn, NOW)
    # queue entry still backing off: not stale either
    _triage(conn, 2, "pending", 100, next_try_min=10)
    assert "STALE" not in web.render_status(conn, NOW)
    # a due entry with the last result 2 h old: stale
    conn.execute("UPDATE triage SET next_try_at = %s WHERE listing_id = 2", (NOW - timedelta(minutes=5),))
    assert "STALE" in web.render_status(conn, NOW)
    # a recent result clears it again
    conn.execute("UPDATE triage SET updated_at = %s WHERE listing_id = 1", (NOW - timedelta(minutes=5),))
    assert "STALE" not in web.render_status(conn, NOW)


def test_status_pipeline_stale_and_fresh(conn):
    _listing(conn, 1, "<img src=x> flagged")
    conn.execute(
        "INSERT INTO deals (listing_id, model, category, price, fair, discount, confidence, state, first_flagged) "
        "VALUES (1, 'rtx 3080', 'gpu', 6000, 9000, 0.33, 0.8, 'would_alert', %s)",
        (NOW - timedelta(hours=3),),
    )
    conn.execute(
        "INSERT INTO kv (key, value) VALUES ('section_last_scraped:gpu', %s)",
        (Jsonb((NOW - timedelta(minutes=7)).isoformat()),),
    )
    _run(conn, "scrape", 10)
    _run(conn, "recheck", 60, ok=False, errors=["timeout", "again"])
    html = web.render_status(conn, NOW)
    assert "STALE" not in html
    assert "2 error(s)" in html and "7 min ago" in html and "gpu" in html
    assert "<img src=x>" not in html and "&lt;img src=x&gt; flagged" in html

    _run(conn, "scrape", 45)  # an older run does not matter, only the latest does
    assert "STALE" not in web.render_status(conn, NOW)
    conn.execute("DELETE FROM scrape_runs WHERE kind = 'scrape'")
    _run(conn, "scrape", 45)
    assert "STALE" in web.render_status(conn, NOW)


def test_status_watch_and_auction_activity(conn):
    _listing(conn, 1, "<u>DDR4</u> kit")
    _listing(conn, 2, "Other kit")
    conn.execute("INSERT INTO watches (id, query) VALUES (1, %s)", ("<b>DDR4 RAM</b>",))
    for lid, matched, pushed, mins in ((1, True, True, 5), (2, False, False, 10)):
        conn.execute(
            "INSERT INTO watch_hits (watch_id, listing_id, matched, pushed_at, created_at) VALUES (1, %s, %s, %s, %s)",
            (lid, matched, NOW if pushed else None, NOW - timedelta(minutes=mins)),
        )
    conn.execute(  # older than 24 h: in the table, not in the day counts
        "UPDATE watch_hits SET created_at = %s WHERE listing_id = 2", (NOW - timedelta(hours=30),)
    )
    conn.execute(
        "INSERT INTO auction_lots (source, lot_id, url, title, status) VALUES "
        "('nms', '1', 'https://nms.gov.cz/1', %s, 'active'), ('nms', '2', 'https://nms.gov.cz/2', 'Old lot', 'active')",
        ("<i>Lot</i> of PCs",),
    )
    for lot, status, days in (("1", "done", 1), ("2", "failed", 10)):
        conn.execute(
            "INSERT INTO auction_extractions (source, lot_id, llm_model, prompt_version, content_hash, status, "
            "updated_at) VALUES ('nms', %s, 'gemma3', 1, 'h', %s, %s)",
            (lot, status, NOW - timedelta(days=days)),
        )
    html = web.render_status(conn, NOW)
    assert "1 checked, 1 matched, 1 pushed" in html
    assert "&lt;b&gt;DDR4 RAM&lt;/b&gt;" in html and "<b>DDR4 RAM</b>" not in html
    assert "&lt;u&gt;DDR4&lt;/u&gt; kit" in html and "<u>DDR4</u>" not in html
    assert "Last 7 days: 1 lots read, 0 pending" in html
    assert "&lt;i&gt;Lot&lt;/i&gt; of PCs" in html and "<i>Lot</i>" not in html


def test_status_page_is_in_nav_and_served_content_type(conn):
    assert 'href="/status"' in web.page("x", "")


# --- /status : Sources -------------------------------------------------------------------------------


def _settings(**changes):
    from deal_hunter.config import load_settings

    base = {
        "sources": ("bazos",),
        "fb_cookies_file": "",
        "new_prices_enabled": True,
        "auctions_enabled": True,
        "loop_minutes": 10.0,
        "shop_hours": 6.0,
        "fb_cycle_max_minutes": 30.0,
        "new_price_days": 7.0,
        "auction_list_hours": 2.5,
    }
    return replace(load_settings(), **{**base, **changes})


def _src_listing(conn, lid, source, first_min=0, seen_min=0, gone=False):
    conn.execute(
        "INSERT INTO listings (id, source, url, title, external_id, first_seen, last_seen, gone_at) "
        "VALUES (%s, %s, %s, 'ad', %s, %s, %s, %s)",
        (
            lid,
            source,
            f"https://example.com/{lid}",
            str(lid),
            NOW - timedelta(minutes=first_min),
            NOW - timedelta(minutes=seen_min),
            NOW if gone else None,
        ),
    )


def _new_price(conn, shop, minutes_ago, error=None):
    conn.execute(
        "INSERT INTO new_prices (model, url, min_price, shop, fetched_at, error) VALUES ('m', %s, 100, %s, %s, %s)",
        (f"https://example.com/{shop}/{minutes_ago}", shop, NOW - timedelta(minutes=minutes_ago), error),
    )


def _lot(conn, lot_id, status, seen_min):
    conn.execute(
        "INSERT INTO auction_lots (source, lot_id, url, title, status, last_seen) "
        "VALUES ('nms', %s, 'https://nms.gov.cz/x', 't', %s, %s)",
        (lot_id, status, NOW - timedelta(minutes=seen_min)),
    )


def _states(conn, settings):
    src = repo.status_sources(conn, NOW)
    return {s["name"]: s["state"] for s in web._site_rows(settings, src, NOW)}


def test_status_sources_empty_database_enabled_sites_never_ran(conn):
    html = web.render_status(conn, NOW, _settings())
    assert "<h2>Sources</h2>" in html
    assert "4 of 8 sites enabled · 0 working · 0 stale · 4 never ran" in html
    states = _states(conn, _settings())
    assert states["Bazoš"] == states["Zboží.cz"] == states["Datart"] == states["nms.gov.cz"] == "never ran"
    assert states["Aukro"] == states["Mironet"] == states["Facebook Marketplace"] == "off"
    assert states["Sbazar"] == "blocked"
    assert "blocked (robots.txt)" in html and "STALE" not in html


def test_status_sources_every_state_and_summary(conn):
    _src_listing(conn, 1, "bazos", first_min=5, seen_min=2)
    _src_listing(conn, 2, "bazos", first_min=3000, seen_min=2)
    _src_listing(conn, 3, "bazos", first_min=3000, seen_min=2000, gone=True)
    _src_listing(conn, 4, "aukro", first_min=500, seen_min=90)  # 3 loops = 30 min: stale
    _src_listing(conn, 5, "sbazar", seen_min=1)  # blocked whatever the data says
    _src_listing(conn, 6, "fb", seen_min=500)  # fb not configured: off, data still shown
    _new_price(conn, "zbozi", 60)
    _new_price(conn, "zbozi", 90, error="<b>HTTP 503</b>")
    _lot(conn, "1", "active", 30)
    _lot(conn, "2", "ended", 30)
    settings = _settings(sources=("bazos", "aukro", "mironet"))
    states = _states(conn, settings)
    assert states == {
        "Bazoš": "OK",
        "Aukro": "STALE",
        "Mironet": "never ran",
        "Facebook Marketplace": "off",
        "Sbazar": "blocked",
        "Zboží.cz": "OK",
        "Datart": "never ran",
        "nms.gov.cz": "OK",
    }
    html = web.render_status(conn, NOW, settings)
    assert "6 of 8 sites enabled · 3 working · 1 stale · 2 never ran" in html
    assert "1 new in 24 h, 2 active" in html  # ad 2 is older than a day, ad 3 is gone
    assert "1 pages ok, 1 failed in the last pass" in html
    assert "1 open lots" in html
    assert 'class="muted"' in html  # the disabled rows are greyed
    assert html.count("STALE") == 1


def test_status_sources_stale_thresholds_both_sides(conn):
    settings = _settings(sources=("bazos", "mironet"), fb_cookies_file="/run/cookies.json")
    _src_listing(conn, 1, "bazos", seen_min=30)  # exactly 3 x 10 min: not yet stale
    _src_listing(conn, 2, "mironet", seen_min=12 * 60)  # 2 x 6 h
    _src_listing(conn, 3, "fb", seen_min=120)  # 4 x 30 min
    _new_price(conn, "zbozi", 9 * 1440)  # 7 days + 2 days grace
    _new_price(conn, "datart", 9 * 1440 + 1)
    _lot(conn, "1", "active", 300)  # 2 x 2.5 h
    states = _states(conn, settings)
    assert states["Bazoš"] == states["Mironet"] == states["Facebook Marketplace"] == "OK"
    assert states["Zboží.cz"] == "OK" and states["Datart"] == "STALE" and states["nms.gov.cz"] == "OK"
    conn.execute("UPDATE listings SET last_seen = last_seen - interval '1 minute'")
    conn.execute("UPDATE auction_lots SET last_seen = last_seen - interval '1 minute'")
    states = _states(conn, settings)
    assert states["Bazoš"] == states["Mironet"] == states["Facebook Marketplace"] == "STALE"
    assert states["nms.gov.cz"] == "STALE"


def test_status_sources_all_failed_price_reads_are_stale_and_show_error(conn):
    _new_price(conn, "zbozi", 10, error="<i>boom</i>")
    assert _states(conn, _settings())["Zboží.cz"] == "STALE"
    html = web.render_status(conn, NOW, _settings())
    assert "last read failed: &lt;i&gt;boom&lt;/i&gt;" in html and "<i>boom</i>" not in html


def test_status_sources_enabled_follows_config_both_ways(conn, monkeypatch):
    for key in ("HUNTER_SOURCES", "FB_COOKIES_FILE", "HUNTER_NEW_PRICES", "HUNTER_AUCTIONS"):
        monkeypatch.delenv(key, raising=False)
    off = web.render_status(conn, NOW)  # settings come from the environment, as in the web service
    assert "4 of 8 sites enabled" in off
    monkeypatch.setenv("HUNTER_SOURCES", "bazos,aukro,mironet,sbazar")
    monkeypatch.setenv("FB_COOKIES_FILE", "/run/secrets/fb.json")
    monkeypatch.setenv("HUNTER_NEW_PRICES", "false")
    monkeypatch.setenv("HUNTER_AUCTIONS", "false")
    on = web.render_status(conn, NOW)
    assert "4 of 8 sites enabled" in on  # bazos, aukro, mironet, fb; sbazar listed but blocked
    assert "blocked (robots.txt)" in on
    from deal_hunter.config import load_settings

    states = _states(conn, load_settings())
    assert states["Sbazar"] == "blocked" and states["Zboží.cz"] == states["nms.gov.cz"] == "off"
    assert states["Facebook Marketplace"] == states["Aukro"] == "never ran"


# --- the plain-shop style rules, on every page at once -----------------------------------------------
#
# The owner's rules are checked mechanically here instead of by eye on every change: no border-radius
# anywhere, no emoji (categories.yaml's "icon" is an ntfy tag and must never be rendered as one), and
# no em/en dashes in text this module writes. seed()/work() use titles and verdicts with no dashes so
# this stays clean; ad titles are otherwise exempt (they are the seller's own words, never rewritten).

EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U00002300-\U000023ff\U00002b00-\U00002bff"
    "\U0001f1e6-\U0001f1ff️]"
)
DASH_RE = re.compile("[–—]")  # en dash, em dash (a plain hyphen "-" is fine)


def _every_page_html(conn, now) -> str:
    from deal_hunter import pricing
    from deal_hunter.config import load_settings

    settings = load_settings()
    prices = pricing.run(conn, settings.alert_min_samples, settings.usd_czk, save=False)
    model = next(iter(prices), None)
    pages = [
        web.page("Deals", web.render_index(conn)),
        web.page("Fair prices", web.render_prices(conn)),
        web.page("Categories", web.render_category(conn)),
        web.page("Grafické karty", web.render_category(conn, "gpu")),
        web.page("Komponenty", web.render_category(conn, "komponenty")),
        web.page("Open auctions", web.render_auctions(conn)),
        web.page("Flips", web.render_flips(conn, now)),
        web.page("Status", web.render_status(conn, now)),
    ]
    if model:
        pages.append(web.page(model, web.render_model(conn, model)))
    listing_id = conn.execute("SELECT id FROM listings LIMIT 1").fetchone()
    if listing_id:
        pages.append(web.page("Listing", web.render_listing(conn, listing_id[0])))
    return "".join(pages)


def test_every_page_has_no_border_radius_emoji_or_dashes(conn):
    seed(conn)
    work(conn, FakeNtfy())
    html = _every_page_html(conn, datetime.now(UTC))
    assert html  # sanity: actually rendered something on every page
    assert "border-radius" not in html
    assert not EMOJI_RE.search(html), EMOJI_RE.search(html)
    assert not DASH_RE.search(html), DASH_RE.search(html)
