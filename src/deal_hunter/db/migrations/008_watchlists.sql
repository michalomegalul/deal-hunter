-- Phase 10 part 1: watchlists. Standing "I want X" searches (DDR4 RAM, an HBA, a rack case, a
-- 19" rack, UniFi gear...), matched against every new listing from any source by a keyword
-- prefilter and then an LLM check, pushed on the deals topic. See watchlist.py.

CREATE TABLE watches (
    id          bigserial PRIMARY KEY,
    query       text NOT NULL,                -- human description, echoed in replies and pushes
    keywords    jsonb NOT NULL DEFAULT '{}',   -- {"include": [[...]], "exclude": [...]}, see watchlist.prefilter
    categories  jsonb NOT NULL DEFAULT '[]',   -- canonical sections this watch applies to; [] = any section
    max_price   integer,                       -- Kč; NULL = no limit
    radius_km   real,                          -- NULL = no pickup radius rule (see HUNTER_PICKUP_KM)
    tag         text,                          -- homelab | resale | friend | ... (free text)
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text                           -- 'seed', or the ntfy message id of the command that made it
);
CREATE INDEX watches_active_idx ON watches (active);

-- One row per (watch, listing) that passed the keyword prefilter: dedups the LLM call and, once
-- matched, the push. A prefilter miss is never stored (cheap to re-check; keeps this table small).
CREATE TABLE watch_hits (
    watch_id    bigint NOT NULL REFERENCES watches (id) ON DELETE CASCADE,
    listing_id  bigint NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    matched     boolean NOT NULL,
    result      jsonb,                         -- the LLM's extracted fields + evidence quote
    pushed_at   timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (watch_id, listing_id)
);
CREATE INDEX watch_hits_pending_idx ON watch_hits (created_at) WHERE matched AND pushed_at IS NULL;

-- kv already holds free-form state (see repository.kv_get/kv_set): the watch command listener's
-- last-seen ntfy message id ('watch_last_id') and each new scrape section's last-run time
-- ('section_last_scraped:<slug>') live there too, no new table needed.
