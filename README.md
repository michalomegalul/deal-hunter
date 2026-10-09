# deal-hunter

Watches second-hand ads on [Bazoš.cz](https://pc.bazos.cz/) (graphics cards, plus ex-office mini PCs
and business laptops), works out what each model normally goes for, and (eventually) pushes only the
genuinely good deals to a phone.

Second-hand markets are much less efficient than stock markets: people price by gut feeling and
good deals are gone within hours. The bottleneck is data, so the scraper runs first and everything
else is built on what it collects.

## What it does today

- **Scraper** (every 10 min): for each watched section (graphics cards `/graficka/`, PCs `/pc/`,
  laptops `/notebook/`) reads the RSS feed and the first category pages (2 for GPUs, 1 for the
  others), and fetches the detail page only for ads it hasn't seen before. In the PC and laptop
  sections that is only ads the normalizer doesn't exclude (no detail pages for gaming PCs and
  MacBooks). Prices, views and location on the list pages refresh known ads for free; price changes
  go to `price_changes`.
- **Rechecker** (every 6 h): re-fetches active ads that have scrolled off the pages we read. A 404,
  410, "Inzerát je již vymazán" page or a redirect away from the ad sets `gone_at`.
- **Backfill** (once): walks up to 15 category pages to seed history.
- **GPU normalizer**: maps titles to canonical models (`rtx 3060 12gb`, `rx 6700 xt`, ...) from a
  YAML catalog, and excludes wanted ads, whole PCs, laptops, multi-card lots, boxes/coolers and
  mining rigs. Anything it isn't sure about stays unmatched and shows up in `hunt review`. Runs
  after every scrape cycle; bumping the catalog `version` re-processes everything.
- **Office normalizer** (`office.py`): ex-office/leasing hardware only. Business mini/SFF PCs
  (ThinkCentre M, OptiPlex, EliteDesk/ProDesk) and business laptops (ThinkPad T/X/L/P/E, Latitude
  5xxx/7xxx, EliteBook/ProBook) map to a model key such as `thinkpad t480`, `thinkpad t14 gen 3`,
  `optiplex 7060 micro` or `elitedesk 800 g4 mini`; RAM, SSD size and CPU are read from the title
  when present. Everything else in those sections (gaming PCs, MacBooks, consumer laptops,
  workstations) is `out_of_scope`, so `hunt review` only shows business titles it couldn't pin down.

## Being polite

- Only URLs that [robots.txt](https://pc.bazos.cz/robots.txt) allows: the category pages
  (`/graficka/`, `/graficka/20/`, ...), the RSS feed and `/inzerat/<id>/<slug>.php`. No search,
  price filter or ordering parameters (`hledat=`, `cenaod=`, `order=`, ...).
- At least 3 seconds between any two requests, shared across the scraper and the rechecker
  (a row lock in Postgres), an honest User-Agent that links to this repo, timeouts, and
  exponential backoff on 429/5xx (honouring `Retry-After`).
- A normal cycle is 7 requests (3 RSS feeds + 4 category pages) plus one per new ad worth keeping,
  typically under 10 in total, every 10 minutes. `HUNTER_SECTIONS` turns sections off.

## Architecture

```
            RSS + category pages            detail pages (new ads only)
pc.bazos.cz ───────────────────► hunt scrape --loop ──────────► Postgres: listings
     ▲                                                              │   price_changes
     └────── detail pages of older ads ── hunt recheck --loop ◄─────┤   scrape_runs
                                                                    │
                                          hunt normalize ───────────┘   normalized
```

- `src/deal_hunter/bazos.py`: pure parsers (category page, RSS, detail page), tested against real
  pages saved in `tests/fixtures/`. If Bazoš changes its layout, CI breaks instead of the scraper
  silently storing garbage.
- `http.py`: the polite client. `scraper.py`: scrape / backfill / recheck cycles.
- `db/`: raw psycopg 3 and numbered SQL migrations (`hunt db migrate`).
- `normalize.py` + `gpu_catalog.yaml`: the rule-based GPU normalizer.

## Usage

```bash
uv sync
docker compose up -d                    # local Postgres (dev credentials, see .env.example)
cp .env.example .env
uv run hunt db migrate
uv run hunt scrape --once --dry-run     # parse and print, nothing saved
uv run hunt scrape --once               # one real cycle
uv run hunt backfill --pages 15         # seed history (capped at 15 pages; --section pc|notebook)
uv run hunt recheck                     # re-check ads not seen for 6 h
uv run hunt normalize [--all]           # map titles to GPU models
uv run hunt review                      # unmatched titles, to improve the catalog
uv run hunt stats
uv run pytest                           # DB tests need DATABASE_URL (they use <db>_test)
```

## Deploy

Its own compose stack in `/opt/deal-hunter` on the homelab (CT 121): `db` (Postgres 17),
`scraper` and `rechecker`. Publishing a GitHub Release builds the image and pushes it to GHCR;
`hunter-deploy.timer` notices the new release within 5 minutes, pulls it, migrates and restarts
the services (`deploy/deploy.sh`). Nothing on GitHub can reach into the server. The database
password lives only in `/opt/deal-hunter/.env`.

## Honest limitations

- **These are asking prices, not sale prices.** Bazoš never says what an item sold for.
- **`gone_at` is only a proxy for "sold".** An ad disappears when it sells, but also when the
  seller gives up, deletes and reposts, or Bazoš removes it. An ad that vanishes soon after
  posting is a reasonable "probably sold at about this price" signal; nothing more.
- We notice disappearance with a delay: within ~10 min for ads on the first pages, up to ~6 h
  (or longer, if there are more than 1,500 ads due) for older ones.
- The date Bazoš shows moves when a seller renews or tops an ad, so `posted_at` is "last
  renewed", not necessarily "first posted". `first_seen` is our own timestamp.
- History only starts when the scraper started (plus a 15-page backfill). Prices for rare
  models will stay noisy for a long time.
- The normalizer is rule-based and deliberately conservative; it reads titles only. A title that
  doesn't say the VRAM size of a multi-variant model (RTX 3060 8/12 GB) leaves `vram_gb` empty.
  Broken or "for parts" cards are still matched; spotting those is a job for the LLM (phase 4).
- Older and workstation cards (GTX 9xx and earlier, RX 400/500, Quadro, ...) are recognised but
  excluded as `out_of_scope`, so the first backfill (which reaches deep into cheap old stock) shows
  a high exclusion rate.

## Roadmap

- [x] **Phase 1: scraper + storage.** RSS + category pages every 10 min, details for new ads,
      6-hourly recheck for removal, price change history, fixture-tested parsers.
- [x] **Phase 2: GPU normalizer.** YAML catalog (GTX 10/16, RTX 20–50, RX 5000–9000, Arc A/B),
      VRAM variants kept apart, exclusions with reasons, ~200 hand-checked real titles in tests.
- [ ] **Phase 3: fair price.** v1: trimmed median of asking prices per model over 60 days with a
      confidence from sample size; v2 (after 4–6 weeks of data): LightGBM on log price that must
      beat the median in a time-split evaluation.
- [ ] **Phase 4: LLM triage.** Only for candidates (price well below fair): a local Ollama model
      reads the description for condition, mining history, warranty, scam signals.
- [ ] **Phase 5: alerts.** ntfy pushes for the best few deals a day, quiet hours, dedup of reposts,
      a weekly market digest, 👍/👎 buttons that become training labels.
- [ ] **Phase 6: UI + ROI tracking.** Deals feed, price history per model, normalizer review,
      and a flips log (bought at, sold at, profit) to show whether it actually pays.
