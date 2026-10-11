"""Phase 9 against a real (throwaway) database: distances, the pickup rule in the worker, disks."""

import json
import os
from dataclasses import replace

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

from deal_hunter import geo, normalize
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


# --- hard disks ---------------------------------------------------------------------------------------


def disk(i, title, price, description="", section="hdd", **kw):
    return Listing(
        id=i,
        url=f"https://pc.bazos.cz/inzerat/{i}/disk.php",
        title=title,
        price_czk=price,
        price_kind="fixed",
        seller=f"seller {i}",
        description=description,
        location="Praha 4",
        psc="140 00",
        section=section,
        images=[f"https://www.bazos.cz/img/1/{i}.jpg"],
        **kw,
    )


def seed_disks(conn):
    """20 ordinary 8 TB and 4 TB NAS drives from 10 days ago (~3 000 / ~1 600 Kč)."""
    for i in range(20):
        repo.save_listing(conn, disk(2000 + i, f"WD Red Plus 8TB #{i}", 2800 + 20 * i), detail=True)
        repo.save_listing(conn, disk(3000 + i, f"Seagate IronWolf 4TB #{i}", 1500 + 10 * i), detail=True)
    conn.execute("UPDATE listings SET first_seen = now() - interval '10 days'")


DISK_GOOD = {
    **GOOD,
    "disk": {
        "power_on_hours": 9100,
        "reallocated_or_pending": 0,
        "health_pct": None,
        "smart_screenshot_mentioned": True,
        "tested": False,
        "warranty_until": None,
        "from_nas_or_server": True,
    },
}


def test_a_disk_deal_is_pushed_with_capacity_badges_and_smart(conn):
    seed_disks(conn)
    text = "Disk z NAS, SMART: 0 vadných sektorů, 9 100 hodin, výpis ve fotkách."
    repo.save_listing(conn, disk(999, "Seagate IronWolf 8TB ST8000VN004", 1900, text), detail=True)
    normalize.run(conn)
    site = FakeNtfy()
    stats = work(conn, site, llm=FakeLLM(json.dumps(DISK_GOOD)), distance=DistanceRules())
    assert stats.pushed == 1, deal_state(conn)
    push = site.posts[0]
    lines = push["message"].splitlines()
    assert lines[0].startswith("1 900 Kč vs ~") and lines[1] == "hdd35 nas"
    assert (
        lines[2]
        == "💾 8 TB · 238 Kč/TB · SATA ✓ · CMR ✓ · SMART: 9 100 h, 0 reallocated/pending, SMART screenshot, from a NAS/server"
    )
    assert "📍 Praha 4 · 2 km" in push["message"] and "floppy_disk" in push["tags"]


def test_disks_that_dont_fit_the_server_skip_the_llm(conn):
    seed_disks(conn)
    repo.save_listing(conn, disk(999, "Seagate IronWolf 4TB", 900), detail=True)  # cheap, but only 4 TB
    normalize.run(conn)
    llm = FakeLLM()  # no answers: any call would fail the test
    stats = work(conn, FakeNtfy(), llm=llm)
    assert stats.candidates >= 1 and not llm.calls
    assert deal_state(conn) == ("would_alert", "server fit: 4 TB < 8 TB")


def test_reallocated_sectors_drop_a_disk(conn):
    seed_disks(conn)
    text = "Disk má 2 přemapované sektory viz výpis Crystal disk info."
    repo.save_listing(conn, disk(999, "WD Red Plus 8TB", 1500, text), detail=True)
    normalize.run(conn)
    answer = {**DISK_GOOD, "disk": {**DISK_GOOD["disk"], "reallocated_or_pending": 2, "power_on_hours": None}}
    work(conn, FakeNtfy(), llm=FakeLLM(json.dumps(answer)))
    assert deal_state(conn) == ("dropped", "reallocated/pending sectors")


def test_multi_disk_ads_are_priced_per_drive(conn):
    seed_disks(conn)
    repo.save_listing(conn, disk(999, "3× WD Red Plus 8TB", 2000, "Cena 2 000 Kč/ks."), detail=True)
    repo.save_listing(
        conn, disk(998, "2x WD Red Plus 8TB", 2000, "Prodám dva disky."), detail=True
    )  # unclear
    repo.save_listing(
        conn, disk(997, "4x WD Red Plus 8TB", 8000, "Cena za všechny."), detail=True
    )  # 2 000 each
    normalize.run(conn)
    site = FakeNtfy()
    work(conn, site, llm=FakeLLM(json.dumps(DISK_GOOD), json.dumps(DISK_GOOD)))
    deals = dict(conn.execute("SELECT listing_id, price FROM deals").fetchall())
    assert deals == {999: 2000, 997: 2000}  # 998: per drive or for both? not priced at all
    pushed = [p for p in site.posts if "3×" in p["message"]]
    assert pushed and pushed[0]["message"].startswith("3× 2 000 Kč/ks vs ~")
    samples = {s.listing_id: s.price for s in repo.price_samples(conn, 60)}
    assert samples[997] == 2000 and 998 not in samples


def test_review_and_gate_know_the_disk_section(conn):
    from datetime import UTC, datetime

    from deal_hunter.alerts import gate_status
    from deal_hunter.pricing import estimate_all

    seed_disks(conn)
    normalize.run(conn)
    prices = estimate_all(repo.price_samples(conn, 60))
    # both the 8 TB and 4 TB drives are WD Red Plus / IronWolf (class nas): pooled into one key, Kč/TB
    assert "hdd35 nas" in prices
    assert prices["hdd35 nas"].category == "hdd" and prices["hdd35 nas"].per_tb
    status = gate_status(prices, repo.data_since(conn), rules(), datetime.now(UTC))
    assert status["days"]["hdd"] >= 9.9 and "hdd35 nas" in status["ready"]
