-- Listings scraped from Bazoš, their price history, the GPU normalizer's verdicts and scrape run logs.

CREATE TABLE listings (
    id                bigint PRIMARY KEY,           -- Bazoš ad id (/inzerat/<id>/...)
    source            text NOT NULL DEFAULT 'bazos',
    url               text NOT NULL,
    title             text NOT NULL,
    price_czk         integer,                      -- NULL unless price_kind = 'fixed'
    price_kind        text NOT NULL DEFAULT 'unknown', -- fixed | negotiable | in_text | free | offer | unknown
    location          text,
    psc               text,
    posted_at         date,                         -- date Bazoš shows; renewing/topping an ad moves it
    description       text,
    images            jsonb NOT NULL DEFAULT '[]',
    views             integer,
    seller            text,
    raw_category      text,                         -- category slug from the detail page breadcrumb
    detail_fetched_at timestamptz,                  -- NULL: only seen on a list page/RSS so far
    first_seen        timestamptz NOT NULL DEFAULT now(),
    last_seen         timestamptz NOT NULL DEFAULT now(),
    gone_at           timestamptz,                  -- when we noticed the ad was removed (proxy for "sold")
    gone_reason       text                          -- http_404 | http_410 | removed_page | redirect
);
CREATE INDEX listings_active_idx ON listings (last_seen) WHERE gone_at IS NULL;
CREATE INDEX listings_first_seen_idx ON listings (first_seen);

CREATE TABLE price_changes (
    id          bigserial PRIMARY KEY,
    listing_id  bigint NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    changed_at  timestamptz NOT NULL DEFAULT now(),
    old_price   integer,
    new_price   integer,
    old_kind    text NOT NULL,
    new_kind    text NOT NULL
);
CREATE INDEX price_changes_listing_idx ON price_changes (listing_id, changed_at);

CREATE TABLE normalized (
    listing_id         bigint PRIMARY KEY REFERENCES listings (id) ON DELETE CASCADE,
    gpu_model          text,      -- canonical, e.g. 'rtx 3060', 'rx 6700 xt'; NULL = not matched/excluded
    vram_gb            integer,
    excluded_reason    text,      -- wanted | bundle | laptop | multiple_gpus | accessory | mining | out_of_scope
    normalizer_version integer NOT NULL,
    normalized_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX normalized_model_idx ON normalized (gpu_model, vram_gb);

CREATE TABLE scrape_runs (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,     -- scrape | backfill | recheck
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    requests    integer NOT NULL DEFAULT 0,
    seen        integer NOT NULL DEFAULT 0,
    new         integer NOT NULL DEFAULT 0,
    updated     integer NOT NULL DEFAULT 0,
    price_changes integer NOT NULL DEFAULT 0,
    gone        integer NOT NULL DEFAULT 0,
    errors      jsonb NOT NULL DEFAULT '[]',
    ok          boolean
);

-- One row: when anyone last sent a request to Bazoš. Keeps the scraper and rechecker processes
-- to one shared request budget (see scraper.DbThrottle).
CREATE TABLE http_throttle (
    id           integer PRIMARY KEY CHECK (id = 1),
    last_request timestamptz NOT NULL DEFAULT '2000-01-01'
);
INSERT INTO http_throttle (id) VALUES (1);
