"""Watchdog: turns what /status shows into a push when a background job dies or keeps failing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from deal_hunter.db import repository as repo

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


def problems(conn, now: datetime, sources: tuple[str, ...] = ("bazos",)) -> dict[str, str]:
    """Problem key -> human-readable message. Empty when everything looks alive."""
    out: dict[str, str] = {}

    beat = repo.kv_get(conn, HEARTBEAT_KEY)
    if beat is None:
        out["worker"] = "worker: no cycle recorded yet"
    else:
        at = datetime.fromisoformat(beat["at"])
        if now - at > WORKER_STALE:
            out["worker"] = f"worker: last cycle {_ago(at, now)}"
        for step, since in beat.get("failing", {}).items():
            since_dt = datetime.fromisoformat(since)
            if now - since_dt >= STEP_FAILING:
                out[f"step:{step}"] = f"worker step '{step}' failing for {_ago(since_dt, now, suffix='')}"

    wanted = {"scrape", "recheck"} | ({"scrape_aukro"} if "aukro" in sources else set())
    for run in repo.status_pipeline(conn)["runs"]:
        kind = run["kind"]
        if kind in wanted and now - run["started_at"] > STALE_AFTER[kind]:
            out[f"run:{kind}"] = f"{kind}: last run {_ago(run['started_at'], now)}"

    tri = repo.status_triage(conn, now, recent=0)
    if tri["pending_due"] > 0 and (tri["last_done"] is None or now - tri["last_done"] > TRIAGE_STALE):
        out["triage"] = (
            f"LLM triage stuck: {tri['pending_due']} due, last result {_ago(tri['last_done'], now)} "
            "(is Ollama up?)"
        )
    return out


def run(conn, ntfy, now: datetime | None = None, sources: tuple[str, ...] = ("bazos",)) -> dict[str, str]:
    """Push newly found problems (and reminders for old ones once a day), and a note when they clear."""
    now = now or datetime.now(UTC)
    found = problems(conn, now, sources)
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


def _ago(dt: datetime | None, now: datetime, suffix: str = " ago") -> str:
    if dt is None:
        return "never"
    minutes = max(0, int((now - dt).total_seconds() // 60))
    if minutes < 120:
        return f"{minutes} min{suffix}"
    if minutes < 48 * 60:
        return f"{minutes // 60} h{suffix}"
    return f"{minutes // 1440} days{suffix}"
