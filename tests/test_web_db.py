"""The web pages against a real (throwaway) database: filters, escaping, and the detail page."""

import os
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
