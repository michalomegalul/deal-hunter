-- Phase 8: votes on auction lots, 👍 follow-ups, the flips log, and new-retail prices (Zboží.cz).

-- A vote is about a Bazoš ad (listing_id) or an auction lot (lot_id), never both.
ALTER TABLE feedback ALTER COLUMN listing_id DROP NOT NULL;
ALTER TABLE feedback ADD COLUMN lot_id text;
ALTER TABLE feedback ADD CONSTRAINT feedback_target_check CHECK ((listing_id IS NULL) <> (lot_id IS NULL));

-- The detailed message sent after a 👍. One per item: tapping 👍 again doesn't send it twice.
CREATE TABLE followups (
    kind      text NOT NULL,                -- listing | lot
    item_id   text NOT NULL,
    source_id text NOT NULL,                -- the ntfy id of the vote that triggered it
    status    text NOT NULL,                -- sent | gone (the ad/lot was no longer there)
    ntfy_id   text,
    sent_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, item_id)
);

-- "Bought ✅" from a follow-up: the start of the flips log (Phase 6 adds selling and profit).
CREATE TABLE flips (
    listing_id bigint PRIMARY KEY,          -- no FK: the ad disappears once it is sold to us
    bought_at  timestamptz NOT NULL,
    buy_price  integer,                     -- the asking price when tapped; edit it after haggling
    sold_at    timestamptz,
    sold_price integer,
    source_id  text NOT NULL UNIQUE,        -- ntfy message id of the tap
    model      text,
    title      text
);

-- Lowest current offer for a product page on Zboží.cz (one row per page per fetch).
CREATE TABLE new_prices (
    id          bigserial PRIMARY KEY,
    model       text NOT NULL,              -- pricing key, 'rtx 3060 12gb'
    url         text NOT NULL,              -- the Zboží product page
    product     text,                       -- its name there
    min_price   integer,                    -- Kč, lowest offer; NULL = no offers right now
    shops       integer,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    error       text
);
CREATE INDEX new_prices_model_idx ON new_prices (model, fetched_at DESC);
CREATE INDEX new_prices_url_idx ON new_prices (url, fetched_at DESC);

-- Zboží.cz gets its own request budget (row 3).
ALTER TABLE http_throttle DROP CONSTRAINT IF EXISTS http_throttle_id_check;
ALTER TABLE http_throttle ADD CONSTRAINT http_throttle_id_check CHECK (id IN (1, 2, 3));
INSERT INTO http_throttle (id) VALUES (3) ON CONFLICT DO NOTHING;
