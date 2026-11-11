"""The watchdog: heartbeat, stale runs, failing steps, one push per problem, recovery notes."""

import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

from deal_hunter import health
from deal_hunter.db import repository as repo
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
    # health.py now shares text.ago with web.py (the granular "X h Y min ago" form, not the old
    # "X h ago" with the minutes dropped).
    assert len(site.posts) == 1 and "scrape: last run 2 h 0 min ago" in site.posts[0]["message"]

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


# --- disk space (HUNTER_WATCHDOG_DISK_PATH/_MIN_MB): a full root disk is a watchdog problem too ------


def test_check_disk_reports_low_free_space(tmp_path):
    assert health.check_disk(str(tmp_path), min_mb=1) is None  # plenty of room
    msg = health.check_disk(str(tmp_path), min_mb=10**9)  # no filesystem has an exabyte free
    assert msg is not None and "disk" in msg


def test_check_disk_handles_a_bad_path():
    assert health.check_disk("/no/such/path/at/all", min_mb=1) is not None


def test_problems_includes_a_low_disk_warning(conn, tmp_path):
    healthy(conn)
    assert "disk" not in health.problems(conn, NOW, disk_path=str(tmp_path), disk_min_mb=1)
    found = health.problems(conn, NOW, disk_path=str(tmp_path), disk_min_mb=10**9)
    assert "disk" in found


# --- a scrape/recheck run that keeps failing is a problem even though it just started ----------------


def test_a_failing_run_is_flagged_even_though_it_just_started(conn):
    healthy(conn)
    conn.execute("DELETE FROM scrape_runs WHERE kind = 'scrape'")
    conn.execute(
        "INSERT INTO scrape_runs (kind, started_at, finished_at, ok, errors) VALUES "
        "('scrape', %s, %s, false, %s)",
        (NOW - timedelta(minutes=1), NOW - timedelta(seconds=30), Jsonb(["boom"])),
    )
    found = health.problems(conn, NOW)
    assert "run:scrape" in found
    assert "failed" in found["run:scrape"]


def test_a_run_with_errors_but_ok_true_is_still_flagged(conn):
    healthy(conn)
    conn.execute("DELETE FROM scrape_runs WHERE kind = 'recheck'")
    conn.execute(
        "INSERT INTO scrape_runs (kind, started_at, finished_at, ok, errors) VALUES "
        "('recheck', %s, %s, true, %s)",
        (NOW - timedelta(minutes=1), NOW - timedelta(seconds=30), Jsonb(["timeout"])),
    )
    assert "run:recheck" in health.problems(conn, NOW)


# --- `hunt watchdog`'s own entry point: a DB it cannot reach must not be a traceback ------------------


def test_watchdog_cli_reports_db_unreachable_instead_of_raising(monkeypatch, tmp_path):
    def boom(*a, **kw):
        raise psycopg.OperationalError("simulated outage")

    monkeypatch.setattr(repo, "connect", boom)
    code = health.watchdog_cli(make_ntfy(FakeNtfy()), disk_path=str(tmp_path), disk_min_mb=1)
    assert code == 1


class _KeepOpen:
    """Wraps the test `conn` fixture's connection so `watchdog_cli`'s own `with conn:` doesn't close
    it (the fixture owns that, and needs it open for the rest of the test)."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_watchdog_cli_runs_normally_when_the_db_is_fine(conn, monkeypatch, tmp_path):
    healthy(conn)
    monkeypatch.setattr(repo, "connect", lambda **kw: _KeepOpen(conn))
    code = health.watchdog_cli(make_ntfy(FakeNtfy()), now=NOW, disk_path=str(tmp_path), disk_min_mb=1)
    assert code == 0
