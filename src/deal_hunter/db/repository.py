"""Raw-SQL access to the deal-hunter database (psycopg 3, no ORM)."""

import datetime as dt
import os
import uuid
from dataclasses import dataclass, field
from importlib import resources

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from deal_hunter.bazos import Listing
from deal_hunter.sources import SHOP_SOURCES
from deal_hunter.storage import unit_price


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
                "INSERT INTO listings (id, source, external_id, url, title, price_czk, price_kind, location, "
                "psc, posted_at, description, images, views, seller, raw_category, section, auction_end_at, "
                "detail_fetched_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "CASE WHEN %s THEN now() END)",
                (
                    item.id,
                    item.source,
                    item.external_id,
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
                    item.auction_end_at,
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
            # a new PSČ or town: the distance is recomputed by the next worker cycle (geo.update_distances)
            "distance_home = CASE WHEN COALESCE(%(psc)s, psc) IS DISTINCT FROM psc "
            "  OR COALESCE(%(location)s, location) IS DISTINCT FROM location THEN NULL ELSE distance_home END, "
            "description = CASE WHEN %(detail)s THEN %(description)s "
            "  ELSE COALESCE(description, %(description)s) END, "
            "images = CASE WHEN %(detail)s THEN %(images)s ELSE images END, "
            "raw_category = COALESCE(%(category)s, raw_category), "
            "auction_end_at = COALESCE(%(auction_end_at)s, auction_end_at), "
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
                "auction_end_at": item.auction_end_at,
            },
        )
        return False, changed


def mark_missing_gone(conn: psycopg.Connection, source: str, raw_category: str, since) -> int:
    """Retire a source's active listings of one category that a complete pass (started at `since`) no
    longer saw. Shop outlets have no per-item page worth fetching: an item that left the listing is gone."""
    with conn.transaction():
        cur = conn.execute(
            "UPDATE listings SET gone_at = now(), gone_reason = 'not_listed' WHERE source = %s "
            "AND raw_category = %s AND gone_at IS NULL AND last_seen < %s",
            (source, raw_category, since),
        )
    return cur.rowcount


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
    in practice this re-checks the older ads that have scrolled off the pages we read. Shop listings
    (sources.SHOP_SOURCES) are not Bazoš pages: their own cycle retires them (mark_missing_gone). Ads the
    normalizer excluded (wanted ads, gaming PCs, MacBooks, ...) are never priced, so whether they
    sold doesn't matter and they aren't re-checked.
    """
    return conn.execute(
        "SELECT l.id, l.url FROM listings l LEFT JOIN normalized n ON n.listing_id = l.id "
        "WHERE l.gone_at IS NULL AND n.excluded_reason IS NULL AND l.source <> ALL(%s) "
        "AND l.last_seen < now() - make_interval(secs => %s) ORDER BY l.last_seen LIMIT %s",
        (list(SHOP_SOURCES), older_than_hours * 3600, limit),
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
) -> list[tuple[int, str, str, str | None]]:
    """(id, title, section, description) of listings never normalized, or normalized by older rules."""
    if everything:
        return conn.execute("SELECT id, title, section, description FROM listings ORDER BY id").fetchall()
    return conn.execute(
        "SELECT l.id, l.title, l.section, l.description FROM listings l "
        "LEFT JOIN normalized n ON n.listing_id = l.id "
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
    """Matched ads with a numeric price, seen within the window: the input to pricing.estimate_all.

    Only price_kind='fixed': a shop's 'retail' prices (sources.RETAIL) are not what used goods sell for."""
    from deal_hunter.pricing import Sample

    rows = conn.execute(
        "SELECT l.id, n.model, n.category, l.price_czk, l.title, l.seller, l.first_seen, l.gone_at, "
        "l.gone_reason, n.features FROM listings l JOIN normalized n ON n.listing_id = l.id "
        "WHERE n.model IS NOT NULL AND l.price_kind = 'fixed' AND l.price_czk > 0 "
        "AND l.last_seen > now() - make_interval(days => %s)",
        (window_days,),
    ).fetchall()
    samples = []
    for row in rows:
        s = Sample(*row)
        s.price = unit_price(s.price, s.features)  # disks: one drive of a multi-drive ad, or None
        if s.price:
            samples.append(s)
    return samples


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
    """Active matched ads with a price, first seen recently: (id, model, category, price, features).

    The price is per drive for multi-disk ads (storage.unit_price); ads where that is unclear are left out.
    """
    rows = conn.execute(
        "SELECT l.id, n.model, n.category, l.price_czk, n.features FROM listings l "
        "JOIN normalized n ON n.listing_id = l.id WHERE l.gone_at IS NULL AND n.model IS NOT NULL "
        "AND l.price_kind IN ('fixed', 'retail') AND l.price_czk > 0 "
        "AND l.first_seen > now() - make_interval(secs => %s)",
        (max_age_days * 86400,),
    ).fetchall()
    out = []
    for lid, model, category, price, features in rows:
        price = unit_price(price, features)
        if price:
            out.append((lid, model, category, price, features))
    return out


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
        "l.images, l.distance_km, l.distance_src, l.source, n.features, "
        "(SELECT t.result FROM triage t WHERE t.listing_id = d.listing_id AND t.status = 'done' "
        " ORDER BY t.prompt_version DESC, t.updated_at DESC LIMIT 1) AS triage_result FROM deals d JOIN listings l ON l.id = d.listing_id "
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


def listings_needing_distance(conn: psycopg.Connection, home_key: str) -> list[tuple]:
    """(id, psc, location) of listings whose distance was never computed, or for another home."""
    return conn.execute(
        "SELECT id, psc, location FROM listings WHERE distance_home IS DISTINCT FROM %s", (home_key,)
    ).fetchall()


def save_distances(
    conn: psycopg.Connection, rows: list[tuple[int, float | None, str]], home_key: str
) -> None:
    """(id, km or None, how) rows, computed for `home_key`."""
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "UPDATE listings SET distance_km = %s, distance_src = %s, distance_home = %s WHERE id = %s",
            [(km, how, home_key, lid) for lid, km, how in rows],
        )


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


def section_due(conn: psycopg.Connection, section: str, cadence_min: float) -> bool:
    """Has it been at least `cadence_min` since this section (bazos.Section.cadence_min) was last
    scraped? True the first time (no kv row yet)."""
    import datetime as dt

    last = kv_get(conn, f"section_last_scraped:{section}")
    if not last:
        return True
    elapsed = (dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(last)).total_seconds() / 60
    return elapsed >= cadence_min


def mark_section_scraped(conn: psycopg.Connection, section: str) -> None:
    import datetime as dt

    kv_set(conn, f"section_last_scraped:{section}", dt.datetime.now(dt.UTC).isoformat())


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
    out["out_of_range"] = [
        r[0]
        for r in conn.execute(
            "SELECT reason FROM deals WHERE state = 'would_alert' AND reason LIKE 'out of range: %%' "
            "AND first_flagged > now() - interval '7 days' ORDER BY score DESC NULLS LAST"
        ).fetchall()
    ]
    return out


# --- auction lots (Phase 7) -----------------------------------------------------------------------


def _dicts(cur) -> list[dict]:
    names = [c.name for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def upsert_lot_summary(conn: psycopg.Connection, source: str, lot) -> tuple[bool, bool]:
    """Store what a list response says about a lot (nms.Lot). Returns (new, status/price/end changed)."""
    old = conn.execute(
        "SELECT status, current_price, final_price, end_at, bids FROM auction_lots WHERE source = %s AND lot_id = %s",
        (source, lot.lot_id),
    ).fetchone()
    current = lot.price if lot.status in ("active", "prepared") else None
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO auction_lots (source, lot_id, url, title, description, category, institution, district,
                zip_code, status, current_price, final_price, min_increment, bids, start_at, end_at)
            VALUES (%(source)s, %(lot_id)s, %(url)s, %(title)s, %(description)s, %(category)s, %(institution)s,
                %(district)s, %(zip_code)s, %(status)s, %(current)s, %(final)s, %(min_increment)s, %(bids)s,
                %(start_at)s, %(end_at)s)
            ON CONFLICT (source, lot_id) DO UPDATE SET
                title = EXCLUDED.title,
                description = COALESCE(auction_lots.description, EXCLUDED.description),
                category = EXCLUDED.category,
                institution = COALESCE(EXCLUDED.institution, auction_lots.institution),
                district = COALESCE(EXCLUDED.district, auction_lots.district),
                zip_code = COALESCE(EXCLUDED.zip_code, auction_lots.zip_code),
                status = EXCLUDED.status,
                current_price = COALESCE(EXCLUDED.current_price, auction_lots.current_price),
                final_price = COALESCE(EXCLUDED.final_price, auction_lots.final_price),
                min_increment = COALESCE(EXCLUDED.min_increment, auction_lots.min_increment),
                bids = COALESCE(EXCLUDED.bids, auction_lots.bids),
                start_at = COALESCE(EXCLUDED.start_at, auction_lots.start_at),
                end_at = COALESCE(EXCLUDED.end_at, auction_lots.end_at),
                last_seen = now()
            """,
            {
                "source": source,
                "lot_id": lot.lot_id,
                "url": lot.url,
                "title": lot.title,
                "description": lot.description,
                "category": lot.category,
                "institution": lot.institution,
                "district": lot.district,
                "zip_code": lot.zip_code,
                "status": lot.status,
                "current": current,
                "final": lot.final_price,
                "min_increment": lot.min_increment,
                "bids": lot.bids,
                "start_at": lot.start_at,
                "end_at": lot.end_at,
            },
        )
    if old is None:
        return True, False
    changed = (old[0], old[1], old[2], old[3], old[4]) != (
        lot.status,
        current if current is not None else old[1],
        lot.final_price if lot.final_price is not None else old[2],
        lot.end_at or old[3],
        lot.bids if lot.bids is not None else old[4],
    )
    return False, changed


def save_lot_detail(
    conn: psycopg.Connection, source: str, lot, attachment_text: str | None, content_hash: str, final: bool
) -> None:
    """Store a detail page (nms.Lot). `final`: fetched after the end, so the final price is in."""
    current = lot.price if lot.status in ("active", "prepared") else None
    with conn.transaction():
        conn.execute(
            """
            UPDATE auction_lots SET
                title = COALESCE(NULLIF(%(title)s, ''), title),
                description = COALESCE(%(description)s, description),
                category = COALESCE(%(category)s, category),
                institution = COALESCE(%(institution)s, institution),
                address = COALESCE(%(address)s, address),
                inspection = COALESCE(%(inspection)s, inspection),
                conditions = COALESCE(%(conditions)s, conditions),
                status = %(status)s,
                start_price = COALESCE(%(start_price)s, start_price),
                current_price = COALESCE(%(current)s, current_price),
                final_price = COALESCE(%(final)s, final_price),
                min_increment = COALESCE(%(min_increment)s, min_increment),
                bids = COALESCE(%(bids)s, bids),
                start_at = COALESCE(%(start_at)s, start_at),
                end_at = COALESCE(%(end_at)s, end_at),
                attachments = %(attachments)s,
                images = %(images)s,
                attachment_text = COALESCE(%(attachment_text)s, attachment_text),
                content_hash = %(hash)s,
                detail_at = now(),
                final_checked = final_checked OR %(final_checked)s,
                last_seen = now()
            WHERE source = %(source)s AND lot_id = %(lot_id)s
            """,
            {
                "source": source,
                "lot_id": lot.lot_id,
                "title": lot.title,
                "description": lot.description,
                "category": lot.category,
                "institution": lot.institution,
                "address": lot.address,
                "inspection": lot.inspection,
                "conditions": lot.conditions,
                "status": lot.status,
                "start_price": lot.start_price,
                "current": current,
                "final": lot.final_price,
                "min_increment": lot.min_increment,
                "bids": lot.bids,
                "start_at": lot.start_at,
                "end_at": lot.end_at,
                "attachments": Jsonb(lot.attachments),
                "images": Jsonb(lot.images),
                "attachment_text": attachment_text,
                "hash": content_hash,
                "final_checked": final,
            },
        )


def lots_needing_detail(conn: psycopg.Connection, limit: int, now=None) -> list[dict]:
    """New lots (running ones first, then sold ones for the price dataset), and lots that ended since
    we last looked (their final price). Lots that ended unsold before we ever saw them are skipped."""
    cur = conn.execute(
        """
        SELECT source, lot_id, url, status, end_at, detail_at, attachment_text FROM auction_lots
        WHERE (detail_at IS NULL AND status IN ('active', 'prepared', 'ended'))
           OR (detail_at IS NOT NULL AND NOT final_checked
               AND end_at < COALESCE(%s, now()) - interval '10 minutes')
        ORDER BY (status IN ('active', 'prepared')) DESC, (detail_at IS NULL) ASC,
            CASE WHEN status IN ('active', 'prepared') THEN end_at END ASC NULLS LAST, end_at DESC NULLS LAST
        LIMIT %s
        """,
        (now, limit),
    )
    return _dicts(cur)


def get_lot(conn: psycopg.Connection, source: str, lot_id: str) -> dict | None:
    cur = conn.execute("SELECT * FROM auction_lots WHERE source = %s AND lot_id = %s", (source, lot_id))
    rows = _dicts(cur)
    return rows[0] if rows else None


def lots_in_status(conn: psycopg.Connection, *statuses: str, limit: int = 500) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM auction_lots WHERE status = ANY(%s) ORDER BY end_at DESC NULLS LAST LIMIT %s",
        (list(statuses), limit),
    )
    return _dicts(cur)


def lots_to_extract(conn: psycopg.Connection, llm_model: str, prompt_version: int, limit: int) -> list[dict]:
    """Detailed lots without a finished extraction of their current text (running ones first)."""
    cur = conn.execute(
        """
        SELECT l.* FROM auction_lots l
        LEFT JOIN auction_extractions e ON e.source = l.source AND e.lot_id = l.lot_id
            AND e.llm_model = %(m)s AND e.prompt_version = %(v)s AND e.content_hash = l.content_hash
        WHERE l.content_hash IS NOT NULL AND l.status IN ('active', 'prepared', 'ended')
          AND (e.status IS NULL OR (e.status = 'pending' AND e.next_try_at <= now()))
        ORDER BY (l.status IN ('active', 'prepared')) DESC, l.end_at ASC NULLS LAST
        LIMIT %(limit)s
        """,
        {"m": llm_model, "v": prompt_version, "limit": limit},
    )
    return _dicts(cur)


def get_extraction(
    conn: psycopg.Connection, source: str, lot_id: str, llm_model: str, prompt_version: int, content_hash: str
) -> dict | None:
    cur = conn.execute(
        "SELECT * FROM auction_extractions WHERE source = %s AND lot_id = %s AND llm_model = %s "
        "AND prompt_version = %s AND content_hash = %s",
        (source, lot_id, llm_model, prompt_version, content_hash),
    )
    rows = _dicts(cur)
    return rows[0] if rows else None


def latest_extraction(conn: psycopg.Connection, source: str, lot_id: str) -> dict | None:
    """The newest finished extraction of a lot, whatever model or prompt made it."""
    cur = conn.execute(
        "SELECT * FROM auction_extractions WHERE source = %s AND lot_id = %s AND status = 'done' "
        "ORDER BY prompt_version DESC, updated_at DESC LIMIT 1",
        (source, lot_id),
    )
    rows = _dicts(cur)
    return rows[0] if rows else None


def save_extraction(
    conn: psycopg.Connection,
    source: str,
    lot_id: str,
    llm_model: str,
    prompt_version: int,
    content_hash: str,
    **fields,
) -> None:
    for key in ("result", "notes", "stats"):
        if key in fields and fields[key] is not None:
            fields[key] = Jsonb(fields[key])
    cols = ", ".join(fields)
    vals = ", ".join(f"%({k})s" for k in fields)
    sets = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields)
    with conn.transaction():
        conn.execute(
            f"INSERT INTO auction_extractions (source, lot_id, llm_model, prompt_version, content_hash, {cols}) "
            f"VALUES (%(s)s, %(l)s, %(m)s, %(v)s, %(h)s, {vals}) "
            "ON CONFLICT (source, lot_id, llm_model, prompt_version, content_hash) "
            f"DO UPDATE SET {sets}, updated_at = now()",
            {**fields, "s": source, "l": lot_id, "m": llm_model, "v": prompt_version, "h": content_hash},
        )


def save_valuation(
    conn: psycopg.Connection, source: str, lot_id: str, valuation: dict | None, qualifies: bool
) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE auction_lots SET valuation = %s, qualifies = %s, valued_at = now() "
            "WHERE source = %s AND lot_id = %s",
            (Jsonb(valuation) if valuation is not None else None, qualifies, source, lot_id),
        )


def auction_alert_kinds(conn: psycopg.Connection, source: str, lot_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT kind FROM auction_alerts WHERE source = %s AND lot_id = %s", (source, lot_id)
    ).fetchall()
    return {r[0] for r in rows}


def record_auction_alert(
    conn: psycopg.Connection,
    source: str,
    lot_id: str,
    kind: str,
    ntfy_id: str | None,
    price: int | None,
    max_bid: int | None,
    profit: float | None,
) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO auction_alerts (source, lot_id, kind, ntfy_id, price, max_bid, profit) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (source, lot_id, kind, ntfy_id, price, max_bid, round(profit) if profit is not None else None),
        )


def auction_alerts_since(conn: psycopg.Connection, since) -> int:
    return conn.execute(
        "SELECT count(*) FROM auction_alerts WHERE ntfy_id IS NOT NULL AND sent_at >= %s", (since,)
    ).fetchone()[0]


def mark_lot_gone(conn: psycopg.Connection, source: str, lot_id: str) -> None:
    """The detail page is gone (404 / redirect): stop asking for it."""
    with conn.transaction():
        conn.execute(
            "UPDATE auction_lots SET status = 'gone', final_checked = true, detail_at = now(), qualifies = false "
            "WHERE source = %s AND lot_id = %s",
            (source, lot_id),
        )


def sold_lot_extractions(conn: psycopg.Connection) -> list[tuple[int, dict]]:
    """(final price, newest finished extraction) of every ended lot that sold."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (l.lot_id) l.final_price, e.result FROM auction_lots l
        JOIN auction_extractions e ON e.source = l.source AND e.lot_id = l.lot_id AND e.status = 'done'
        WHERE l.final_price IS NOT NULL AND l.final_price > 0 AND e.result IS NOT NULL
        ORDER BY l.lot_id, e.prompt_version DESC, e.updated_at DESC
        """
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


# --- Phase 8: votes, follow-ups, flips, new prices ---------------------------------------------------


def save_votes(conn: psycopg.Connection, votes: list) -> list:
    """Store alerts.Vote rows; returns the ones that were new (an already-seen ntfy id is skipped).

    'up'/'down' go to `feedback` (on a listing or a lot); 'bought' starts a row in `flips`.
    """
    fresh = []
    with conn.transaction():
        for v in votes:
            if v.vote == "bought":
                if v.kind != "listing":
                    continue
                cur = conn.execute(
                    "INSERT INTO flips (listing_id, bought_at, buy_price, source_id, model, title, category) "
                    "SELECT %(id)s, %(at)s, l.price_czk, %(src)s, n.model, l.title, n.category FROM (SELECT 1) one "
                    "LEFT JOIN listings l ON l.id = %(id)s LEFT JOIN normalized n ON n.listing_id = %(id)s "
                    "ON CONFLICT DO NOTHING",
                    {"id": int(v.item), "at": v.voted_at, "src": v.source_id},
                )
            else:
                listing_id = int(v.item) if v.kind == "listing" else None
                lot_id = v.item if v.kind == "lot" else None
                cur = conn.execute(
                    "INSERT INTO feedback (listing_id, lot_id, vote, source_id, voted_at) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (source_id) DO NOTHING",
                    (listing_id, lot_id, v.vote, v.source_id, v.voted_at),
                )
            if cur.rowcount:
                fresh.append(v)
    return fresh


def save_feedback(conn: psycopg.Connection, rows: list[tuple[int, str, str, object]]) -> int:
    """Insert (listing_id, vote, ntfy message id, voted_at) rows; already-seen ids are skipped."""
    from deal_hunter.alerts import Vote

    return len(save_votes(conn, [Vote("listing", str(lid), vote, src, at) for lid, vote, src, at in rows]))


def followup_sent(conn: psycopg.Connection, kind: str, item_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM followups WHERE kind = %s AND item_id = %s", (kind, item_id)).fetchone()
    return row is not None


def record_followup(
    conn: psycopg.Connection, kind: str, item_id: str, source_id: str, status: str, ntfy_id: str | None
) -> bool:
    """Claim the follow-up for an item. False if one was already recorded."""
    with conn.transaction():
        cur = conn.execute(
            "INSERT INTO followups (kind, item_id, source_id, status, ntfy_id) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (kind, item_id, source_id, status, ntfy_id),
        )
    return cur.rowcount == 1


def listing_for_followup(conn: psycopg.Connection, listing_id: int) -> dict | None:
    """Everything a follow-up shows about an ad: the listing, its model and its deal row (if any)."""
    cur = conn.execute(
        "SELECT l.*, n.model, n.category, n.features, d.fair, d.discount, d.verdict, d.state AS deal_state, "
        "d.ships "
        "FROM listings l LEFT JOIN normalized n ON n.listing_id = l.id LEFT JOIN deals d ON d.listing_id = l.id "
        "WHERE l.id = %s",
        (listing_id,),
    )
    rows = _dicts(cur)
    return rows[0] if rows else None


def latest_triage(conn: psycopg.Connection, listing_id: int) -> dict | None:
    """The newest finished triage of an ad, whatever model or prompt made it."""
    cur = conn.execute(
        "SELECT * FROM triage WHERE listing_id = %s AND status = 'done' ORDER BY prompt_version DESC, "
        "updated_at DESC LIMIT 1",
        (listing_id,),
    )
    rows = _dicts(cur)
    return rows[0] if rows else None


def save_new_price(
    conn: psycopg.Connection,
    model: str,
    url: str,
    product: str | None,
    min_price: int | None,
    shops: int | None,
    error: str | None = None,
    shop: str = "zbozi",
) -> None:
    """One read of a product page. `shop` is who quoted it: 'zbozi' (the lowest of its `shops` offers)
    or a single shop with its own feed ('datart', see datart.py)."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO new_prices (model, url, product, min_price, shops, error, shop) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (model, url, product, min_price, shops, error, shop),
        )


def new_price_fetched_at(conn: psycopg.Connection, shop: str = "zbozi") -> dict[str, tuple[object, object]]:
    """{url: (last successful read, last attempt)} of one shop's pages; the first is None if every read failed."""
    rows = conn.execute(
        "SELECT url, max(fetched_at) FILTER (WHERE error IS NULL), max(fetched_at) FROM new_prices "
        "WHERE shop = %s GROUP BY url",
        (shop,),
    ).fetchall()
    return {url: (ok, tried) for url, ok, tried in rows}


def latest_new_offers(conn: psycopg.Connection, max_age_days: float = 30) -> dict[str, tuple[int, int, str]]:
    """{model: (lowest price in Kč, shops on that product, who quoted it)} over every shop's pages
    (Zboží product pages, Datart category rows), each page's latest read within the window.

    The cheapest wins; on a tie Zboží, which compares several shops, is the one shown.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (model) model, min_price, shops, shop FROM (
            SELECT DISTINCT ON (url) model, url, min_price, shops, shop FROM new_prices
            WHERE fetched_at > now() - make_interval(secs => %s) AND error IS NULL
            ORDER BY url, fetched_at DESC
        ) latest WHERE min_price IS NOT NULL AND min_price > 0
        ORDER BY model, min_price, (shop = 'zbozi') DESC
        """,
        (max_age_days * 86400,),
    ).fetchall()
    return {m: (p, s or 0, shop) for m, p, s, shop in rows}


def latest_new_prices(conn: psycopg.Connection, max_age_days: float = 30) -> dict[str, tuple[int, int]]:
    """{model: (lowest offer in Kč, shops on that product)}: latest_new_offers without the shop."""
    return {m: (p, s) for m, (p, s, _) in latest_new_offers(conn, max_age_days).items()}


def new_price_rows(conn: psycopg.Connection) -> list[dict]:
    """The latest fetch of every product page, for `hunt newprices`."""
    cur = conn.execute(
        "SELECT DISTINCT ON (url) model, url, product, min_price, shops, shop, fetched_at, error "
        "FROM new_prices ORDER BY url, fetched_at DESC"
    )
    return sorted(_dicts(cur), key=lambda r: (r["model"], r["min_price"] or 10**9))


# --- web (read-only pages) -------------------------------------------------------------------------


def price_history(conn: psycopg.Connection, listing_id: int) -> list[dict]:
    """Price changes for one ad, oldest first: for the `/listing/<id>` page."""
    cur = conn.execute(
        "SELECT changed_at, old_price, new_price, old_kind, new_kind FROM price_changes "
        "WHERE listing_id = %s ORDER BY changed_at",
        (listing_id,),
    )
    return _dicts(cur)


# --- watchlists (Phase 10 part 1) ------------------------------------------------------------------


def insert_watch(
    conn: psycopg.Connection,
    query: str,
    keywords: dict,
    categories: list[str] | None = None,
    max_price: int | None = None,
    radius_km: float | None = None,
    tag: str | None = None,
    created_by: str | None = None,
) -> int:
    row = conn.execute(
        "INSERT INTO watches (query, keywords, categories, max_price, radius_km, tag, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (query, Jsonb(keywords), Jsonb(categories or []), max_price, radius_km, tag, created_by),
    )
    watch_id = row.fetchone()[0]
    conn.commit()
    return watch_id


def list_watches(conn: psycopg.Connection, active_only: bool = False) -> list[dict]:
    cur = conn.execute(
        "SELECT id, query, keywords, categories, max_price, radius_km, tag, active, created_at, created_by "
        "FROM watches" + (" WHERE active" if active_only else "") + " ORDER BY id"
    )
    return _dicts(cur)


def get_watch(conn: psycopg.Connection, watch_id: int) -> dict | None:
    cur = conn.execute(
        "SELECT id, query, keywords, categories, max_price, radius_km, tag, active, created_at, created_by "
        "FROM watches WHERE id = %s",
        (watch_id,),
    )
    rows = _dicts(cur)
    return rows[0] if rows else None


def set_watch_active(conn: psycopg.Connection, watch_id: int, active: bool) -> bool:
    """Returns False if there is no watch with that id."""
    with conn.transaction():
        cur = conn.execute("UPDATE watches SET active = %s WHERE id = %s", (active, watch_id))
    return cur.rowcount == 1


def listings_for_watch(conn: psycopg.Connection, watch_id: int, hours: float, limit: int) -> list[dict]:
    """Recent, active listings (any source/section) this watch hasn't been checked against yet.

    A prefilter miss is never stored in `watch_hits` (see the migration), so it is simply seen
    again on the next cycle until it ages out of the `hours` window -- bounded and cheap (no LLM
    call unless the prefilter matches).
    """
    cur = conn.execute(
        "SELECT l.id, l.title, l.description, l.price_czk, l.location, l.psc, l.source, l.url, "
        "l.images, l.section, l.distance_km FROM listings l "
        "WHERE l.first_seen > now() - make_interval(secs => %s) AND l.gone_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM watch_hits h WHERE h.watch_id = %s AND h.listing_id = l.id) "
        "ORDER BY l.first_seen LIMIT %s",
        (hours * 3600, watch_id, limit),
    )
    return _dicts(cur)


def record_watch_hit(
    conn: psycopg.Connection, watch_id: int, listing_id: int, matched: bool, result: dict
) -> bool:
    """Returns True if this is the first time (watch_id, listing_id) was recorded."""
    with conn.transaction():
        cur = conn.execute(
            "INSERT INTO watch_hits (watch_id, listing_id, matched, result) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (watch_id, listing_id) DO NOTHING",
            (watch_id, listing_id, matched, Jsonb(result)),
        )
    return cur.rowcount == 1


def pending_watch_matches(conn: psycopg.Connection, limit: int = 100) -> list[dict]:
    """Matched hits never pushed yet, oldest first, with the listing and the watch they're for."""
    cur = conn.execute(
        "SELECT h.watch_id, h.listing_id, h.result, h.created_at, w.query, w.tag, "
        "l.title, l.price_czk, l.location, l.psc, l.url, l.images, l.source, l.distance_km "
        "FROM watch_hits h JOIN watches w ON w.id = h.watch_id JOIN listings l ON l.id = h.listing_id "
        "WHERE h.matched AND h.pushed_at IS NULL ORDER BY h.created_at LIMIT %s",
        (limit,),
    )
    return _dicts(cur)


def mark_watch_pushed(conn: psycopg.Connection, watch_id: int, listing_id: int) -> None:
    conn.execute(
        "UPDATE watch_hits SET pushed_at = now() WHERE watch_id = %s AND listing_id = %s",
        (watch_id, listing_id),
    )
    conn.commit()


def watch_pushes_since(conn: psycopg.Connection, since) -> int:
    return conn.execute("SELECT count(*) FROM watch_hits WHERE pushed_at >= %s", (since,)).fetchone()[0]


# --- Phase 6: the flips log and the alert evaluation --------------------------------------------------

FLIP_FIELDS = ("buy_price", "sold_at", "sold_price", "fees", "notes")


def list_flips(conn: psycopg.Connection, status: str | None = None) -> list[dict]:
    """Flips, newest purchase first. status: 'open' (not sold yet), 'sold' or None for all.

    `category` is the flip's own, or the ad's from `normalized` when the flip has none.
    """
    where = {"open": "WHERE f.sold_at IS NULL", "sold": "WHERE f.sold_at IS NOT NULL"}.get(status or "", "")
    cur = conn.execute(
        "SELECT f.listing_id, f.bought_at, f.buy_price, f.sold_at, f.sold_price, f.fees, f.notes, f.model, "
        f"f.title, coalesce(f.category, n.category) AS category FROM flips f "
        f"LEFT JOIN normalized n ON n.listing_id = f.listing_id {where} "
        "ORDER BY f.bought_at DESC, f.listing_id DESC"
    )
    return _dicts(cur)


def add_flip(conn: psycopg.Connection, listing_id: int, buy_price: int, bought_at, notes: str | None) -> bool:
    """Log a purchase made outside the 👍 flow. Title, model and category come from the ad when we have it.

    False if this ad is already in the log.
    """
    cur = conn.execute(
        "INSERT INTO flips (listing_id, bought_at, buy_price, source_id, model, title, category, notes) "
        "SELECT %(id)s, %(at)s, %(price)s, %(src)s, n.model, l.title, n.category, %(notes)s FROM (SELECT 1) one "
        "LEFT JOIN listings l ON l.id = %(id)s LEFT JOIN normalized n ON n.listing_id = %(id)s "
        "ON CONFLICT DO NOTHING",
        {
            "id": listing_id,
            "at": bought_at,
            "price": buy_price,
            "src": f"manual:{uuid.uuid4()}",
            "notes": notes,
        },
    )
    return cur.rowcount == 1


def update_flip(conn: psycopg.Connection, listing_id: int, **fields) -> bool:
    """Set some of FLIP_FIELDS on a flip. False if there is no flip for the ad."""
    unknown = set(fields) - set(FLIP_FIELDS)
    if unknown or not fields:
        raise ValueError(f"update_flip needs some of {FLIP_FIELDS}, got {sorted(fields)}")
    assignments = ", ".join(f"{name} = %({name})s" for name in fields)  # names are whitelisted above
    cur = conn.execute(
        f"UPDATE flips SET {assignments} WHERE listing_id = %(listing_id)s",
        {**fields, "listing_id": listing_id},
    )
    return cur.rowcount == 1


def eval_deals(conn: psycopg.Connection, since) -> list[dict]:
    """One row per deal flagged since `since`, with how the ad ended up and what we said about it.

    Votes on auction lots (lot_id) and test pushes (listing_id 0) never match a deal's listing, so
    they are left out. has_up / has_down: the ad got at least one 👍 / 👎. bought: it is in `flips`.
    """
    cur = conn.execute(
        "SELECT d.listing_id, d.category, d.state, d.score, l.first_seen, l.gone_at, l.gone_reason, "
        "EXISTS (SELECT 1 FROM feedback f WHERE f.listing_id = d.listing_id AND f.listing_id > 0 "
        "AND f.vote = 'up') AS has_up, "
        "EXISTS (SELECT 1 FROM feedback f WHERE f.listing_id = d.listing_id AND f.listing_id > 0 "
        "AND f.vote = 'down') AS has_down, "
        "EXISTS (SELECT 1 FROM flips x WHERE x.listing_id = d.listing_id) AS bought "
        "FROM deals d JOIN listings l ON l.id = d.listing_id WHERE d.first_flagged >= %s "
        "ORDER BY d.listing_id",
        (since,),
    )
    return _dicts(cur)


# --- /status page: what the background jobs did, read from the tables they already write ----------------


def status_triage(conn: psycopg.Connection, now, recent: int = 15) -> dict:
    """LLM triage activity: queue counts, the latest results, 24 h stats and the latest error."""
    counts = {
        status: n for status, n in conn.execute("SELECT status, count(*) FROM triage GROUP BY 1").fetchall()
    }
    pending_wait, pending_due, last_done = conn.execute(
        "SELECT count(*) FILTER (WHERE status = 'pending' AND next_try_at > %(now)s), "
        "count(*) FILTER (WHERE status = 'pending' AND next_try_at <= %(now)s), "
        "max(updated_at) FILTER (WHERE status IN ('done', 'failed')) FROM triage",
        {"now": now},
    ).fetchone()
    day = conn.execute(
        "SELECT count(*), "
        "avg((stats->>'seconds')::float) FILTER (WHERE coalesce((stats->>'cached')::boolean, false) IS NOT TRUE), "
        "count(*) FILTER (WHERE (stats->>'cached')::boolean), "
        "sum((stats->>'prompt_tokens')::bigint), sum((stats->>'output_tokens')::bigint) "
        "FROM triage WHERE status IN ('done', 'failed') AND updated_at > %s",
        (now - dt.timedelta(hours=24),),
    ).fetchone()
    cur = conn.execute(
        "SELECT t.listing_id, t.updated_at, t.status, l.title, t.result->>'condition', "
        "t.result->>'one_line_verdict', (t.stats->>'seconds')::float, (t.stats->>'cached')::boolean, "
        "d.state, d.score FROM triage t JOIN listings l ON l.id = t.listing_id "
        "LEFT JOIN deals d ON d.listing_id = t.listing_id WHERE t.status IN ('done', 'failed') "
        "ORDER BY t.updated_at DESC, t.listing_id DESC LIMIT %s",
        (recent,),
    )
    keys = [
        "listing_id",
        "at",
        "status",
        "title",
        "condition",
        "verdict",
        "seconds",
        "cached",
        "state",
        "score",
    ]
    last_error = conn.execute(
        "SELECT updated_at, error FROM triage WHERE error IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    return {
        "counts": counts,
        "pending_wait": pending_wait,
        "pending_due": pending_due,
        "last_done": last_done,
        "day": {
            "n": day[0],
            "avg_seconds": day[1],
            "cached": day[2],
            "prompt_tokens": day[3],
            "output_tokens": day[4],
        },
        "recent": [dict(zip(keys, r)) for r in cur.fetchall()],
        "last_error": last_error,
    }


def status_watch(conn: psycopg.Connection, now, recent: int = 10) -> dict:
    """Watchlist LLM checks (watch_hits rows: ads that passed the keyword prefilter)."""
    since = now - dt.timedelta(hours=24)
    last, checked, matched, pushed = conn.execute(
        "SELECT max(created_at), count(*) FILTER (WHERE created_at > %(s)s), "
        "count(*) FILTER (WHERE created_at > %(s)s AND matched), count(*) FILTER (WHERE pushed_at > %(s)s) "
        "FROM watch_hits",
        {"s": since},
    ).fetchone()
    cur = conn.execute(
        "SELECT h.created_at, w.query, h.listing_id, l.title, h.matched, h.pushed_at "
        "FROM watch_hits h JOIN watches w ON w.id = h.watch_id JOIN listings l ON l.id = h.listing_id "
        "ORDER BY h.created_at DESC, h.listing_id DESC, h.watch_id DESC LIMIT %s",
        (recent,),
    )
    keys = ["at", "query", "listing_id", "title", "matched", "pushed_at"]
    return {
        "last": last,
        "checked": checked,
        "matched": matched,
        "pushed": pushed,
        "recent": [dict(zip(keys, r)) for r in cur.fetchall()],
    }


def status_auctions(conn: psycopg.Connection, now, recent: int = 5) -> dict:
    """LLM extractions of auction lots."""
    last, week, pending = conn.execute(
        "SELECT max(updated_at) FILTER (WHERE status IN ('done', 'failed')), "
        "count(*) FILTER (WHERE status IN ('done', 'failed') AND updated_at > %s), "
        "count(*) FILTER (WHERE status = 'pending') FROM auction_extractions",
        (now - dt.timedelta(days=7),),
    ).fetchone()
    cur = conn.execute(
        "SELECT e.updated_at, e.status, l.title, (e.stats->>'seconds')::float "
        "FROM auction_extractions e JOIN auction_lots l ON l.source = e.source AND l.lot_id = e.lot_id "
        "WHERE e.status IN ('done', 'failed') ORDER BY e.updated_at DESC, e.lot_id DESC LIMIT %s",
        (recent,),
    )
    keys = ["at", "status", "title", "seconds"]
    return {
        "last": last,
        "week": week,
        "pending": pending,
        "recent": [dict(zip(keys, r)) for r in cur.fetchall()],
    }


def status_pipeline(conn: psycopg.Connection) -> dict:
    """Heartbeat: the latest run of each kind, each section's last scrape (kv) and the newest deal."""
    cur = conn.execute(
        "SELECT DISTINCT ON (kind) kind, started_at, finished_at, ok, new, jsonb_array_length(errors) "
        "FROM scrape_runs ORDER BY kind, started_at DESC, id DESC"
    )
    keys = ["kind", "started_at", "finished_at", "ok", "new", "errors"]
    runs = [dict(zip(keys, r)) for r in cur.fetchall()]
    sections = conn.execute(
        "SELECT substr(key, length('section_last_scraped:') + 1), value #>> '{}' FROM kv "
        "WHERE key LIKE 'section\\_last\\_scraped:%' ORDER BY 1"
    ).fetchall()
    deal = conn.execute(
        "SELECT d.listing_id, l.title, d.state, d.first_flagged FROM deals d JOIN listings l ON l.id = d.listing_id "
        "ORDER BY d.first_flagged DESC, d.listing_id DESC LIMIT 1"
    ).fetchone()
    return {"runs": runs, "sections": sections, "deal": deal}


def status_sources(conn: psycopg.Connection, now, window: dt.timedelta = dt.timedelta(days=1)) -> dict:
    """Per-site activity for /status: what each listing source, new-price feed and the auction site wrote.

    `window` is the "recent" span: new ads per listing source, and how far back from a shop's newest
    read its pages count as one pass.
    """
    listings = {
        source: {"last_seen": last_seen, "last_new": last_new, "new_recent": new_recent, "active": active}
        for source, last_seen, last_new, new_recent, active in conn.execute(
            "SELECT source, max(last_seen), max(first_seen), count(*) FILTER (WHERE first_seen > %s), "
            "count(*) FILTER (WHERE gone_at IS NULL) FROM listings GROUP BY source",
            (now - window,),
        ).fetchall()
    }
    prices = {}
    for shop, last_ok, last_try, ok, failed in conn.execute(
        "SELECT shop, max(fetched_at) FILTER (WHERE error IS NULL), max(fetched_at), "
        "count(*) FILTER (WHERE error IS NULL AND fetched_at > m.newest - %(w)s), "
        "count(*) FILTER (WHERE error IS NOT NULL AND fetched_at > m.newest - %(w)s) "
        "FROM new_prices n JOIN (SELECT shop AS s, max(fetched_at) AS newest FROM new_prices GROUP BY shop) m "
        "ON m.s = n.shop GROUP BY shop",
        {"w": window},
    ).fetchall():
        prices[shop] = {"last_ok": last_ok, "last_try": last_try, "ok": ok, "failed": failed, "error": None}
    for shop, error in conn.execute(
        "SELECT DISTINCT ON (shop) shop, error FROM new_prices ORDER BY shop, fetched_at DESC, id DESC"
    ).fetchall():
        prices[shop]["error"] = error
    last_seen, open_lots, total = conn.execute(
        "SELECT max(last_seen), count(*) FILTER (WHERE status IN ('active', 'prepared')), count(*) "
        "FROM auction_lots WHERE source = 'nms'"
    ).fetchone()
    return {
        "listings": listings,
        "prices": prices,
        "nms": {"last_seen": last_seen, "open": open_lots, "total": total},
    }
