"""Watchdog: turns what /status shows into a push when a background job dies or keeps failing."""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta

from deal_hunter.db import repository as repo
from deal_hunter.text import ago

log = logging.getLogger(__name__)

SCRAPE_STALE = timedelta(minutes=30)
RECHECK_STALE = timedelta(hours=12)
TRIAGE_STALE = timedelta(hours=1)
WORKER_STALE = timedelta(minutes=30)
STEP_FAILING = timedelta(minutes=30)  # a few consecutive failed cycles, not one blip
REMIND_AFTER = timedelta(hours=24)
STALE_AFTER = {"scrape": SCRAPE_STALE, "scrape_aukro": SCRAPE_STALE, "recheck": RECHECK_STALE}

HEARTBEAT_KEY = "worker_heartbeat"
OPEN_KEY = "watchdog_open"


def record_heartbeat(conn, now: datetime, failed_steps: list[str]) -> None:
    """Called at the end of every worker cycle; keeps when each currently failing step first failed."""
    prev = repo.kv_get(conn, HEARTBEAT_KEY) or {}
    since = prev.get("failing", {})
    failing = {step: since.get(step, now.isoformat()) for step in failed_steps}
    repo.kv_set(conn, HEARTBEAT_KEY, {"at": now.isoformat(), "failing": failing})


def check_disk(path: str = "/", min_mb: int = 1024) -> str | None:
    """None when there's enough free space on `path`'s filesystem; a message otherwise.

    Checked before anything that needs the database, because a full root disk is exactly the kind
    of problem that also takes Postgres down with it (HUNTER_WATCHDOG_DISK_PATH/_MIN_MB).
    """
    try:
        free_mb = shutil.disk_usage(path).free / (1024 * 1024)
    except OSError as exc:
        return f"disk: could not check free space on {path} ({exc})"
    if free_mb < min_mb:
        return f"disk: only {free_mb:.0f} MB free on {path} (min {min_mb} MB)"
    return None


def problems(
    conn,
    now: datetime,
    sources: tuple[str, ...] = ("bazos",),
    disk_path: str = "/",
    disk_min_mb: int = 1024,
) -> dict[str, str]:
    """Problem key -> human-readable message. Empty when everything looks alive."""
    out: dict[str, str] = {}

    disk_msg = check_disk(disk_path, disk_min_mb)
    if disk_msg:
        out["disk"] = disk_msg

    beat = repo.kv_get(conn, HEARTBEAT_KEY)
    if beat is None:
        out["worker"] = "worker: no cycle recorded yet"
    else:
        at = datetime.fromisoformat(beat["at"])
        if now - at > WORKER_STALE:
            out["worker"] = f"worker: last cycle {ago(at, now)}"
        for step, since in beat.get("failing", {}).items():
            since_dt = datetime.fromisoformat(since)
            if now - since_dt >= STEP_FAILING:
                out[f"step:{step}"] = f"worker step '{step}' failing for {ago(since_dt, now, suffix='')}"

    wanted = {"scrape", "recheck"} | ({"scrape_aukro"} if "aukro" in sources else set())
    for run in repo.status_pipeline(conn)["runs"]:
        kind = run["kind"]
        if kind not in wanted:
            continue
        # A run that keeps failing restarts promptly, so "started recently" alone says nothing:
        # only its own ok/errors tell us the previous attempt didn't actually work.
        failed = run["ok"] is False or (run["errors"] or 0) > 0
        if failed:
            when = run["finished_at"] or run["started_at"]
            errors = f" ({run['errors']} error(s))" if run["errors"] else ""
            out[f"run:{kind}"] = f"{kind}: last run failed {ago(when, now)}{errors}"
        elif now - run["started_at"] > STALE_AFTER[kind]:
            out[f"run:{kind}"] = f"{kind}: last run {ago(run['started_at'], now)}"

    tri = repo.status_triage(conn, now, recent=0)
    if tri["pending_due"] > 0 and (tri["last_done"] is None or now - tri["last_done"] > TRIAGE_STALE):
        out["triage"] = (
            f"LLM triage stuck: {tri['pending_due']} due, last result {ago(tri['last_done'], now)} "
            "(is Ollama up?)"
        )
    return out


def run(
    conn,
    ntfy,
    now: datetime | None = None,
    sources: tuple[str, ...] = ("bazos",),
    disk_path: str = "/",
    disk_min_mb: int = 1024,
) -> dict[str, str]:
    """Push newly found problems (and reminders for old ones once a day), and a note when they clear."""
    now = now or datetime.now(UTC)
    found = problems(conn, now, sources, disk_path, disk_min_mb)
    open_ = repo.kv_get(conn, OPEN_KEY) or {}

    due = {
        k: m
        for k, m in found.items()
        if k not in open_ or now - datetime.fromisoformat(open_[k]["at"]) > REMIND_AFTER
    }
    cleared = sorted(k for k in open_ if k not in found)

    if due and _push(ntfy, "deal-hunter: something is wrong", "\n".join(due.values()), 4, "warning"):
        open_.update({k: {"at": now.isoformat(), "msg": m} for k, m in due.items()})
    if cleared:
        message = "\n".join(f"OK again: {open_[k]['msg']}" for k in cleared)
        if _push(ntfy, "deal-hunter: recovered", message, 2, "white_check_mark"):
            for k in cleared:
                open_.pop(k)
    repo.kv_set(conn, OPEN_KEY, open_)
    return found


def watchdog_cli(
    ntfy,
    sources: tuple[str, ...] = ("bazos",),
    disk_path: str = "/",
    disk_min_mb: int = 1024,
    now: datetime | None = None,
) -> int:
    """Entry point for `hunt watchdog`: owns the DB connection itself.

    The watchdog's whole job is to report when something else is broken, and the DB being
    unreachable (Postgres refusing connections, e.g. because the disk that holds its data filled
    up) is exactly the kind of thing that must never come back as a bare traceback instead of a
    report. The disk check runs first and does not need the database, so a full disk is reported
    even when it has already taken Postgres down with it.

    cli/worker.py's cmd_watchdog calls this instead of connecting and calling `run` itself.

    Returns the process exit code (0 all good/handled, 1 if the DB itself could not be reached).
    """
    disk_msg = check_disk(disk_path, disk_min_mb)
    try:
        conn = repo.connect(autocommit=True)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - any connect failure is "DB unreachable"
        lines = [f"DB unreachable: {exc}"]
        if disk_msg:
            lines.append(disk_msg)
        print("\n".join(lines), flush=True)
        return 1

    try:
        with conn:
            found = run(conn, ntfy, now, sources, disk_path, disk_min_mb)
    except Exception as exc:  # the connection was open but something still went wrong mid-query
        log.exception("watchdog could not finish")
        print(f"watchdog failed: {exc}", flush=True)
        return 1

    print("\n".join(found.values()) if found else "watchdog: all good", flush=True)
    return 0


def _push(ntfy, title: str, message: str, priority: int, tag: str) -> bool:
    if ntfy is None:
        log.warning("%s (no NTFY_TOKEN, not pushed): %s", title, message)
        return False
    try:
        ntfy.publish(title, message, priority=priority, tags=(tag,))
    except Exception:
        log.exception("watchdog push failed")
        return False
    return True
