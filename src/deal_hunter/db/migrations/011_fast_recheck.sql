-- A deal candidate that drops off the pages we read is re-checked soon (scraper.fast_recheck) rather
-- than by the 6-hourly rechecker, so its gone_at (the "probably sold" signal) is hours more precise.
ALTER TABLE deals ADD COLUMN rechecked_at timestamptz;
