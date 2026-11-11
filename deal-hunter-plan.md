# deal-hunter: outage follow-up and improvement plan (DRAFT - paused 2026-11-10, not final)

## Context

The owner asked for a plan to make deal-hunter better and more usable (bugs, dead code, optimizations,
features) and reported "unavailable" notifications from the server.
This file is a work-in-progress record so the session can resume; the final plan is not written yet.

## Where things stand

- Server: CT 121, hostname `bot-arena`, LAN `192.168.4.41`, web UI on `:8090`, stack in `/opt/deal-hunter`.
  Reachable from the laptop over Tailscale subnet routing. No SSH key from the laptop works; the owner
  runs commands and pastes the output.
- Outage (2026-11-09 00:48 UTC to 2026-11-10 ~14:57 UTC): root disk 100% full, Postgres refused
  connections ("could not write init file: No space left on device"), web returned 500 on every path,
  watchdog and backup units failed and pushed every 10 min.
- Fixed for now by `docker builder prune -af` (2.9 GB). Disk at 89% (2.7 GB free). Web returns 200,
  fresh backup taken 14:57.
- ROOT CAUSE OF THE DISK FILLING: `deal-hunter_pgdata` is 6.4 GB but the database is ~80 MB.
  The volume is full of Postgres core dumps: `/var/lib/postgresql/data/core.<pid>`, 146 MB each
  (roughly 40 of them). Postgres backends are crashing with a core-dumping signal.
  Not caused by bloat, WAL, replication slots, logs or backups (all checked, all small).

## Open questions for next session

1. Why do backends crash? Need from the server:
   `ls -la --time-style=long-iso /var/lib/postgresql/data/core.*` (dates and count) and
   `docker compose logs db | grep -B2 -A6 'terminated by signal'` (signal and the crashing query).
   Suspects, unconfirmed: SIGBUS from Docker's default 64 MB `/dev/shm` during parallel queries
   (compose sets no `shm_size`); a specific query (pids step by 3, so something retries); swap was
   953/1024 MB used.
2. Have the core files been deleted (they are safe to delete, ~6 GB)?
3. The pg_stat_activity query printed nothing in the paste; re-run if long transactions matter.
4. Review agent "dead code and performance" had not reported when the session paused.

## Plan outline (to be detailed)

### P0 - stop the disk filling again
- Delete core files; stop Postgres writing them (`ulimit core 0` via compose `ulimits: core: 0`),
  set `shm_size` on `db`, fix the crashing cause once known.
- `deploy/compose.yml`: `logging` max-size/max-file on all services.
- Watchdog: disk-space check that warns before 100%; a host-side check that still reports when the DB
  is down ("DB unreachable"), with a cooldown in `deploy/notify.sh` (today it repeats every 10 min).
- `deploy/backup.sh`: remove `*.dump.tmp` on failure, check free space first.
- `deploy/deploy.sh`: do not re-run backup every 5 min after a failed deploy; prune old release images.
- Web: 503 "database unreachable" page, `/healthz`, route before `repo.connect` (`web.py:1392`).

### P1 - confirmed bugs (from the review agents; verify each before fixing)
- Recheck and thumbs-up follow-up fetch Aukro/Facebook/Mironet ads with the Bazoš client and mark them
  gone (`db/repository.py:181`, `feedback.py:67`). Corrupts the sold-fast pricing signal.
- Feedback and watchlist listeners never reconnect after a DB restart (`listener.py:46-58`,
  `cli/worker.py:239`, `cli/watch.py:65`).
- Watchdog checks only that a scrape started, not that it succeeded (`health.py:49-52`).
- Price parser joins all digit runs, can overflow int4 and stall the scrape loop (`bazos.py:121`,
  `scraper.py:164`); non-JSON Aukro reply aborts the scrape step (`scraper.py:248`).
- Worker core steps unguarded (`alerts.py:714-724`); duplicate pushes when the ntfy reply is not JSON
  (`alerts.py:143`).
- Watch command parser misreads "do 50 km" and "max 1500 Kč" (`watchlist.py:101-137`).
- Web: `?min_discount=abc` and `/listing/²` give 500 (`web.py:1396`, `1432`).
- No `connect_timeout`/`statement_timeout` (`db/repository.py:28`); unbounded `Retry-After` (`http.py:89`).
- systemd oneshot units have no `TimeoutStartSec`.

### P2 - usability
- Home page: hide gone ads, limit and paging, show why a deal was not pushed, Czech state names.
- Capped deals are dropped for good after the 5th push of the day (`alerts.py:579`): carry over.
- Cache `pricing.run` in the web process (recomputed on every page view; 5 call sites).
- Finish the Czech (~80 strings), `lang="cs"`, dark mode, larger tap targets, tables as cards on phones.
- Watch pushes: Stop / thumbs buttons; `start`, exclude terms; mute/snooze a model.
- "Why was this not alerted" on `/listing/<id>` and as `hunt why`.
- Config validation at startup (unknown source, unknown home postcode silently becomes Prague).

### P3 - features and cleanup
- Price-drop alerts after day 3, resale-profit line, flips CSV export, off-disk backup copy,
  cross-source duplicate detection, use LLM-extracted RAM/SSD.

### P3b - performance (third agent, reported after the pause; index claims are unverified, no EXPLAIN run)
- Web recomputes all fair prices per request: `pricing.run` at `web.py:423, 585, 687, 827, 901`. Cache it.
- `deals_in_state` unbounded with a per-row triage subquery (`db/repository.py:594`), used by `/` and
  `/category`; add a count query and a limit.
- Worker redoes identical work every 5 min: fair-price upsert (`alerts.py:715`), candidate upserts
  (`alerts.py:400-412`), `deals_in_state("triage")` 4 times plus `get_triage` per deal (`alerts.py:422-481`),
  reconnect + `repo.migrate` every cycle (`cli/worker.py:66`, `cli/scrape.py:17`).
- Every list-page sighting is its own `FOR UPDATE` + full-row UPDATE (`db/repository.py:62-147`);
  batch the `last_seen`-only case.
- Category/search sort applied in Python after `LIMIT 1000` (`db/repository.py:440-505`): wrong past
  1000 rows; sort in SQL and page.
- No retention anywhere (no DELETE in src or deploy): `scrape_runs`, `new_prices`, `fair_prices`,
  `triage.raw`, `watch_hits`, gone listings' text, LLM cache files. Add a daily prune.
- Candidate indexes to verify with EXPLAIN: `listings(last_seen)`, `normalized(category)`,
  `scrape_runs(kind, started_at DESC)`, trigram on `listings.title`.
- CI: gha cache scope shared by the two image builds (`release.yml`), CI runs twice on PR branches,
  CI never builds the image.

### P3c - dead code and duplication (confirmed by grep)
- Unused: `apple.py:112 IMAC_SCREEN`; test-only: `repository.get_watch`, `bazos.RSS_URL`,
  `categories.icons`; unused params `aukro.category_url(seo_url)`, `_feedback_context(throttle_conn)`,
  `watchlist.handle(now)`; unused fixtures `detail_hdd_224787091.html`, `detail_hdd_224903856.html`;
  sbazar still an enable-able source with no parser.
- Duplicates: `_row(...)` block 4 times in `web.py`; `listings_in_categories` vs `listings_by_models`;
  `WANTED`/`ACCESSORY` regex in 5 normalizers; thousands formatting 10 times (`text.kc` exists);
  `web.ago` vs `health._ago` (disagree); source labels in `alerts.py:48` and `web.py:178`.
- Write-only columns: `auction_lots.district/zip_code/conditions/valued_at`, `listings.distance_src`,
  `deals.ntfy_id`, `followups.ntfy_id`, most `scrape_runs` counters.
- Doc drift: `categories.yaml:9-13`, `docs/web.md:14`, `alerts.py:611 gate_status` hard-codes 3 leaves.
- Big-module split (web.py, repository.py, storage.py, alerts.py) is optional, lowest priority.

## Constraints noted
- `tests/test_web_db.py:595ff` asserts no border-radius, no emoji, no en/em dashes on any page.
- Web UI is read-only by design and has no client-side JS; actions from the web need auth and CSRF.
- Owner commits and pushes himself; no AI attribution in commits.

## Verification (to be detailed)
- `uv run pytest` (DB tests need `DATABASE_URL`), `uv run ruff check`.
- After deploy: `curl 192.168.4.41:8090/healthz`, `/status` shows no STALE, `df -h /` stable over days,
  no new `core.*` in pgdata.

## Status 2026-11-10 (evening session): implemented in the working tree, not committed

Done (all unit tests pass: 2205 passed, 10 skipped; ruff clean; DB-gated tests could not run locally,
Docker daemon inactive):

- P0 deploy: compose `ulimits core 0` + `shm_size 256m` on db, json-file logging on all services,
  backup free-space check + tmp cleanup, deploy backoff after a failed release + old-image prune,
  notify.sh 60 min cooldown, `TimeoutStartSec` on the three oneshot units, docs/operations.md.
- P0 watchdog/web: disk-space check (HUNTER_WATCHDOG_DISK_PATH/_MIN_MB), failed scrape runs reported,
  "DB unreachable" instead of traceback (`health.watchdog_cli`, wired into `hunt watchdog`),
  `GET /healthz`, 503 page on DB errors, `?min_discount=abc` -> ignored, `/listing/²` -> 404.
- P1: all items confirmed and fixed (recheck/follow-up Bazoš-only, listener reconnect, connect/statement
  timeouts, price parser + clamp, Aukro non-JSON, guarded worker steps, ntfy non-JSON reply, Retry-After
  cap, watch parser "do 50 km"/"max 1500 Kč").
- P2/P3b/P3c partial: pricing cache in web (60 s), home page hides gone + limit 200, `_offer_row`,
  `text.ago`, `sources.SOURCE_LABELS`, dead code removed, shared `title_filters.WANTED`, source
  validation in `load_settings`, postcode fallback warning, gate_status from category tree, doc drift.

Root cause, confirmed on the server over SSH (root@192.168.4.41 works from the laptop):
- The disk was full first; the core dumps were a consequence. Raw docker json log of the db container
  (`docker compose logs` no longer shows it; the file has NUL runs from the disk-full period):
  `2026-11-09 00:28:42 PANIC: could not write to file "pg_logical/replorigin_checkpoint.tmp": No space
  left on device` -> `checkpointer process (PID 27) was terminated by signal 6: Aborted` -> `all server
  processes terminated; reinitializing`, repeating every 5 min (checkpoint_timeout). Each PANIC dumps a
  ~150 MB core. `crash-evidence.txt` on the server lists 179 cores, the first at 2026-11-08 21:31 (many
  0 bytes because the disk was already full). SIGBUS/shm suspicion refuted. `pg_stat_checkpointer.stats_reset`
  = 2026-11-10 14:56 (last crash recovery, when the owner freed space).
- What filled the 24 GB disk: 29 images / 11.6 GB (6.4 GB reclaimable): 16 deal-hunter tags (v0.1.0 to
  v0.14.1, ~235 MB each) and 6 bot-arena tags (~1.4 GB each) plus timescaledb 2.2 GB, because both
  deploy.sh scripts run `docker image prune -f`, which never removes tagged images; plus 2.9 GB builder
  cache (pruned 2026-11-10), plus the cores. Now: 14 GB used, 8.4 GB free, cores deleted.
- Neighbour risk found on the same CT (4 GB RAM): bot-arena's timescaledb runs with shared_buffers =
  3958 MB (tuned for the Proxmox host, not the CT) and no mem_limit; 732 MB of it sits in swap and swap
  is 100% used. An OOM there can take deal-hunter's Postgres with it. Not this repo: set TS_TUNE_MEMORY
  (or shared_buffers ~512MB-1GB) in /opt/bot-arena/compose.yml and add a mem_limit.

Owner actions on the server (remote writes are blocked for Claude):
- `docker rmi` the old deal-hunter and bot-arena tags (the new deploy.sh does this for deal-hunter on
  the next release; bot-arena's deploy.sh needs the same change).
- Fix the timescaledb memory above.

Not done / needs the owner:
- sbazar in HUNTER_SOURCES stays accepted (an existing test asserts it shows as "blocked").
- Remaining P2 (Czech strings, dark mode, Stop/thumbs on watch pushes, `hunt why`, carry-over of capped
  deals), P3 features, retention prune, sort-in-SQL paging, index EXPLAINs, CI cache scope.
- Hand-rolled thousands formatting still in storage.py, triage.py, geo.py, auctions.py, watchlist.py,
  cli/common.py, cli/scrape.py (use `text.thousands`).
- Run the DB tests with Docker up (`docker compose up -d && DATABASE_URL=... uv run pytest -q`) before
  releasing; new DB tests in test_db.py, test_web_db.py, test_health_db.py are unverified.

## Feature backlog for the next session (ordered; one PR each; start here)

Prereq: commit the 2026-11-10 working tree, run DB tests with Docker up, release (restarts the db
container, clears the leaked swap).

1. **Carry over capped deals** (`alerts.py:~579`): deals that miss the 5-per-day push cap stay `queued`
   with a `carried_over` flag and go first next day instead of being dropped. Test in test_alerts.py.
2. **"Why not alerted"**: `alerts.gate_status`-style explanation per listing (no model, no fair price,
   discount below threshold, triage rejected, capped, gone). Show on `/listing/<id>` and add `hunt why
   <id>`. Pure function in alerts.py + web template + cli/worker.py; tests in test_alerts.py/test_web.py.
3. **Watch pushes get actions** (`watchlist.py`, `followup.py`): ntfy action buttons Stop / thumbs on
   watch hits, `start` command, exclude terms (`-slovo`), `mute <model> 7d`. Parser tests first.
4. **Web usability**: finish the ~80 Czech strings, `lang="cs"`, dark mode via `prefers-color-scheme`,
   tap targets >= 44 px, tables as cards under 600 px. Keep the no-JS and no-border-radius/emoji/dash
   rules (tests/test_web_db.py:595ff). Add paging to `/` and `/category` (sort in SQL, `LIMIT/OFFSET`,
   fixes the wrong order past 1000 rows in `db/repository.py:440-505`).
5. **Retention**: `hunt db prune` (scrape_runs > 90 d, new_prices > 1 y, fair_prices history > 1 y,
   triage.raw for gone listings > 30 d, watch_hits > 90 d, gone listings' description > 180 d, LLM cache
   files > 90 d) run daily from the worker (optional step) and documented in docs/operations.md.
6. **Price-drop alerts** after day 3 (`price_changes` table exists): push when a watched/candidate ad
   drops >= 15 % below its first seen price. Worker step, guarded like the others.
7. **Resale-profit line** in pushes and `/listing`: fair price minus ask minus pickup cost; **flips CSV
   export** at `/flips.csv`.
8. **Cross-source duplicate detection**: same title+price within 7 days across bazos/aukro/fb -> link
   as one deal, push once. Needs a `listing_groups` migration.
9. **Use LLM-extracted RAM/SSD** in office PC/laptop fair price (spec-adjust flag in categories.yaml);
   `laptop_spec.py` already parses specs from titles, extend to triage output.
10. **Off-disk backup copy**: `deploy/backup.sh` rsync/rclone the newest dump to another host; alert
    from the watchdog if the newest off-disk copy is > 2 days old.

Perf/index follow-ups (verify with EXPLAIN on the server, `psql` over SSH is read-only safe):
`listings(last_seen)`, `normalized(category)`, `scrape_runs(kind, started_at DESC)`, pg_trgm on
`listings.title` for `/hledat`; batch the `last_seen`-only sighting update (`db/repository.py:62-147`).
Remaining `text.thousands` call sites: storage.py, triage.py, geo.py, auctions.py, watchlist.py,
cli/common.py, cli/scrape.py. CI: separate gha cache scopes per image in release.yml, build the
runtime image on PRs.
