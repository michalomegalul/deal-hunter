"""Database tests. Skipped unless DATABASE_URL points at a Postgres server.

They run in a separate throwaway database (`<name>_test`), never the one in DATABASE_URL.
"""

import os
from datetime import date

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from deal_hunter import bazos, scraper
from deal_hunter.bazos import Listing
from deal_hunter.db import repository as repo
from deal_hunter.http import PoliteClient
from tests.conftest import fixture_text


@pytest.fixture(scope="module")
def test_url():
    base = conninfo_to_dict(os.environ["DATABASE_URL"])
    name = f"{base.get('dbname', 'hunter')}_test"
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(**{**base, "dbname": name})
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def conn(test_url):
    with repo.connect(test_url, autocommit=True) as conn:
        repo.migrate(conn)
        conn.execute("TRUNCATE listings, price_changes, normalized, scrape_runs RESTART IDENTITY CASCADE")
        yield conn


def ad(id=1, price=1000, kind="fixed", **kw):
    return Listing(
        id=id,
        url=f"https://pc.bazos.cz/inzerat/{id}/x.php",
        title=f"RTX {id}",
        price_czk=price,
        price_kind=kind,
        **kw,
    )


def test_migrate_is_idempotent(conn):
    assert repo.migrate(conn) == []
    tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"listings", "price_changes", "normalized", "scrape_runs", "http_throttle"} <= tables


def test_save_new_then_update_records_price_change(conn):
    assert repo.save_listing(conn, ad(price=5000, seller="Pepa", views=3), detail=False) == (True, False)
    assert repo.save_listing(conn, ad(price=5000, views=10), detail=False) == (False, False)
    assert repo.save_listing(conn, ad(price=4500), detail=False) == (False, True)
    assert repo.save_listing(conn, ad(price=None, kind="negotiable"), detail=False) == (False, True)
    row = conn.execute("SELECT price_czk, price_kind, seller, views FROM listings WHERE id = 1").fetchone()
    assert row == (None, "negotiable", "Pepa", 10)
    changes = conn.execute(
        "SELECT old_price, new_price, old_kind, new_kind FROM price_changes ORDER BY id"
    ).fetchall()
    assert changes == [(5000, 4500, "fixed", "fixed"), (4500, None, "fixed", "negotiable")]


def test_detail_overrides_list_preview(conn):
    repo.save_listing(conn, ad(description="short preview..."), detail=False)
    assert repo.detailed_ids(conn, [1, 2]) == set()
    full = ad(
        description="the whole text",
        images=["a.jpg", "b.jpg"],
        raw_category="graficka",
        posted_at=date(2026, 10, 9),
    )
    repo.save_listing(conn, full, detail=True)
    assert repo.detailed_ids(conn, [1, 2]) == {1}
    # a later list-page sighting must not clobber the full description or images
    repo.save_listing(conn, ad(description="short preview..."), detail=False)
    row = conn.execute("SELECT description, images, raw_category, posted_at FROM listings").fetchone()
    assert row == ("the whole text", ["a.jpg", "b.jpg"], "graficka", date(2026, 10, 9))


def test_gone_and_back(conn):
    repo.save_listing(conn, ad(), detail=True)
    assert repo.mark_gone(conn, 1, "http_410") is True
    assert repo.mark_gone(conn, 1, "http_410") is False
    assert conn.execute("SELECT gone_reason FROM listings").fetchone()[0] == "http_410"
    repo.save_listing(conn, ad(), detail=False)  # seen again: not gone after all
    assert conn.execute("SELECT gone_at, gone_reason FROM listings").fetchone() == (None, None)


def test_due_for_recheck(conn):
    for i in (1, 2, 3):
        repo.save_listing(conn, ad(i), detail=True)
    conn.execute("UPDATE listings SET last_seen = now() - interval '7 hours' WHERE id IN (1, 2)")
    conn.execute("UPDATE listings SET last_seen = now() - interval '9 hours' WHERE id = 2")
    repo.mark_gone(conn, 1, "http_404")
    assert [r[0] for r in repo.due_for_recheck(conn, 6, 10)] == [2]


class FakeBazos:
    """Serves the saved fixtures as if it were pc.bazos.cz."""

    def __init__(self, gone_ids=(), detail="detail_224601918.html"):
        self.gone_ids = set(gone_ids)
        self.detail = detail
        self.paths = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.paths.append(url)
        if url == bazos.RSS_URL:
            return httpx.Response(200, text=fixture_text("rss.xml"))
        if url == bazos.category_url(0):
            return httpx.Response(200, text=fixture_text("category_0.html"))
        if url == bazos.category_url(1):
            return httpx.Response(200, text=fixture_text("category_20.html"))
        for section in ("pc", "notebook"):
            if url == bazos.rss_url(section):
                return httpx.Response(200, text=fixture_text(f"rss_{section}.xml"))
            if url == bazos.category_url(0, section):
                return httpx.Response(200, text=fixture_text(f"{section}_0.html"))
        if any(f"/{s}/" in url for s in bazos.SECTIONS):
            return httpx.Response(200, text="<html><body>no ads</body></html>")
        listing_id = bazos.ad_id(url)
        if listing_id in self.gone_ids:
            return httpx.Response(410, text=fixture_text("detail_gone.html"))
        # every ad gets the same detail page body; parse_detail takes the id from the URL
        return httpx.Response(200, text=fixture_text(self.detail))


def fake_client(handler):
    return PoliteClient("test", min_delay=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)


GPU_ONLY = {"graficka": 2}


def test_scrape_once_fetches_details_only_for_new_ads(conn):
    site = FakeBazos()
    stats = scraper.scrape_once(fake_client(site), conn, pages=2, sections=GPU_ONLY)
    n = conn.execute("SELECT count(*), count(detail_fetched_at) FROM listings").fetchone()
    assert n[0] == n[1] == stats.new > 40
    assert not stats.errors
    detail_requests = sum("/inzerat/" in p for p in site.paths)
    assert detail_requests == stats.new

    site2 = FakeBazos()
    stats2 = scraper.scrape_once(fake_client(site2), conn, pages=2, sections=GPU_ONLY)
    assert stats2.new == 0 and stats2.updated == stats.new
    assert not any("/inzerat/" in p for p in site2.paths)  # nothing new: no detail pages fetched
    run = conn.execute("SELECT kind, ok, new FROM scrape_runs ORDER BY id").fetchall()
    assert run == [("scrape", True, stats.new), ("scrape", True, 0)]


def test_backfill_stops_at_empty_page(conn):
    site = FakeBazos()
    stats = scraper.backfill(fake_client(site), conn, pages=50)
    assert stats.new == 40
    assert sum("/graficka/" in p for p in site.paths) == 3  # pages 0, 1 and the empty page 2


def test_recheck_marks_removed_ads_gone(conn):
    repo.save_listing(conn, ad(11), detail=True)
    repo.save_listing(conn, ad(12), detail=True)
    conn.execute("UPDATE listings SET last_seen = now() - interval '1 day'")
    stats = scraper.recheck(fake_client(FakeBazos(gone_ids={11})), conn, older_than_hours=6, limit=100)
    assert (stats.seen, stats.gone, stats.updated) == (2, 1, 1)
    rows = dict(conn.execute("SELECT id, gone_reason FROM listings").fetchall())
    assert rows == {11: "http_410", 12: None}


def test_redirects(conn):
    def handler(request):
        url = str(request.url)
        if "/inzerat/21/" in url and "new-slug" not in url:
            return httpx.Response(301, headers={"Location": "/inzerat/21/new-slug.php"})
        if "/inzerat/22/" in url:
            return httpx.Response(302, headers={"Location": "https://pc.bazos.cz/graficka/"})
        return httpx.Response(200, text=fixture_text("detail_224601918.html"))

    client = fake_client(handler)
    listing, gone, _ = scraper.fetch_ad(client, "https://pc.bazos.cz/inzerat/21/old.php")
    assert listing.id == 21 and listing.url.endswith("/inzerat/21/new-slug.php") and gone is None
    listing, gone, _ = scraper.fetch_ad(client, "https://pc.bazos.cz/inzerat/22/x.php")
    assert listing is None and gone == "redirect"


def test_db_throttle_spaces_requests(test_url):
    slept = []
    with repo.connect(test_url, autocommit=True) as c:
        repo.migrate(c)
        throttle = scraper.DbThrottle(c, 3.0, sleep=slept.append)
        throttle.wait_turn()
        throttle.wait_turn()
    assert len(slept) == 1 and 2.5 < slept[0] <= 3.0


def test_normalize_run_and_review(conn, monkeypatch):
    from deal_hunter import normalize

    for i, title in enumerate(["MSI RTX 3060 12GB", "Koupím RTX 3080", "Grafická karta"], start=1):
        repo.save_listing(
            conn, Listing(id=i, url=f"https://pc.bazos.cz/inzerat/{i}/x.php", title=title), True
        )
    assert normalize.run(conn) == 3
    assert normalize.run(conn) == 0  # nothing new
    rows = conn.execute(
        "SELECT listing_id, category, model, features, excluded_reason FROM normalized ORDER BY 1"
    )
    assert rows.fetchall() == [
        (1, "gpu", "rtx 3060 12gb", {"vram_gb": 12}, None),
        (2, "gpu", None, {}, "wanted"),
        (3, "gpu", None, {}, None),
    ]
    assert [r[1] for r in repo.unmatched(conn, 10)] == ["Grafická karta"]

    monkeypatch.setattr(normalize, "version", lambda: 999_999)  # a catalog bump redoes everything
    assert normalize.run(conn) == 3


def test_office_sections_fetch_details_only_in_scope(conn):
    from deal_hunter import normalize

    site = FakeBazos(detail="detail_224725687.html")  # a ThinkPad T14s
    stats = scraper.scrape_once(fake_client(site), conn, pages=1, sections={"pc": 1, "notebook": 1})
    assert not stats.errors
    rows = conn.execute("SELECT id, section, detail_fetched_at IS NOT NULL FROM listings").fetchall()
    assert {r[1] for r in rows} == {"pc", "notebook"}
    assert len(rows) == stats.new > 100  # 2 x (50 RSS + 20 page) minus the overlap
    titles = {}
    for section in ("pc", "notebook"):
        titles |= {i.id: (i.title, section) for i in bazos.parse_rss(fixture_text(f"rss_{section}.xml"))}
        titles |= {i.id: (i.title, section) for i in bazos.parse_category(fixture_text(f"{section}_0.html"))}
    detailed = {r[0] for r in rows if r[2]}
    in_scope = {
        i
        for i, (title, section) in titles.items()
        if normalize.classify(title, section).excluded_reason is None
    }
    assert detailed == in_scope and 5 < len(detailed) < len(rows) / 3
    assert sum("/inzerat/" in p for p in site.paths) == len(detailed)
    list_requests = [p for p in site.paths if "/inzerat/" not in p]
    assert len(list_requests) == 4  # 2 RSS feeds + 2 category pages

    normalize.run(conn)
    matched = conn.execute(
        "SELECT n.category, count(*) FROM normalized n WHERE n.model IS NOT NULL GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert matched and {c for c, _ in matched} <= {"office_laptop", "office_pc"}  # (all details are a T14s)
    # out-of-scope office ads (gaming PCs, MacBooks) are never re-checked for removal
    conn.execute("UPDATE listings SET last_seen = now() - interval '1 day'")
    assert {r[0] for r in repo.due_for_recheck(conn, 6, 10_000)} == in_scope
