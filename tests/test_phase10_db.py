"""Phase 10 part 1 (watchlists) against a real (throwaway) database: watch CRUD, the scan + push
cycle, LLM-check dedup, the command listener, and the new scrape sections' cadence gate."""

import json
import os
from datetime import datetime

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

import httpx

from deal_hunter import alerts, watchlist
from deal_hunter.alerts import PRAGUE
from deal_hunter.bazos import Listing
from deal_hunter.db import repository as repo
from tests.test_triage import FakeLLM

GOOD_ANSWER = {
    "matches": True,
    "product_type": "RAM kit",
    "brand": "Kingston",
    "model": "HX432C16FBK2/32",
    "condition": "used",
    "price_czk": 1200,
    "evidence": "Kingston DDR4 2x16GB 3200MHz",
}


class FakeNtfy:
    def __init__(self, feedback=()):
        self.posts, self.feedback = [], list(feedback)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.posts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"msg{len(self.posts)}"})
        lines = "\n".join(json.dumps(e) for e in self.feedback)
        return httpx.Response(200, text=lines)


def make_ntfy(site):
    return alerts.Ntfy(
        "http://ntfy.lan",
        "tk_pub",
        "deals",
        "https://ntfy.example",
        "deals-feedback",
        "tk_fb",
        httpx.MockTransport(site),
        watch_topic="watch",
        watch_token="tk_watch",
    )


def ram_listing(conn, listing_id=501, title="Kingston DDR4 2x16GB 3200MHz", section="pamet", **kw):
    item = Listing(
        id=listing_id,
        url=f"https://pc.bazos.cz/inzerat/{listing_id}/ram.php",
        title=title,
        price_czk=kw.pop("price_czk", 1200),
        price_kind="fixed",
        section=section,
        description=kw.pop("description", "Prodám paměti, nikdy nepoužité."),
        **kw,
    )
    repo.save_listing(conn, item, detail=True)
    return listing_id


# --- watch CRUD + commands -------------------------------------------------------------------------


def test_insert_list_stop_watch(conn):
    wid = repo.insert_watch(conn, "ddr4 16gb", {"include": [["ddr4", "16gb"]]}, max_price=1500, tag="homelab")
    rows = repo.list_watches(conn)
    assert len(rows) == 1 and rows[0]["id"] == wid and rows[0]["active"] is True
    assert repo.get_watch(conn, wid)["query"] == "ddr4 16gb"
    assert repo.get_watch(conn, 999) is None
    assert repo.set_watch_active(conn, wid, False) is True
    assert repo.set_watch_active(conn, 999, False) is False
    assert repo.list_watches(conn, active_only=True) == []
    assert repo.list_watches(conn)[0]["active"] is False


def test_seed_is_idempotent(conn):
    assert watchlist.seed(conn) == len(watchlist.SEEDS)
    assert watchlist.seed(conn) == 0
    rows = repo.list_watches(conn)
    assert len(rows) == len(watchlist.SEEDS)
    assert all(r["created_by"] == "seed" and r["tag"] == "homelab" for r in rows)


def test_execute_commands(conn):
    from deal_hunter.watchlist import Command, execute

    reply = execute(conn, Command("add", query="ddr4 16gb", max_price=1500, radius_km=50, tag="homelab"))
    assert reply.startswith("Watching #1: ddr4 16gb") and "max 1500 Kč" in reply and "#homelab" in reply
    assert "#1" in execute(conn, Command("list")) and "ddr4 16gb" in execute(conn, Command("list"))
    assert execute(conn, Command("stop", watch_id=1)) == "Stopped watch #1."
    assert "(stopped)" in execute(conn, Command("list"))
    assert execute(conn, Command("stop", watch_id=42)) == "No watch #42."
    assert execute(conn, Command("help")) == watchlist.HELP_TEXT
    assert "Didn't understand" in execute(conn, Command("unknown", error="nope"))


# --- scan + match + push ----------------------------------------------------------------------------


def test_scan_without_llm_prefilters_but_records_nothing(conn):
    repo.insert_watch(conn, "ddr4 16gb", watchlist.SEEDS[0]["keywords"])
    ram_listing(conn)
    stats = watchlist.scan_and_match(conn, None, None, hours=48)
    assert stats.scanned == 1 and stats.prefiltered == 1 and stats.llm_checked == 0
    assert repo.pending_watch_matches(conn) == []
    # nothing was recorded, so a second scan sees the same listing again (bounded by `hours`, not stuck)
    stats2 = watchlist.scan_and_match(conn, None, None, hours=48)
    assert stats2.scanned == 1


def test_scan_matches_and_dedups_the_llm_call(conn):
    repo.insert_watch(conn, "ddr4 16gb", watchlist.SEEDS[0]["keywords"])
    ram_listing(conn)
    llm = FakeLLM(json.dumps(GOOD_ANSWER))
    stats = watchlist.scan_and_match(conn, llm, None, hours=48)
    assert stats.prefiltered == 1 and stats.llm_checked == 1 and stats.matched == 1
    pending = repo.pending_watch_matches(conn)
    assert len(pending) == 1 and pending[0]["result"]["brand"] == "Kingston"
    # already recorded: the next scan doesn't touch the LLM again (an empty FakeLLM would raise if called)
    stats2 = watchlist.scan_and_match(conn, FakeLLM(), None, hours=48)
    assert stats2.scanned == 0 and stats2.llm_checked == 0


def test_scan_prefilter_miss_never_calls_the_llm(conn):
    repo.insert_watch(conn, "ddr4 16gb", watchlist.SEEDS[0]["keywords"])
    ram_listing(conn, listing_id=502, title="RTX 3060 12GB grafická karta")
    stats = watchlist.scan_and_match(conn, FakeLLM(), None, hours=48)
    assert stats.scanned == 1 and stats.prefiltered == 0 and stats.llm_checked == 0


def test_push_matches_sends_and_marks_pushed(conn):
    repo.insert_watch(conn, "ddr4 16gb", watchlist.SEEDS[0]["keywords"], tag="homelab")
    ram_listing(conn)
    watchlist.scan_and_match(conn, FakeLLM(json.dumps(GOOD_ANSWER)), None, hours=48)
    site = FakeNtfy()
    now = datetime(2026, 10, 11, 14, 0, tzinfo=PRAGUE)  # well outside the default 23-7 quiet hours
    pushed = watchlist.push_matches(conn, make_ntfy(site), now, 23, 7, max_per_day=10)
    assert pushed == 1 and repo.pending_watch_matches(conn) == []
    assert site.posts[0]["topic"] == "deals"
    assert "Kingston DDR4" in site.posts[0]["message"] and "1 200 Kč" in site.posts[0]["message"]
    # already pushed: a second call sends nothing more
    assert watchlist.push_matches(conn, make_ntfy(FakeNtfy()), now, 23, 7, max_per_day=10) == 0


def test_push_matches_respects_quiet_hours(conn):
    repo.insert_watch(conn, "ddr4 16gb", watchlist.SEEDS[0]["keywords"])
    ram_listing(conn)
    watchlist.scan_and_match(conn, FakeLLM(json.dumps(GOOD_ANSWER)), None, hours=48)
    site = FakeNtfy()
    now = datetime(2026, 10, 11, 2, 0, tzinfo=PRAGUE)  # inside 23-7 quiet hours
    assert watchlist.push_matches(conn, make_ntfy(site), now, 23, 7, max_per_day=10) == 0
    assert site.posts == [] and len(repo.pending_watch_matches(conn)) == 1


def test_push_matches_respects_the_daily_cap(conn):
    repo.insert_watch(conn, "ddr4 16gb", watchlist.SEEDS[0]["keywords"])
    ram_listing(conn, listing_id=501)
    ram_listing(conn, listing_id=502, title="Corsair DDR4 2x16GB 3200MHz")
    llm = FakeLLM(json.dumps(GOOD_ANSWER), json.dumps(GOOD_ANSWER))
    watchlist.scan_and_match(conn, llm, None, hours=48)
    assert len(repo.pending_watch_matches(conn)) == 2
    site = FakeNtfy()
    now = datetime(2026, 10, 11, 14, 0, tzinfo=PRAGUE)
    pushed = watchlist.push_matches(conn, make_ntfy(site), now, 23, 7, max_per_day=1)
    assert pushed == 1 and len(repo.pending_watch_matches(conn)) == 1


def test_tick_scans_and_pushes_in_one_step(conn):
    repo.insert_watch(conn, "ddr4 16gb", watchlist.SEEDS[0]["keywords"])
    ram_listing(conn)
    site = FakeNtfy()
    now = datetime(2026, 10, 11, 14, 0, tzinfo=PRAGUE)
    summary = watchlist.tick(
        conn, FakeLLM(json.dumps(GOOD_ANSWER)), None, make_ntfy(site), now, 48, 23, 7, 10
    )
    assert "1 matched, 1 pushed" in summary
    assert len(site.posts) == 1


# --- the command listener ---------------------------------------------------------------------------


def watch_event(i, text, at=None):
    return {"id": f"w{i}", "event": "message", "message": text, "time": 0}


def test_handle_add_replies_on_deals_topic(conn):
    site = FakeNtfy()
    ctx = watchlist.Context(conn, make_ntfy(site))
    reply = watchlist.handle(ctx, watch_event(1, "watch ddr4 16gb max 1500 #homelab"))
    assert reply.startswith("Watching #1") and "#homelab" in reply
    assert site.posts[-1]["topic"] == "deals" and reply in site.posts[-1]["message"]
    assert len(repo.list_watches(conn)) == 1


def test_handle_ignores_non_message_events(conn):
    site = FakeNtfy()
    ctx = watchlist.Context(conn, make_ntfy(site))
    assert watchlist.handle(ctx, {"event": "keepalive"}) is None
    assert site.posts == []


def test_listen_once_resumes_from_the_last_message(conn):
    events = [
        {"id": "o", "event": "open", "time": 0},
        watch_event(1, "list"),
        {"id": "k", "event": "keepalive", "time": 0},
        watch_event(2, "stop 1"),
    ]
    site = FakeNtfy(feedback=events)
    seen = []
    orig = site.__call__

    def spy(request):
        if request.method == "GET":
            seen.append(request.url.params.get("since"))
        return orig(request)

    ctx = watchlist.Context(conn, make_ntfy(spy))
    assert watchlist.listen_once(ctx) == 2
    assert repo.kv_get(conn, watchlist.LAST_ID_KEY) == "w2"
    watchlist.listen_once(ctx)  # reconnect: asks for everything after w2 (the fake replays all)
    assert seen == ["all", "w2"]


# --- new scrape sections' cadence gate ----------------------------------------------------------------


def test_section_due_gates_on_elapsed_time(conn):
    assert repo.section_due(conn, "pamet", 45) is True  # no kv row yet
    repo.mark_section_scraped(conn, "pamet")
    assert repo.section_due(conn, "pamet", 45) is False
    # back-date the kv row past the cadence
    import datetime as dt

    repo.kv_set(
        conn, "section_last_scraped:pamet", (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    )
    assert repo.section_due(conn, "pamet", 45) is True


def test_watch_section_is_normalized_as_excluded(conn):
    from deal_hunter import normalize

    listing_id = ram_listing(conn)
    assert normalize.run(conn) == 1
    row = conn.execute(
        "SELECT category, model, excluded_reason FROM normalized WHERE listing_id = %s", (listing_id,)
    ).fetchone()
    assert row == ("watch", None, "watch_only")
