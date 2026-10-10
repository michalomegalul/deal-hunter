-- Phase 9: distance from home per listing, and whether a deal's ad offers shipping.

-- Straight-line km from home (geo.py), how the ad was located (psc | town | unknown), and the home
-- point it was computed for: a changed HUNTER_HOME_* recomputes everything, a changed PSČ or town
-- clears it (repository.save_listing).
ALTER TABLE listings ADD COLUMN distance_km real;
ALTER TABLE listings ADD COLUMN distance_src text;
ALTER TABLE listings ADD COLUMN distance_home text;

-- The ad offers shipping (a regex on the ad first, the LLM's reading second); decided when scored.
ALTER TABLE deals ADD COLUMN ships boolean;
