-- Phase 10: more listing sources (Sbazar, Aukro, Facebook Marketplace) feeding the same pipeline.
--
-- `source` already existed (default 'bazos', since 001_initial.sql) but nothing used it yet, and
-- `id` is the site's own ad id, which is only unique *within* Bazoš. Other sources have their own,
-- unrelated id spaces, so `id` here becomes a deterministic hash of (source, native id) for every
-- non-Bazoš row (see sources.make_id) -- Bazoš rows keep their literal ad id, unchanged. The
-- site's own id moves to `external_id`; the (source, external_id) unique index is what actually
-- enforces "ids unique per source", independent of any hash collision in `id` (astronomically
-- unlikely, but `id` is a derived convenience, not the source of truth).
ALTER TABLE listings ADD COLUMN external_id text;
UPDATE listings SET external_id = id::text WHERE external_id IS NULL;
ALTER TABLE listings ALTER COLUMN external_id SET NOT NULL;
CREATE UNIQUE INDEX listings_source_external_id_idx ON listings (source, external_id);
CREATE INDEX listings_source_idx ON listings (source, first_seen);

-- Aukro lists both auctions and fixed-price ("bazar") offers; an auction's current bid is not a
-- sale price (price_kind = 'auction', excluded from price_samples same as 'negotiable' etc.), but
-- its end time is worth keeping for display and for a future "final price of ended auctions" use.
ALTER TABLE listings ADD COLUMN auction_end_at timestamptz;
