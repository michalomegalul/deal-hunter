"""listener.forever/listen_once: reconnect-with-backoff on a dropped database connection.

No database needed: these exercise the control flow with fakes, not real psycopg connections.
"""

import httpx
import psycopg
import pytest

from deal_hunter import listener


class _StopLoop(Exception):
    """Breaks out of listener.forever's `while True` after a fixed number of sleep() calls."""


def _sleep_after(n):
    calls = []

    def sleep(delay):
        calls.append(delay)
        if len(calls) >= n:
            raise _StopLoop

    return sleep, calls


def test_forever_reraises_operational_error_without_a_reconnect_hook():
    """No `reconnect` given: don't spin forever against a dead connection, let the process die."""

    def listen():
        raise psycopg.OperationalError("connection lost")

    with pytest.raises(psycopg.OperationalError):
        listener.forever(listen, "test", sleep=lambda d: None)


def test_forever_reconnects_and_keeps_retrying_on_operational_error():
    attempts = []

    def listen():
        attempts.append(1)
        raise psycopg.OperationalError("connection lost")

    reconnects = []
    sleep, _delays = _sleep_after(3)

    with pytest.raises(_StopLoop):
        listener.forever(listen, "test", sleep=sleep, reconnect=lambda: reconnects.append(1))

    assert len(attempts) == 3
    assert len(reconnects) == 3  # reconnected every time, not just once


def test_forever_does_not_treat_other_errors_as_a_db_reconnect():
    reconnects = []
    sleep, _delays = _sleep_after(1)

    def listen():
        raise httpx.HTTPError("boom")

    with pytest.raises(_StopLoop):
        listener.forever(listen, "test", sleep=sleep, reconnect=lambda: reconnects.append(1))

    assert reconnects == []


def test_forever_survives_a_failing_reconnect_and_keeps_retrying():
    """A reconnect attempt that itself fails (DB still down) must not kill the loop."""
    sleep, delays = _sleep_after(2)

    def listen():
        raise psycopg.OperationalError("connection lost")

    def bad_reconnect():
        raise psycopg.OperationalError("still down")

    with pytest.raises(_StopLoop):
        listener.forever(listen, "test", sleep=sleep, reconnect=bad_reconnect)

    assert len(delays) == 2


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Just enough of psycopg.Connection for kv_get/kv_set, recording every statement run."""

    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("SELECT value FROM kv"):
            return _FakeResult(("all",))
        return _FakeResult(None)

    def transaction(self):
        import contextlib

        return contextlib.nullcontext()


def test_listen_once_propagates_operational_error_without_recording_progress():
    """If handling a message loses the database connection, the stream must not advance past it:
    no kv_set for that message id, so a reconnect resumes at the same message, not after it."""

    conn = _FakeConn()

    def stream(since):
        yield {"event": "message", "id": "m1", "message": "{}"}

    def handle(event):
        raise psycopg.OperationalError("connection lost")

    with pytest.raises(psycopg.OperationalError):
        listener.listen_once(conn, stream, handle, "last_id", "test")

    assert not any(s.startswith("INSERT INTO kv") for s in conn.sql)
