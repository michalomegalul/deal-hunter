-- Phase 10: e-shops. Two things, kept apart on purpose:
--
-- 1. Shop outlets (Mironet.cz "rozbaleno" / "bazar", see shops.py) are listings like any other
--    (source = 'mironet'), but their prices are shelf prices. They get price_kind = 'retail', which
--    nothing that estimates a used fair price reads (price_samples keeps to 'fixed'); candidates and
--    watchlists do read them. No schema change is needed for that.
--
-- 2. Datart.cz category pages are a second feed of new prices next to Zboží (datart.py). A new_prices
--    row now says who quoted it; a model's new price is the lowest of the fresh rows of all shops.
ALTER TABLE new_prices ADD COLUMN shop text NOT NULL DEFAULT 'zbozi';  -- zbozi | datart
CREATE INDEX new_prices_shop_idx ON new_prices (shop, fetched_at DESC);

-- The shop outlets get their own request budget (row 4); the Datart feed shares Zboží's (row 3),
-- both being the weekly new-price step of the worker.
ALTER TABLE http_throttle DROP CONSTRAINT IF EXISTS http_throttle_id_check;
ALTER TABLE http_throttle ADD CONSTRAINT http_throttle_id_check CHECK (id IN (1, 2, 3, 4));
INSERT INTO http_throttle (id) VALUES (4) ON CONFLICT DO NOTHING;
