"""Against a real (throwaway) database: votes, the feedback stream, follow-ups, flips, new prices."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

import httpx

from deal_hunter import feedback, normalize, pricing, zbozi
from deal_hunter.alerts import Vote
from deal_hunter.bazos import Listing
from deal_hunter.db import repository as repo
from deal_hunter.http import PoliteClient
from tests.conftest import fixture_text
from tests.test_triage import GOOD, FakeLLM
from tests.test_worker import FakeNtfy, make_ntfy, seed

NOW = datetime.now(UTC)
MESSAGE = (
    "Dobrý den, zajímám se o grafickou kartu Gigabyte RTX 3080 10GB. "
    "Mohl byste mi prosím sdělit, zda je stále k dispozici?"
)


def vote_event(i, body, at=None):
    at = at or NOW
    return {"id": f"v{i}", "time": int(at.timestamp()), "event": "message", "message": json.dumps(body)}


class FakeBazos:
    """Answers ad pages: the ad's own page (still there) or 410 (gone)."""

    def __init__(self, gone=False):
        self.gone, self.urls = gone, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if self.gone:
            return httpx.Response(410, text="Inzerát je již vymazán")
        html = fixture_text("detail_224857281.html")  # any real ad page; its id differs, so map it
        return httpx.Response(200, text=html)


def context(conn, site, bazos=None, llm=None):
    client = PoliteClient("test", min_delay=0, transport=httpx.MockTransport(bazos), sleep=lambda s: None)
    return feedback.Context(
        conn=conn,
        ntfy=make_ntfy(site),
        bazos=client if bazos is not None else None,
        llm=llm,
        cache_dir=None,
    )


def test_votes_feedback_lots_and_flips(conn):
    seed(conn)
    votes = [
        Vote("listing", "999", "up", "a", NOW),
        Vote("lot", "62388", "down", "b", NOW),
        Vote("listing", "999", "bought", "c", NOW),
        Vote("listing", "4242", "bought", "d", NOW),  # an ad we never saw: still logged, no price
    ]
    assert len(repo.save_votes(conn, votes)) == 4
    assert repo.save_votes(conn, votes) == []  # replayed messages are skipped
    rows = conn.execute("SELECT listing_id, lot_id, vote FROM feedback ORDER BY id").fetchall()
    assert rows == [(999, None, "up"), (None, "62388", "down")]
    flips = conn.execute(
        "SELECT listing_id, buy_price, model, sold_at FROM flips ORDER BY listing_id"
    ).fetchall()
    assert flips == [(999, 6500, "rtx 3080 10gb", None), (4242, None, None, None)]


def test_thumbs_up_sends_one_detailed_followup(conn):
    seed(conn)
    site = FakeNtfy()
    llm = FakeLLM(json.dumps(GOOD))  # only the on-demand triage: the message is a template
    ctx = context(conn, site, llm=llm)
    result = feedback.handle(ctx, vote_event(1, {"ad": 999, "vote": "up"}))
    assert "follow-up for ad 999 sent" in result
    push, copy = site.posts[-2:]
    assert push["title"] == "👍 Gigabyte RTX 3080 10GB Vision" and push["priority"] == 4
    assert push["attach"] == "https://www.bazos.cz/img/1/999/999.jpg"
    assert "💰 6 500 Kč · fair ~" in push["message"] and "Prodám, účtenka." in push["message"]
    assert "📍 Brno · 182 km · posted" in push["message"] or "📍 Brno · 182 km ·" in push["message"]
    assert "💬 If negotiating: ~6 000 Kč" in push["message"]
    assert "🤖 used" in push["message"] and push["message"].endswith(
        MESSAGE
    )  # triaged on demand (no deal row)
    open_ad, bought = push["actions"]
    assert open_ad == {"action": "view", "label": "Open ad", "url": push["click"], "clear": False}
    assert json.loads(bought["body"]) == {"ad": 999, "vote": "bought"}
    assert copy["title"] == "✉️ Message for the seller" and copy["message"] == MESSAGE
    assert copy["click"] == push["click"]
    # a second 👍 (or a replay) doesn't send it again
    assert "follow-up already sent" in feedback.handle(ctx, vote_event(2, {"ad": 999, "vote": "up"}))
    assert len(site.posts) == 2
    assert conn.execute("SELECT status FROM followups WHERE item_id = '999'").fetchone() == ("sent",)


def test_down_votes_old_votes_and_the_test_ad_send_nothing(conn):
    seed(conn)
    site = FakeNtfy()
    ctx = context(conn, site)
    assert feedback.handle(ctx, vote_event(1, {"ad": 999, "vote": "down"})) == "listing 999: down"
    old = vote_event(2, {"ad": 999, "vote": "up"}, NOW - timedelta(hours=7))
    assert "too old" in feedback.handle(ctx, old)
    assert "test push" in feedback.handle(ctx, vote_event(3, {"ad": 0, "vote": "up"}))
    assert feedback.handle(ctx, {"id": "k", "event": "keepalive"}) is None
    assert not site.posts


def test_test_votes_are_marked_and_not_stored(conn):
    seed(conn)
    site = FakeNtfy()
    ctx = context(conn, site)
    result = feedback.handle(ctx, vote_event(1, {"ad": 999, "vote": "up", "test": True}))
    assert result.startswith("TEST listing 999: up")
    assert site.posts[-2]["title"].startswith("TEST · 👍")
    assert json.loads(site.posts[-2]["actions"][1]["body"])["test"] is True
    assert site.posts[-1]["title"] == "TEST · ✉️ Message for the seller"
    assert conn.execute("SELECT count(*) FROM feedback").fetchone() == (0,)
    # the real 👍 still gets its own follow-up
    feedback.handle(ctx, vote_event(2, {"ad": 999, "vote": "up"}))
    assert len(site.posts) == 4 and not site.posts[-2]["title"].startswith("TEST")


def test_gone_ad_says_so(conn):
    seed(conn)
    site = FakeNtfy()
    bazos = FakeBazos(gone=True)
    ctx = context(conn, site, bazos=bazos)
    assert "is gone; said so" in feedback.handle(ctx, vote_event(1, {"ad": 999, "vote": "up"}))
    assert bazos.urls == ["https://pc.bazos.cz/inzerat/999/deal.php"]
    assert (
        site.posts[-1]["title"].startswith("👍 gone:") and "no longer available" in site.posts[-1]["message"]
    )
    assert conn.execute("SELECT gone_reason FROM listings WHERE id = 999").fetchone() == ("http_410",)
    assert conn.execute("SELECT status FROM followups").fetchone() == ("gone",)


def seed_lot(conn, status="active", end=None):
    from deal_hunter import nms

    lot = nms.Lot(
        lot_id="62388",
        url="https://nms.gov.cz/Home/AuctionDetail/62388",
        title="Soubor 14 ks HW vybavení - Notebooky",
        status=status,
        price=30000,
        bids=0,
        end_at=end or NOW + timedelta(days=2),
    )
    repo.upsert_lot_summary(conn, "nms", lot)
    conn.execute("UPDATE auction_lots SET content_hash = 'h', inspection = 'po domluvě', start_price = 30000")
    result = {
        "items": [
            {
                "model": "Dell Latitude 5511",
                "kind": "laptop",
                "quantity": 14,
                "condition": "untested",
                "missing": [],
                "quote": "14 ks",
            }
        ],
        "stated_units": 14,
    }
    repo.save_extraction(conn, "nms", "62388", "fake:1b", 1, "h", status="done", result=result)


def test_thumbs_up_on_a_lot(conn):
    for i in range(12):  # a fair price for the Latitude 5511
        repo.save_listing(
            conn,
            Listing(
                id=500 + i,
                url=f"https://pc.bazos.cz/inzerat/{500 + i}/x.php",
                title=f"Dell Latitude 5511 kus {chr(97 + i)}",
                price_czk=6000 + 10 * i,
                price_kind="fixed",
                seller=f"s{i}",
                section="notebook",
            ),
            detail=True,
        )
    normalize.run(conn)
    seed_lot(conn)
    site = FakeNtfy()
    ctx = context(conn, site)
    assert "follow-up for lot 62388 sent" in feedback.handle(
        ctx, vote_event(1, {"lot": "62388", "vote": "up"})
    )
    push = site.posts[-1]
    assert push["title"] == "👍 🏛 14× Dell Latitude 5511" and "max bid" in push["message"]
    assert "• 14× Dell Latitude 5511 (untested) → fair" in push["message"]
    assert conn.execute("SELECT lot_id, vote FROM feedback").fetchall() == [("62388", "up")]


def test_ended_lot_says_so(conn):
    seed_lot(conn, status="ended", end=NOW - timedelta(hours=1))
    site = FakeNtfy()
    assert "has ended" in feedback.handle(context(conn, site), vote_event(1, {"lot": "62388", "vote": "up"}))
    assert "auction lot is no longer available" in site.posts[-1]["message"]


def test_stream_resumes_from_the_last_message(conn):
    seed(conn)
    events = [
        {"id": "o", "event": "open", "time": int(NOW.timestamp())},
        vote_event(1, {"ad": 999, "vote": "down"}),
        {"id": "k", "event": "keepalive", "time": int(NOW.timestamp())},
        vote_event(2, {"ad": 5, "vote": "down"}),
    ]
    site = FakeNtfy(feedback=events)
    seen = []
    orig = site.__call__

    def spy(request):
        if request.method == "GET":
            seen.append(request.url.params.get("since"))
        return orig(request)

    ctx = context(conn, spy)
    assert feedback.listen_once(ctx) == 2
    assert repo.kv_get(conn, feedback.LAST_ID_KEY) == "v2"
    feedback.listen_once(ctx)  # reconnect: asks only for what came after v2 (the fake replays all)
    assert seen == ["all", "v2"]
    assert conn.execute("SELECT count(*) FROM feedback").fetchone() == (2,)


# --- new prices --------------------------------------------------------------------------------------


class FakeZbozi:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def __call__(self, request):
        self.urls.append(str(request.url))
        name = self.pages.get(str(request.url))
        if name is None:
            return httpx.Response(503)
        return httpx.Response(200, text=fixture_text(name))


def zbozi_client(site):
    return PoliteClient(
        "test", min_delay=0, transport=httpx.MockTransport(site), sleep=lambda s: None, retries=1
    )


def test_new_prices_are_fetched_weekly_and_checked(conn, monkeypatch):
    ok = "https://www.zbozi.cz/vyrobek/msi-geforce-rtx-3060-ventus-2x-12g-oc/"
    none = "https://www.zbozi.cz/vyrobek/msi-geforce-rtx-4060-ventus-2x-black-8g-oc/"
    wrong = "https://www.zbozi.cz/vyrobek/wrong/"
    broken = "https://www.zbozi.cz/vyrobek/broken/"
    monkeypatch.setattr(
        zbozi,
        "pages",
        lambda: {"rtx 3060 12gb": (ok,), "rtx 4060": (none,), "rtx 3060 ti": (wrong,), "rtx 4070": (broken,)},
    )
    site = FakeZbozi(
        {
            ok: "zbozi_rtx3060_ventus.html",
            none: "zbozi_rtx4060_no_offers.html",
            wrong: "zbozi_rtx3060_ventus.html",
        }
    )
    stats = zbozi.fetch_due(zbozi_client(site), conn, 7, 50)
    assert (stats.fetched, stats.priced, stats.rejected, stats.errors) == (3, 1, 1, 1)
    assert repo.latest_new_offers(conn) == {"rtx 3060 12gb": (10490, 1, "zbozi")}
    rows = {r["url"]: r for r in repo.new_price_rows(conn)}
    assert "normalizes to 'rtx 3060 12gb'" in rows[wrong]["error"] and rows[none]["min_price"] is None
    # nothing is due a day later; a failure is retried after 24 h; everything after a week
    assert zbozi.fetch_due(zbozi_client(site), conn, 7, 50).fetched == 0
    later = datetime.now(UTC) + timedelta(hours=25)
    assert zbozi.fetch_due(zbozi_client(site), conn, 7, 50, later).errors == 1
    week = datetime.now(UTC) + timedelta(days=8)
    assert zbozi.fetch_due(zbozi_client(site), conn, 7, 50, week).fetched == 3


def test_worker_prices_use_the_stored_new_price(conn):
    for i in range(5):
        repo.save_listing(
            conn,
            Listing(
                id=700 + i,
                url=f"https://pc.bazos.cz/inzerat/{700 + i}/x.php",
                title=f"RTX 3060 12GB #{i}",
                price_czk=5000 + 200 * i,
                price_kind="fixed",
                seller=f"s{i}",
            ),
            detail=True,
        )
    normalize.run(conn)
    repo.save_new_price(conn, "rtx 3060 12gb", "https://www.zbozi.cz/vyrobek/a/", "RTX 3060", 10000, 3)
    repo.save_new_price(conn, "rtx 3060 12gb", "https://www.zbozi.cz/vyrobek/b/", "RTX 3060", 9000, 1)
    fp = pricing.run(conn, save=False)["rtx 3060 12gb"]
    assert fp.new_price == 9000 and fp.new_detail == "1 shop" and fp.prior == 6750  # 9 000 x 0.75
    assert fp.raw_median == 5400 and fp.median == round(pricing.shrink(5400, fp.n, 6750, pricing.PRIOR_N))
    assert conn.execute("SELECT count(*) FROM fair_prices").fetchone() == (0,)  # save=False
