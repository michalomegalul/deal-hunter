"""Phase 9 against a real (throwaway) database: distances, the pickup rule in the worker, disks."""

import json
import os
from dataclasses import replace

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

from deal_hunter import geo
from deal_hunter.bazos import Listing
from deal_hunter.db import repository as repo
from deal_hunter.geo import DistanceRules
from tests.test_triage import GOOD, FakeLLM
from tests.test_worker import FakeNtfy, deal_state, rules, seed, work


def test_distances_are_stored_and_follow_changes(conn):
    seed(conn)
    assert geo.update_distances(conn, geo.PRAGUE_CENTRE) == 21
    assert geo.update_distances(conn, geo.PRAGUE_CENTRE) == 0  # nothing new
    km, src = conn.execute("SELECT distance_km, distance_src FROM listings WHERE id = 999").fetchone()
    assert src == "psc" and round(km) == 182
    unknown = conn.execute("SELECT distance_km, distance_src FROM listings WHERE id = 100").fetchone()
    assert unknown == (None, "unknown")  # no town, no PSČ
    # the seller moves the ad to Prague: recomputed on the next cycle
    item = Listing(
        999, "https://pc.bazos.cz/inzerat/999/deal.php", "Gigabyte RTX 3080 10GB Vision", 6500, "fixed"
    )
    item.location, item.psc = "Praha 4", "140 00"
    repo.save_listing(conn, item, detail=False)
    assert geo.update_distances(conn, geo.PRAGUE_CENTRE) == 1
    assert conn.execute("SELECT round(distance_km) FROM listings WHERE id = 999").fetchone()[0] < 3
    # a new home recomputes everything
    assert geo.update_distances(conn, geo.home_point("602 00", None, None)) == 21


def test_out_of_range_deal_is_kept_as_would_alert(conn):
    seed(conn)  # the deal is in Brno, 182 km from Prague, and says nothing about shipping
    site = FakeNtfy()
    stats = work(conn, site, distance=DistanceRules())
    assert stats.pushed == 0 and stats.would_alert == 1 and not site.posts
    assert deal_state(conn) == ("would_alert", "out of range: Brno 182 km > 40 km, no shipping")
    assert conn.execute("SELECT ships FROM deals WHERE listing_id = 999").fetchone() == (False,)


def test_out_of_range_deal_that_ships_is_pushed_with_the_distance(conn):
    seed(conn)
    conn.execute("UPDATE listings SET description = 'Prodám, účtenka. Pošlu Zásilkovnou.' WHERE id = 999")
    site = FakeNtfy()
    stats = work(conn, site, distance=DistanceRules())
    assert stats.pushed == 1
    assert "📍 Brno · 182 km (ships)" in site.posts[0]["message"]


def test_the_llm_decides_when_the_ad_only_hints_at_shipping(conn):
    seed(conn)
    says_ships = FakeLLM(json.dumps({**GOOD, "ships": True}))
    # 'kurýrem' is no clear offer for the regex, but it is about sending: the LLM's yes counts
    conn.execute("UPDATE listings SET description = 'Prodám, účtenka. Kurýrem po dohodě.' WHERE id = 999")
    work(conn, FakeNtfy(), llm=says_ships, distance=DistanceRules())
    assert deal_state(conn) == ("alerted", None)


def test_an_llm_yes_without_any_word_about_shipping_is_ignored(conn):
    seed(conn)  # 'Prodám, účtenka.'
    work(conn, FakeNtfy(), llm=FakeLLM(json.dumps({**GOOD, "ships": True})), distance=DistanceRules())
    assert deal_state(conn)[0] == "would_alert"


def test_wider_radius_for_a_category(conn):
    seed(conn)
    site = FakeNtfy()
    work(conn, site, distance=DistanceRules(pickup_km_by_category=(("gpu", 200.0),)))
    assert deal_state(conn) == ("alerted", None)


def test_digest_mentions_out_of_range_deals(conn):
    from datetime import UTC, datetime

    from deal_hunter.alerts import PRAGUE, digest_text

    seed(conn)
    work(conn, FakeNtfy(), distance=DistanceRules())
    sunday = datetime(2026, 10, 11, 18, 30, tzinfo=PRAGUE).astimezone(UTC)
    _, message = digest_text(
        conn, {}, repo.data_since(conn), replace(rules(), distance=DistanceRules()), sunday
    )
    assert "Out of pickup range (would-alert): 1, e.g. Brno 182 km" in message
