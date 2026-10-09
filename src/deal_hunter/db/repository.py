"""Raw-SQL access to the deal-hunter database (psycopg 3, no ORM)."""

import os
from dataclasses import dataclass, field
from importlib import resources

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from deal_hunter.bazos import Listing


def connect(url: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    """Connect using `url` or the DATABASE_URL environment variable (.env is loaded).

    Long-running services use autocommit=True, so every `conn.transaction()` block commits on its
    own and a plain SELECT never leaves a transaction open between scrape requests.
    """
    load_dotenv()
    url = url or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("Missing DATABASE_URL. See .env.example (and `docker compose up -d db`).")
    return psycopg.connect(url, autocommit=autocommit)


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any migrations that haven't run yet, in order. Returns the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
    files = sorted(
        (f for f in resources.files("deal_hunter.db.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    applied = []
    for f in files:
        if f.name in done:
            continue
        with conn.transaction():
            conn.execute(f.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
        applied.append(f.name)
    conn.commit()
    return applied


def detailed_ids(conn: psycopg.Connection, ids: list[int]) -> set[int]:
    """The ids among `ids` whose detail page we already have."""
    rows = conn.execute(
        "SELECT id FROM listings WHERE id = ANY(%s) AND detail_fetched_at IS NOT NULL", (ids,)
    ).fetchall()
    return {r[0] for r in rows}


def save_listing(conn: psycopg.Connection, item: Listing, detail: bool) -> tuple[bool, bool]:
    """Insert or update one ad. Returns (is_new, price_changed).

    `detail=True` means `item` came from the ad's own page, so description, images and category are
    authoritative. A list-page item only refreshes what a list page shows (price, views, location...).
    Seeing an ad again always clears gone_at: it is evidently still there.
    """
    with conn.transaction():
        old = conn.execute(
            "SELECT price_czk, price_kind FROM listings WHERE id = %s FOR UPDATE", (item.id,)
        ).fetchone()
        if old is None:
            conn.execute(
                "INSERT INTO listings (id, url, title, price_czk, price_kind, location, psc, posted_at, "
                "description, images, views, seller, raw_category, section, detail_fetched_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)",
                (
                    item.id,
                    item.url,
                    item.title,
                    item.price_czk,
                    item.price_kind,
                    item.location,
                    item.psc,
                    item.posted_at,
                    item.description,
                    Jsonb(item.images),
                    item.views,
                    item.seller,
                    item.raw_category,
                    item.section,
                    detail,
                ),
            )
            return True, False

        changed = (old[0], old[1]) != (item.price_czk, item.price_kind) and item.price_kind != "unknown"
        if changed:
            conn.execute(
                "INSERT INTO price_changes (listing_id, old_price, new_price, old_kind, new_kind) "
                "VALUES (%s, %s, %s, %s, %s)",
                (item.id, old[0], item.price_czk, old[1], item.price_kind),
            )
        conn.execute(
            "UPDATE listings SET url = %(url)s, title = %(title)s, "
            "price_czk = CASE WHEN %(changed)s THEN %(price)s ELSE price_czk END, "
            "price_kind = CASE WHEN %(changed)s THEN %(kind)s ELSE price_kind END, "
            "location = COALESCE(%(location)s, location), psc = COALESCE(%(psc)s, psc), "
            "posted_at = COALESCE(%(posted_at)s, posted_at), views = COALESCE(%(views)s, views), "
            "seller = COALESCE(%(seller)s, seller), "
            "description = CASE WHEN %(detail)s THEN %(description)s "
            "  ELSE COALESCE(description, %(description)s) END, "
            "images = CASE WHEN %(detail)s THEN %(images)s ELSE images END, "
            "raw_category = COALESCE(%(category)s, raw_category), "
            "detail_fetched_at = CASE WHEN %(detail)s THEN now() ELSE detail_fetched_at END, "
            "last_seen = now(), gone_at = NULL, gone_reason = NULL "
            "WHERE id = %(id)s",
            {
                "id": item.id,
                "url": item.url,
                "title": item.title,
                "changed": changed,
                "price": item.price_czk,
                "kind": item.price_kind,
                "location": item.location,
                "psc": item.psc,
                "posted_at": item.posted_at,
                "views": item.views,
                "seller": item.seller,
                "detail": detail,
                "description": item.description,
                "images": Jsonb(item.images),
                "category": item.raw_category,
            },
        )
        return False, changed


def mark_gone(conn: psycopg.Connection, listing_id: int, reason: str) -> bool:
    """Record that an ad has disappeared. Returns False if it was already marked."""
    with conn.transaction():
        cur = conn.execute(
            "UPDATE listings SET gone_at = now(), gone_reason = %s WHERE id = %s AND gone_at IS NULL",
            (reason, listing_id),
        )
    return cur.rowcount == 1


def due_for_recheck(conn: psycopg.Connection, older_than_hours: float, limit: int) -> list[tuple[int, str]]:
    """(id, url) of active ads not seen for `older_than_hours`, least recently seen first.

    Ads that still show up on the category pages get their last_seen refreshed by every scrape, so
    in practice this re-checks the older ads that have scrolled off the pages we read. Ads the
    normalizer excluded (wanted ads, gaming PCs, MacBooks, ...) are never priced, so whether they
    sold doesn't matter and they aren't re-checked.
    """
    return conn.execute(
        "SELECT l.id, l.url FROM listings l LEFT JOIN normalized n ON n.listing_id = l.id "
        "WHERE l.gone_at IS NULL AND n.excluded_reason IS NULL "
        "AND l.last_seen < now() - make_interval(secs => %s) ORDER BY l.last_seen LIMIT %s",
        (older_than_hours * 3600, limit),
    ).fetchall()


@dataclass
class RunStats:
    kind: str
    requests: int = 0
    seen: int = 0
    new: int = 0
    updated: int = 0
    price_changes: int = 0
    gone: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.kind}: {self.requests} requests, {self.seen} seen, {self.new} new, "
            f"{self.updated} updated, {self.price_changes} price changes, {self.gone} gone, "
            f"{len(self.errors)} errors"
        )


def start_run(conn: psycopg.Connection, kind: str) -> int:
    run_id = conn.execute("INSERT INTO scrape_runs (kind) VALUES (%s) RETURNING id", (kind,)).fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, stats: RunStats) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at = now(), requests = %s, seen = %s, new = %s, updated = %s, "
        "price_changes = %s, gone = %s, errors = %s, ok = %s WHERE id = %s",
        (
            stats.requests,
            stats.seen,
            stats.new,
            stats.updated,
            stats.price_changes,
            stats.gone,
            Jsonb(stats.errors[:50]),
            not stats.errors,
            run_id,
        ),
    )
    conn.commit()


def stats(conn: psycopg.Connection) -> dict:
    """Headline numbers for `hunt stats`."""
    row = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE gone_at IS NULL), count(*) FILTER (WHERE gone_at IS NOT NULL), "
        "count(*) FILTER (WHERE detail_fetched_at IS NOT NULL), "
        "count(*) FILTER (WHERE first_seen > now() - interval '24 hours'), "
        "count(*) FILTER (WHERE gone_at > now() - interval '24 hours'), "
        "count(*) FILTER (WHERE price_kind = 'fixed'), min(first_seen), max(last_seen) FROM listings"
    ).fetchone()
    keys = [
        "listings",
        "active",
        "gone",
        "with_detail",
        "new_24h",
        "gone_24h",
        "fixed_price",
        "first",
        "last",
    ]
    out = dict(zip(keys, row))
    out["price_changes"] = conn.execute("SELECT count(*) FROM price_changes").fetchone()[0]
    out["runs"] = conn.execute(
        "SELECT kind, count(*), count(*) FILTER (WHERE NOT ok), max(started_at) FROM scrape_runs GROUP BY kind "
        "ORDER BY kind"
    ).fetchall()
    out["normalized"] = conn.execute(
        "SELECT CASE WHEN model IS NOT NULL THEN 'matched' WHEN excluded_reason IS NOT NULL "
        "THEN 'excluded' ELSE 'unmatched' END AS status, count(*) FROM normalized GROUP BY 1 ORDER BY 1"
    ).fetchall()
    out["sections"] = conn.execute(
        "SELECT l.section, count(*), count(*) FILTER (WHERE l.gone_at IS NULL), count(n.model), "
        "count(*) FILTER (WHERE n.model IS NULL AND n.excluded_reason IS NULL), "
        "count(*) FILTER (WHERE l.first_seen > now() - interval '24 hours'), min(l.first_seen) "
        "FROM listings l LEFT JOIN normalized n ON n.listing_id = l.id GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return out


def to_normalize(
    conn: psycopg.Connection, version: int, everything: bool = False
) -> list[tuple[int, str, str]]:
    """(id, title, section) of listings never normalized, or normalized by older rules."""
    if everything:
        return conn.execute("SELECT id, title, section FROM listings ORDER BY id").fetchall()
    return conn.execute(
        "SELECT l.id, l.title, l.section FROM listings l LEFT JOIN normalized n ON n.listing_id = l.id "
        "WHERE n.listing_id IS NULL OR n.normalizer_version < %s ORDER BY l.id",
        (version,),
    ).fetchall()


def save_normalized(
    conn: psycopg.Connection, rows: list[tuple[int, str, str | None, dict, str | None]], version: int
):
    """Upsert (listing_id, category, model, features, excluded_reason) rows."""
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO normalized (listing_id, category, model, features, excluded_reason, normalizer_version) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (listing_id) DO UPDATE SET category = EXCLUDED.category, "
            "model = EXCLUDED.model, features = EXCLUDED.features, excluded_reason = EXCLUDED.excluded_reason, "
            "normalizer_version = EXCLUDED.normalizer_version, normalized_at = now()",
            [(lid, cat, model, Jsonb(feats), reason, version) for lid, cat, model, feats, reason in rows],
        )


def unmatched(conn: psycopg.Connection, limit: int, section: str | None = None) -> list[tuple]:
    """(id, title, price_czk, url, section) of listings the normalizer couldn't place, newest first."""
    return conn.execute(
        "SELECT l.id, l.title, l.price_czk, l.url, l.section FROM normalized n "
        "JOIN listings l ON l.id = n.listing_id WHERE n.model IS NULL AND n.excluded_reason IS NULL "
        "AND (%(section)s::text IS NULL OR l.section = %(section)s) ORDER BY l.first_seen DESC LIMIT %(limit)s",
        {"section": section, "limit": limit},
    ).fetchall()


def normalized_summary(conn: psycopg.Connection) -> list[tuple]:
    """(model or excluded reason or 'unmatched', count) for `hunt normalize -v`."""
    return conn.execute(
        "SELECT category || ': ' || COALESCE(model, 'excluded ' || excluded_reason, 'unmatched'), count(*) "
        "FROM normalized GROUP BY 1 ORDER BY 2 DESC, 1"
    ).fetchall()
