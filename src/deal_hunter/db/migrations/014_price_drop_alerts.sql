-- Price-drop alerts: ads that are no longer fresh candidates (older than FRESH_DAYS, so
-- find_candidates/active_priced never looks at them again) but whose price later fell further.
-- One row per listing, ever: dedupes the push so a listing is never alerted for a price drop twice.
-- See alerts.py (price_drop_step) and db/repository.py (price_drop_candidates, record_price_drop_alert).

CREATE TABLE price_drop_alerts (
    listing_id bigint PRIMARY KEY REFERENCES listings (id) ON DELETE CASCADE,
    old_price  integer NOT NULL,     -- the first-seen price (price_changes.old_price of its earliest row)
    new_price  integer NOT NULL,     -- the price at the time of the push
    alerted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX price_drop_alerts_alerted_idx ON price_drop_alerts (alerted_at);
