-- Phase 7: auction lots (nms.gov.cz, state property), what the LLM found in them, their valuation,
-- and the pushes sent about them. Ended lots keep their final price: real sale prices.

CREATE TABLE auction_lots (
    source          text NOT NULL,          -- 'nms'
    lot_id          text NOT NULL,
    url             text NOT NULL,
    title           text NOT NULL,
    description     text,
    category        text,
    institution     text,
    district        text,
    zip_code        text,
    address         text,                   -- the selling office (pickup is usually there)
    inspection      text,
    conditions      text,
    status          text NOT NULL,          -- prepared | active | ended (sold) | unsold
    start_price     integer,                -- "Nejnižší podání"
    current_price   integer,                -- the latest price we saw while it ran
    final_price     integer,                -- "Konečná cena", only for lots that sold
    min_increment   integer,
    bids            integer,
    start_at        timestamptz,
    end_at          timestamptz,
    attachments     jsonb NOT NULL DEFAULT '[]',   -- [{"name", "url"}], URLs only
    images          jsonb NOT NULL DEFAULT '[]',
    attachment_text text,                   -- the item list read from list-like PDFs (compacted)
    content_hash    text,                   -- of what the LLM reads; a change means extract again
    detail_at       timestamptz,            -- last detail page fetch
    final_checked   boolean NOT NULL DEFAULT false,  -- detail fetched after the end (final price)
    valuation       jsonb,                  -- the latest valuation (auctions.Valuation)
    valued_at       timestamptz,
    qualifies       boolean NOT NULL DEFAULT false,
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, lot_id)
);
CREATE INDEX auction_lots_status_idx ON auction_lots (status, end_at);

-- The LLM's reading of a lot. A new prompt version, model or lot text gives a new row.
CREATE TABLE auction_extractions (
    source         text NOT NULL,
    lot_id         text NOT NULL,
    llm_model      text NOT NULL,
    prompt_version integer NOT NULL,
    content_hash   text NOT NULL,
    status         text NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts       integer NOT NULL DEFAULT 0,
    next_try_at    timestamptz NOT NULL DEFAULT now(),
    result         jsonb,
    raw            text,
    notes          jsonb NOT NULL DEFAULT '[]',
    error          text,
    stats          jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, lot_id, llm_model, prompt_version, content_hash),
    FOREIGN KEY (source, lot_id) REFERENCES auction_lots (source, lot_id) ON DELETE CASCADE
);

-- Pushes about lots: the first one, the ~24 h and ~2 h reminders. One of each per lot.
-- A row with ntfy_id NULL means "covered by an earlier push" (found less than 24 h before the end).
CREATE TABLE auction_alerts (
    source    text NOT NULL,
    lot_id    text NOT NULL,
    kind      text NOT NULL,                -- first | day | final
    sent_at   timestamptz NOT NULL DEFAULT now(),
    ntfy_id   text,
    price     integer,
    max_bid   integer,
    profit    integer,
    PRIMARY KEY (source, lot_id, kind),
    FOREIGN KEY (source, lot_id) REFERENCES auction_lots (source, lot_id) ON DELETE CASCADE
);
CREATE INDEX auction_alerts_sent_idx ON auction_alerts (sent_at) WHERE ntfy_id IS NOT NULL;

-- The auction site gets its own request budget (row 2), slower than Bazoš (row 1).
ALTER TABLE http_throttle DROP CONSTRAINT IF EXISTS http_throttle_id_check;
ALTER TABLE http_throttle ADD CONSTRAINT http_throttle_id_check CHECK (id IN (1, 2));
INSERT INTO http_throttle (id) VALUES (2) ON CONFLICT DO NOTHING;
