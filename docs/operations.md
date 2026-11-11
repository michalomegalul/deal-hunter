# Deploy and operations

Back to the [README](../README.md).

Its own compose stack in `/opt/deal-hunter` on the homelab (CT 121, `deploy/compose.yml`):

| Service | What it runs |
|---------|--------------|
| `db` | Postgres 17 |
| `scraper` | `hunt scrape --loop` (Bazoš, plus Aukro/Mironet per `HUNTER_SOURCES`) |
| `rechecker` | `hunt recheck --loop` |
| `worker` | `hunt work --loop` (256 MB; the auction, new-price and watchlist steps run inside it; the LLM cache lives in the `hunterdata` volume) |
| `feedback` | `hunt feedback --loop` (160 MB; the live 👍/👎 listener, sharing the LLM cache) |
| `watchlist` | `hunt watch listen --loop` (96 MB; idles quietly until `NTFY_WATCH_TOKEN` is set) |
| `web` | `hunt web`, published on host port 8090, LAN only |
| `fb-scraper` | optional, own profile and image: `docker compose --profile fb up -d fb-scraper` |

Publishing a GitHub Release builds the image (base images from `mirror.gcr.io`, since shared runners hit
Docker Hub's and ECR's pull limits) and pushes it to GHCR. `hunter-deploy.timer` notices the new
release within 5 minutes and runs `deploy/deploy.sh`: pull, back up the database, migrate, restart the
services, then install the host scripts and systemd units from the same release (so changes to them
ship like code), then prune old release images (never `:latest`, never one a running container still
uses). These steps only run when the release tag actually changed; a failed attempt at a given tag is
retried with an exponential backoff (capped at 2h, tracked in `.deploy_attempt`) instead of every tick.
Nothing on GitHub can reach into the server. Secrets live only in
`/opt/deal-hunter/.env`: the database password, `NTFY_TOKEN` (user `deal-hunter`),
`NTFY_FEEDBACK_TOKEN` (user `deals-feedback-writer`) and `NTFY_WATCH_TOKEN` (read-only on the `watch`
topic), next to settings such as `HUNTER_ALERTS_ENABLED` and `HUNTER_SOURCES`. Ollama runs on CT 100
(`onboot=1`). If it is off, candidates (and watch matches) simply wait.

## Operations

- **Backups** (`hunter-backup.timer`, 03:30 daily, and before every migration): `deploy/backup.sh`
  writes a `pg_dump -Fc` to `/opt/deal-hunter/backups/` and keeps 14 days. It aborts instead of
  dumping if free disk space is below 500 MB or less than 3x the previous dump's size, and cleans up
  its `.dump.tmp` file if the dump fails partway through.
  Restore with `docker compose exec -T db pg_restore -U hunter -d hunter --clean --if-exists <
  backups/<file>.dump`. The dumps sit on the same disk as the database: they cover a bad migration or a
  mistake, not a lost disk.
- **Watchdog** (`hunter-watchdog.timer`, every 10 min, `hunt watchdog`): pushes once when something
  looks dead or stuck - no Bazoš (or, when enabled, Aukro) scrape run for 30 min, no recheck for 12 h,
  no worker cycle (heartbeat) for 30 min, a non-empty triage queue with no result for an hour (Ollama
  down), or an optional worker step (auctions, new prices, watchlists) failing for 30 min or more. An
  open problem is repeated once a day; a "recovered" push follows when it clears. The limits live in
  `health.py` and are the same ones `/status` marks STALE.
- **When the host itself fails** (`hunter-notify-failure@.service`): if the deploy, backup or watchdog
  unit fails (database down, broken image), `deploy/notify.sh` pushes straight from the host with
  `curl`. A GitHub API outage is not a failed deploy. The same title+message is pushed at most once
  per hour (state under `/run/deal-hunter-notify`, falling back to `/var/tmp`); the first occurrence
  always pushes immediately. Each of these oneshot units also has a `TimeoutStartSec` (backup 15 min,
  deploy 20 min, watchdog 5 min) so a hung run can't block the next one forever.
- **Retention**: the worker prunes old data once a day on its own (`alerts.maybe_prune`, gated through
  `kv` so it only runs once per Prague day, logged in the cycle summary as e.g. "pruned 42 old
  row(s)/file(s)"). It deletes `scrape_runs` after 90 days, `new_prices` and `fair_prices` history
  after a year, and `watch_hits` after 90 days; it clears `triage.raw` and a listing's `description`
  once that ad has been gone for 30 / 180 days respectively (a still-active ad keeps both); and it
  deletes LLM cache files (`HUNTER_LLM_CACHE`) older than 90 days. Run it by hand with
  `hunt db prune` (same code path, prints what it removed).
