"""The watchdog: heartbeat, stale runs, failing steps, one push per problem, recovery notes."""

import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

from deal_hunter import health
from tests.test_worker import FakeNtfy, make_ntfy, seed, work

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def run_at(conn, kind, at):
    conn.execute(
        "INSERT INTO scrape_runs (kind, started_at, finished_at, ok) VALUES (%s, %s, %s, true)",
        (kind, at, at),
    )


def healthy(conn, now=NOW):
    run_at(conn, "scrape", now - timedelta(minutes=5))
    run_at(conn, "recheck", now - timedelta(hours=2))
    health.record_heartbeat(conn, now - timedelta(minutes=3), [])


def test_all_good_is_silent(conn):
    healthy(conn)
    site = FakeNtfy()
    assert health.run(conn, make_ntfy(site), NOW) == {}
    assert site.posts == []


def test_stale_scrape_pushes_once_then_recovers(conn):
    healthy(conn)
    conn.execute("DELETE FROM scrape_runs WHERE kind = 'scrape'")
    run_at(conn, "scrape", NOW - timedelta(hours=2))
    site = FakeNtfy()

    assert set(health.run(conn, make_ntfy(site), NOW)) == {"run:scrape"}
    assert len(site.posts) == 1 and "scrape: last run 120 min ago" in site.posts[0]["message"]

    health.run(conn, make_ntfy(site), NOW + timedelta(minutes=10))
    assert len(site.posts) == 1  # still open: no repeat until the daily reminder

    health.run(conn, make_ntfy(site), NOW + timedelta(hours=25))
    assert len(site.posts) == 2  # the reminder

    run_at(conn, "scrape", NOW + timedelta(hours=25))
    health.record_heartbeat(conn, NOW + timedelta(hours=25), [])
    run_at(conn, "recheck", NOW + timedelta(hours=25))
    assert health.run(conn, make_ntfy(site), NOW + timedelta(hours=25, minutes=1)) == {}
    assert site.posts[-1]["title"] == "deal-hunter: recovered"
    assert "OK again: scrape: last run" in site.posts[-1]["message"]


def test_dead_worker(conn):
    healthy(conn)
    health.record_heartbeat(conn, NOW - timedelta(hours=1), [])
    assert "worker" in health.problems(conn, NOW)


def test_step_failing_needs_to_persist(conn):
    healthy(conn)
    health.record_heartbeat(conn, NOW - timedelta(minutes=40), ["watches"])
    health.record_heartbeat(conn, NOW - timedelta(minutes=5), ["watches"])  # keeps the first failure time
    found = health.problems(conn, NOW)
    assert found == {"step:watches": "worker step 'watches' failing for 40 min"}

    health.record_heartbeat(conn, NOW - timedelta(minutes=1), [])
    assert health.problems(conn, NOW) == {}


def test_one_failed_cycle_is_not_a_problem(conn):
    healthy(conn)
    health.record_heartbeat(conn, NOW - timedelta(minutes=5), ["auctions"])
    assert health.problems(conn, NOW) == {}


def test_aukro_runs_only_checked_when_aukro_is_on(conn):
    healthy(conn)
    run_at(conn, "scrape_aukro", NOW - timedelta(days=3))
    assert health.problems(conn, NOW) == {}
    assert "run:scrape_aukro" in health.problems(conn, NOW, sources=("bazos", "aukro"))


def test_work_once_writes_the_heartbeat(conn):
    seed(conn)
    work(conn, FakeNtfy())
    beat = conn.execute("SELECT value FROM kv WHERE key = 'worker_heartbeat'").fetchone()[0]
    assert beat["failing"] == {}
    assert datetime.fromisoformat(beat["at"]) > datetime.now(UTC) - timedelta(minutes=1)
