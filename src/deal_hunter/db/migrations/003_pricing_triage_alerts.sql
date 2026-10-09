-- Phases 3-5: fair price snapshots, LLM triage of candidate deals, scored deals and their alerts,
-- 👍/👎 feedback from the phone, and a little key-value state for the worker.

-- One row per model per day (the last estimate of the day wins). Trends and the weekly digest
-- compare these snapshots.
CREATE TABLE fair_prices (
    model       text NOT NULL,
    day         date NOT NULL,
    category    text NOT NULL,
    n           integer NOT NULL,       -- samples used (deduped, outliers removed)
    n_listings  integer NOT NULL,       -- matched ads with a price in the window
    median      integer NOT NULL,
    q25         integer NOT NULL,
    q75         integer NOT NULL,
    confidence  real NOT NULL,          -- 0..1, see pricing.py
    level       text NOT NULL,          -- low | medium | high
    params      jsonb NOT NULL DEFAULT '{}',  -- sold count, reference RAM/SSD, slopes (office)
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model, day)
);

-- The local LLM's reading of a candidate ad. One row per ad, model and prompt version, so a new
-- prompt or model re-triages without losing the old answers.
CREATE TABLE triage (
    listing_id     bigint NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    llm_model      text NOT NULL,
    prompt_version integer NOT NULL,
    status         text NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts       integer NOT NULL DEFAULT 0,
    next_try_at    timestamptz NOT NULL DEFAULT now(),
    result         jsonb,                            -- the cleaned-up answer (see triage.py)
    raw            text,
    notes          jsonb NOT NULL DEFAULT '[]',      -- what the tolerant parser had to fix
    error          text,
    stats          jsonb NOT NULL DEFAULT '{}',      -- tokens, seconds, cached
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (listing_id, llm_model, prompt_version)
);
CREATE INDEX triage_pending_idx ON triage (next_try_at) WHERE status = 'pending';

-- Ads priced well below fair, and what became of them.
CREATE TABLE deals (
    listing_id    bigint PRIMARY KEY REFERENCES listings (id) ON DELETE CASCADE,
    model         text NOT NULL,
    category      text NOT NULL,
    price         integer NOT NULL,
    fair          integer NOT NULL,       -- the model's fair price at this ad's spec
    discount      real NOT NULL,          -- 1 - price / fair
    confidence    real NOT NULL,
    score         real,                   -- discount x confidence x penalties (after triage)
    penalty       real,
    state         text NOT NULL,          -- see alerts.py: triage | dropped | low_score | ready | would_alert |
                                          -- queued | alerted | capped | duplicate | expired
    reason        text,
    verdict       text,                   -- the LLM's one-liner
    priority      integer,
    first_flagged timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    alerted_at    timestamptz,
    ntfy_id       text
);
CREATE INDEX deals_state_idx ON deals (state, updated_at);
CREATE INDEX deals_alerted_idx ON deals (alerted_at) WHERE alerted_at IS NOT NULL;

-- 👍/👎 taps, read back from the deals-feedback ntfy topic. Labels for the v2 price model.
CREATE TABLE feedback (
    id          bigserial PRIMARY KEY,
    listing_id  bigint NOT NULL,          -- no FK: a vote may outlive the ad, and 0 = test push
    vote        text NOT NULL,            -- up | down
    source_id   text NOT NULL UNIQUE,     -- ntfy message id: polling the topic again is harmless
    voted_at    timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX feedback_listing_idx ON feedback (listing_id);

CREATE TABLE kv (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
