-- Phase 10 part 2: watch commands -- start/stop (start reuses watches.active, no change there),
-- exclude terms (already fit in keywords.exclude, no schema change needed), mute (snooze pushes
-- without losing matches) and a vote on a watch hit, tapped from the push's own action buttons.
-- See watchlist.py and db/watch_repo.py.

-- NULL = not muted. While set and in the future, a watch is still scanned and matched (recorded in
-- watch_hits as usual) but never pushed -- see watchlist.push_matches.
ALTER TABLE watches ADD COLUMN muted_until timestamptz;

-- 'up' | 'down' | NULL, the last vote only (no history). Set from the push's 👍/👎 buttons.
ALTER TABLE watch_hits ADD COLUMN vote text;
