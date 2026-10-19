# Web UI

Back to the [README](../README.md).

A plain, read-only LAN website (`hunt web`, port 8080): server-rendered HTML, one small inline CSS
block, no JS framework, no build step (stdlib `http.server`, nothing new in `pyproject.toml`). It
only reads the database - the scraper, worker and feedback service are unaffected.

- `/` - recent deals (listings that would alert or were alerted: `would_alert`, `queued`, `alerted`,
  `capped`), title linking to the ad, price, fair price, discount %, distance, the LLM's verdict,
  category and when it was flagged/pushed. `?category=gpu|office_pc|office_laptop|hdd|office` and
  `?min_discount=30` filter the list.
- `/prices` - the fair price table (model, n, median, level, new price, prior/note), the same
  estimate `hunt prices` prints; pooled disk classes show Kč/TB.
- `/auctions` - open nms.gov.cz lots with their valuation (max bid, profit, confidence) and end time.
- `/flips` - the flips log (buy, sell, fees, profit, days held) with its totals, and the `hunt eval`
  tables for the last 90 days. Read-only like the rest: flips are recorded with `hunt flip`.
- `/status` - when the background jobs last ran and what they did, from rows they already write.
  Sources comes first: one line ("4 of 8 sites enabled · 3 working · 1 stale") and a table with a
  row per site (Bazoš, Aukro, Mironet, Facebook Marketplace, Sbazar, Zboží.cz, Datart, nms.gov.cz):
  enabled, last data, volume and state. Enabled comes from the web service's own environment
  (`HUNTER_SOURCES`, `FB_COOKIES_FILE` set, `HUNTER_NEW_PRICES`, `HUNTER_AUCTIONS`; the web, worker
  and scraper services share `.env` in `deploy/compose.yml`); Sbazar is always "blocked
  (robots.txt)". Last data is the newest `last_seen` (listing sources, nms lots) or the newest
  successful `new_prices` read. State is OK, STALE (no data for 3 scrape loops for Bazoš and Aukro,
  2 x `HUNTER_SHOP_HOURS` for Mironet, 4 x the longest Facebook cycle, `HUNTER_NEW_PRICE_DAYS` + 2
  days for Zboží and Datart, 2 x `HUNTER_AUCTION_LIST_HOURS` for nms), never ran (enabled, no rows)
  or off (disabled sites keep their last data, greyed).
  LLM triage: last result, queue counts (pending split into due and waiting to retry, done, failed),
  the last 15 results (condition, verdict, seconds, cached, resulting deal state and score), 24 h
  count, average model time, cache hit rate and tokens, and the latest error. Watchlist checks: last
  check, 24 h checked/matched/pushed, last 10 hits. Auction extractions: last run, 7-day count, last
  5. Pipeline: latest scrape/recheck/backfill run, each section's last scrape time and the newest
  deal. A scrape run (Bazoš `scrape` or `scrape_aukro`) older than 30 minutes, a recheck older than
  12 hours, or a non-empty triage queue with no result for over an hour is marked STALE. These limits
  live in `health.py` and are the same ones `hunt watchdog` pushes about (see
  [Operations](operations.md#operations)). Not shown because it is not recorded: the model's reasoning (`think` is off) and
  when the watchlist filter last ran without producing a row. (The worker's heartbeat is recorded,
  for the watchdog, but not shown here.)
- `/listing/<id>` - one ad: the description, the LLM's triage answer (pretty JSON) and its price
  history.

Everything from an ad (title, description, seller, location, the LLM's verdict) is untrusted input
and is HTML-escaped before it reaches the page; every query is parameterized (no SQL is built in
`web.py` itself - it calls into `repository.py`, `pricing.py` and `auction_cycle.py`). Run it locally
with `uv run hunt web` (defaults to `0.0.0.0:8080`); in production it is its own compose service
(`web`), publishing host port 8090 (8080 is bot-arena's on CT 121) on the homelab's LAN only - no Cloudflare tunnel, since it was never
meant to be reachable from outside.
