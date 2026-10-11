"""The web pages against a real (throwaway) database: filters, escaping, and the detail page."""

import os

import pytest

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
