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


# --- fair prices ---------------------------------------------------------------------------------


def price_samples(conn: psycopg.Connection, window_days: int) -> list:
    """Matched ads with a numeric price, seen within the window: the input to pricing.estimate_all."""
    from deal_hunter.pricing import Sample

    rows = conn.execute(
        "SELECT l.id, n.model, n.category, l.price_czk, l.title, l.seller, l.first_seen, l.gone_at, "
        "l.gone_reason, n.features FROM listings l JOIN normalized n ON n.listing_id = l.id "
        "WHERE n.model IS NOT NULL AND l.price_kind = 'fixed' AND l.price_czk > 0 "
        "AND l.last_seen > now() - make_interval(days => %s)",
        (window_days,),
    ).fetchall()
    return [Sample(*row) for row in rows]


def save_fair_prices(conn: psycopg.Connection, prices: list) -> None:
    """Upsert today's snapshot (UTC date) for each FairPrice."""
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO fair_prices (model, day, category, n, n_listings, median, q25, q75, confidence, level, "
            "params) VALUES (%s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (model, day) DO "
            "UPDATE SET category = EXCLUDED.category, n = EXCLUDED.n, n_listings = EXCLUDED.n_listings, "
            "median = EXCLUDED.median, q25 = EXCLUDED.q25, q75 = EXCLUDED.q75, confidence = EXCLUDED.confidence, "
            "level = EXCLUDED.level, params = EXCLUDED.params, computed_at = now()",
            [
                (
                    p.model,
                    p.category,
                    p.n,
                    p.n_listings,
                    p.median,
                    p.q25,
                    p.q75,
                    p.confidence,
                    p.level,
                    Jsonb(p.params()),
                )
                for p in prices
            ],
        )


def fair_price_on(conn: psycopg.Connection, day_offset: int) -> dict[str, tuple[int, str]]:
    """{model: (median, level)} from the latest snapshot at least `day_offset` days old."""
    rows = conn.execute(
        "SELECT DISTINCT ON (model) model, median, level FROM fair_prices "
        "WHERE day <= CURRENT_DATE - %s ORDER BY model, day DESC",
        (day_offset,),
    ).fetchall()
    return {m: (median, lvl) for m, median, lvl in rows}


def model_listings(conn: psycopg.Connection, model: str, limit: int) -> list[tuple]:
    """Recent ads of one model: (id, price, price_kind, title, first_seen, gone_at, features, url)."""
    return conn.execute(
        "SELECT l.id, l.price_czk, l.price_kind, l.title, l.first_seen, l.gone_at, n.features, l.url "
        "FROM listings l JOIN normalized n ON n.listing_id = l.id WHERE n.model = %s "
        "ORDER BY l.first_seen DESC LIMIT %s",
        (model, limit),
    ).fetchall()


def data_since(conn: psycopg.Connection) -> dict[str, object]:
    """{section: first_seen of its oldest listing}: how long each section has been collected."""
    return dict(conn.execute("SELECT section, min(first_seen) FROM listings GROUP BY 1").fetchall())


# --- deals, triage, feedback ----------------------------------------------------------------------


def active_priced(conn: psycopg.Connection, max_age_days: float) -> list[tuple]:
    """Active matched ads with a price, first seen recently: (id, model, category, price, features)."""
    return conn.execute(
        "SELECT l.id, n.model, n.category, l.price_czk, n.features FROM listings l "
        "JOIN normalized n ON n.listing_id = l.id WHERE l.gone_at IS NULL AND n.model IS NOT NULL "
        "AND l.price_kind = 'fixed' AND l.price_czk > 0 AND l.first_seen > now() - make_interval(secs => %s)",
        (max_age_days * 86400,),
    ).fetchall()


def upsert_candidate(
    conn: psycopg.Connection,
    listing_id: int,
    model: str,
    category: str,
    price: int,
    fair: int,
    discount: float,
    conf: float,
) -> None:
    """Record (or refresh the numbers of) a candidate deal. New ones start in state 'triage'."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO deals (listing_id, model, category, price, fair, discount, confidence, state) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'triage') ON CONFLICT (listing_id) DO UPDATE SET "
            "model = EXCLUDED.model, price = EXCLUDED.price, fair = EXCLUDED.fair, discount = EXCLUDED.discount, "
            "confidence = EXCLUDED.confidence, updated_at = now(), "
            # a price drop on an ad we didn't push re-opens it (a re-score, no new LLM call)
            "state = CASE WHEN deals.state IN ('low_score', 'would_alert') AND EXCLUDED.price < deals.price "
            "THEN 'triage' ELSE deals.state END",
            (listing_id, model, category, price, fair, discount, conf),
        )


def deals_in_state(conn: psycopg.Connection, *states: str) -> list[dict]:
    cur = conn.execute(
        "SELECT d.*, l.title, l.url, l.location, l.psc, l.seller, l.description, l.gone_at, l.first_seen, "
        "n.features FROM deals d JOIN listings l ON l.id = d.listing_id "
        "JOIN normalized n ON n.listing_id = d.listing_id WHERE d.state = ANY(%s) ORDER BY d.discount DESC",
        (list(states),),
    )
    names = [c.name for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def update_deal(conn: psycopg.Connection, listing_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    with conn.transaction():
        conn.execute(
            f"UPDATE deals SET {sets}, updated_at = now() WHERE listing_id = %(listing_id)s",
            {**fields, "listing_id": listing_id},
        )


def recent_alerts(conn: psycopg.Connection, days: float) -> list[dict]:
    cur = conn.execute(
        "SELECT d.listing_id, d.model, d.price, d.alerted_at, l.title, l.seller FROM deals d "
        "JOIN listings l ON l.id = d.listing_id WHERE d.alerted_at > now() - make_interval(secs => %s)",
        (days * 86400,),
    )
    names = [c.name for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def alerts_since(conn: psycopg.Connection, since) -> int:
    return conn.execute("SELECT count(*) FROM deals WHERE alerted_at >= %s", (since,)).fetchone()[0]


def get_triage(conn: psycopg.Connection, listing_id: int, llm_model: str, prompt_version: int) -> dict | None:
    cur = conn.execute(
        "SELECT * FROM triage WHERE listing_id = %s AND llm_model = %s AND prompt_version = %s",
        (listing_id, llm_model, prompt_version),
    )
    row = cur.fetchone()
    return dict(zip([c.name for c in cur.description], row)) if row else None


def queue_triage(conn: psycopg.Connection, listing_id: int, llm_model: str, prompt_version: int) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO triage (listing_id, llm_model, prompt_version) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (listing_id, llm_model, prompt_version),
        )


def due_triage(conn: psycopg.Connection, llm_model: str, prompt_version: int, limit: int) -> list[int]:
    rows = conn.execute(
        "SELECT t.listing_id FROM triage t JOIN deals d ON d.listing_id = t.listing_id "
        "WHERE t.status = 'pending' AND t.llm_model = %s AND t.prompt_version = %s AND t.next_try_at <= now() "
        "ORDER BY d.discount * d.confidence DESC LIMIT %s",
        (llm_model, prompt_version, limit),
    ).fetchall()
    return [r[0] for r in rows]


def save_triage(
    conn: psycopg.Connection, listing_id: int, llm_model: str, prompt_version: int, **fields
) -> None:
    for key in ("result", "notes", "stats"):
        if key in fields and fields[key] is not None:
            fields[key] = Jsonb(fields[key])
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    with conn.transaction():
        conn.execute(
            f"UPDATE triage SET {sets}, updated_at = now() WHERE listing_id = %(id)s AND llm_model = %(m)s "
            "AND prompt_version = %(v)s",
            {**fields, "id": listing_id, "m": llm_model, "v": prompt_version},
        )


def save_feedback(conn: psycopg.Connection, rows: list[tuple[int, str, str, object]]) -> int:
    """Insert (listing_id, vote, ntfy message id, voted_at) rows; already-seen ids are skipped."""
    added = 0
    with conn.transaction():
        for row in rows:
            cur = conn.execute(
                "INSERT INTO feedback (listing_id, vote, source_id, voted_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (source_id) DO NOTHING",
                row,
            )
            added += cur.rowcount
    return added


def kv_get(conn: psycopg.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = %s", (key,)).fetchone()
    return row[0] if row else default


def kv_set(conn: psycopg.Connection, key: str, value) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = now()",
            (key, Jsonb(value)),
        )


def week_summary(conn: psycopg.Connection) -> dict:
    """Numbers for the weekly digest."""
    out = {}
    out["deals"] = dict(
        conn.execute(
            "SELECT state, count(*) FROM deals WHERE first_flagged > now() - interval '7 days' GROUP BY 1"
        ).fetchall()
    )
    out["new_by_model"] = conn.execute(
        "SELECT n.model, count(*) FROM listings l JOIN normalized n ON n.listing_id = l.id "
        "WHERE n.model IS NOT NULL AND l.first_seen > now() - interval '7 days' GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
    ).fetchall()
    out["new_listings"] = conn.execute(
        "SELECT count(*) FROM listings WHERE first_seen > now() - interval '7 days'"
    ).fetchone()[0]
    out["votes"] = dict(
        conn.execute(
            "SELECT vote, count(*) FROM feedback WHERE received_at > now() - interval '7 days' GROUP BY 1"
        ).fetchall()
    )
    return out
