"""Hold an ntfy topic's JSON stream open, resume after the last handled message, reconnect with backoff."""

import logging
import time
from collections.abc import Callable, Iterable

import httpx

log = logging.getLogger(__name__)

BACKOFF_START, BACKOFF_MAX = 2.0, 120.0


def listen_once(
    conn,
    stream: Callable[[str], Iterable[dict]],
    handle: Callable[[dict], str | None],
    last_id_key: str,
    name: str,
    stop=lambda: False,
) -> int:
    """Read until the stream drops (or `stop()`); returns how many messages were handled."""
    from deal_hunter.db import repository as repo

    since = repo.kv_get(conn, last_id_key) or "all"
    handled = 0
    for event in stream(since):
        if event.get("event") == "open":
            log.info("%s stream connected (since %s)", name, since)
        if event.get("event") == "message":
            try:
                result = handle(event)
            except Exception:  # one bad message must not wedge the stream
                log.exception("handling %s message %s failed", name, event.get("id"))
                result = "failed"
            log.info("%s %s: %s", name, event.get("id"), result)
            repo.kv_set(conn, last_id_key, event["id"])
            handled += 1
        if stop():
            break
    return handled


def forever(listen: Callable[[], object], name: str, sleep=time.sleep) -> None:
    delay = BACKOFF_START
    while True:
        started = time.monotonic()
        try:
            listen()
            log.info("%s stream closed by the server; reconnecting", name)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s stream: %s; reconnecting in %.0f s", name, exc, delay)
        except Exception:
            log.exception("%s stream failed; reconnecting in %.0f s", name, delay)
        if time.monotonic() - started > 60:
            delay = BACKOFF_START  # it was up for a while: a fresh failure, start the backoff over
        sleep(delay)
        delay = min(BACKOFF_MAX, delay * 2)
