"""Raw-SQL queries for the watch-command extensions (mute, vote): kept out of `repository.py` so
this file and that one can be touched independently. Same style as `repository.py` (raw psycopg 3,
no ORM); see migration `013_watch_mute_vote.sql` for the columns this reads and writes.
"""

import psycopg


def set_watch_muted(conn: psycopg.Connection, watch_id: int, days: float) -> bool:
    """Snooze pushes for this watch for `days` (fractional allowed) from now. Still matched and
    recorded in `watch_hits`, just not pushed (see `watchlist.push_matches`). Returns False if there
    is no watch with that id."""
    with conn.transaction():
        cur = conn.execute(
            "UPDATE watches SET muted_until = now() + make_interval(secs => %s) WHERE id = %s",
            (days * 86400, watch_id),
        )
    return cur.rowcount == 1


def muted_watch_ids(conn: psycopg.Connection) -> set[int]:
    """Ids of watches currently snoozed (`muted_until` set and still in the future)."""
    cur = conn.execute("SELECT id FROM watches WHERE muted_until IS NOT NULL AND muted_until > now()")
    return {row[0] for row in cur.fetchall()}


def record_vote(conn: psycopg.Connection, watch_id: int, listing_id: int, direction: str) -> bool:
    """👍/👎 on one watch hit, tapped from the push itself. Overwrites any earlier vote on the same
    hit (a plain last-vote flag, no history). Returns False if there is no such hit."""
    with conn.transaction():
        cur = conn.execute(
            "UPDATE watch_hits SET vote = %s WHERE watch_id = %s AND listing_id = %s",
            (direction, watch_id, listing_id),
        )
    return cur.rowcount == 1
