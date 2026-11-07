-- The worker's distance backfill (geo.update_distances / repository.listings_needing_distance) scans
-- every active listing each cycle for distance_home != the current home point. Once it has caught up
-- almost every row matches and is skipped, so an index lets Postgres do that cheaply instead of a
-- sequential scan every cycle. Partial on active listings only: gone ads are never shown, so their
-- distance is never recomputed (listings_needing_distance filters the same way).
CREATE INDEX listings_distance_home_idx ON listings (distance_home) WHERE gone_at IS NULL;
