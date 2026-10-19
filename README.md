# deal-hunter

Watches second-hand ads on [Bazoš.cz](https://pc.bazos.cz/) (graphics cards, ex-office mini PCs
and business laptops, and hard disks for a home server), works out what each model normally goes for, has a local LLM read the ads that look cheap, and
pushes only the genuinely good deals to a phone (ntfy). It also watches the state's IT auctions on
[nms.gov.cz](https://nms.gov.cz/) (lots like "14× Dell Latitude 5511") and works out the highest bid that
still pays after refurbishing and reselling the units.

Second-hand markets are much less efficient than stock markets: people price by gut feeling and
good deals are gone within hours. The bottleneck is data, so the scraper runs first and everything
else is built on what it collects.

## What it does today

- **Scraper** (`hunt scrape --loop`, every 10 min): the RSS feed and first category pages of the Bazoš
  sections for graphics cards, PCs, laptops and hard disks, plus Aukro's JSON API; a detail page only
  for new ads worth keeping. Five watch-only Bazoš sections (RAM, network, cases, motherboards) every
  45 min. The **rechecker** (every 6 h) marks ads that have gone; a one-off **backfill** seeds history.
  See [Sources and politeness](docs/sources.md).
- **Normalizers**: rule-based, conservative mapping of titles to pricing keys: GPUs (`rtx 3060 12gb`,
  from a YAML catalog), ex-office PCs and business laptops (`thinkpad t480`, `optiplex 7060 micro`,
  with RAM/SSD/CPU from the title) and hard disks (`hdd35 nas`, pooled per class and priced in Kč/TB).
  Anything unclear stays unmatched and shows up in `hunt review`.
- **Worker** (`hunt work --loop`, every 5 min): fair prices per model, candidate deals, LLM triage of
  the candidates, the deal score, the pickup-radius rule and ntfy pushes once a model has enough data,
  plus a Sunday digest. See [Pricing](docs/pricing.md), [LLM triage and the deal score](docs/scoring.md)
  and [Alerts](docs/alerts.md).
- **Auctions** (inside the worker): IT lots from the ÚZSVM state auctions on nms.gov.cz, the LLM's
  reading of each lot, a valuation with a max sensible bid, pushes before the end, and final prices of
  ended lots as real sale prices. See [State auctions](docs/auctions.md).
- **New prices** (inside the worker, weekly): the lowest current offers on Zboží.cz (curated product
  pages) and Datart.cz (category pages), plus launch MSRPs; shown in pushes, used as a sanity check and
  as a prior for thin models. See [Pricing](docs/pricing.md#new-prices-and-priors).
- **Feedback** (`hunt feedback --loop`): 👍/👎/"Bought ✅" taps arrive over a live ntfy stream; a 👍
  sends a detailed follow-up with a ready-to-send Czech message to the seller; "Bought ✅" starts the
  flips log (`hunt flip`, `hunt eval`). See [Alerts, follow-ups and flips](docs/alerts.md).
- **Watchlists** (`hunt watch listen --loop` + a worker step): standing "I want X" searches managed
  from the phone, matched against every new listing by a keyword prefilter and an LLM check. See
  [Watchlists](docs/watchlists.md).
- **Web UI** (`hunt web`): a read-only LAN site with deals, fair prices, auctions, flips and a
  `/status` page. See [Web UI](docs/web.md).
- **Watchdog** (`hunt watchdog`, a host timer): pushes when a job goes stale or keeps failing. See
  [Operations](#operations).

## Documentation

| File | What's in it |
|------|--------------|
| [docs/pricing.md](docs/pricing.md) | Fair price v1, RAM/SSD adjustment, new prices (Zboží, Datart, MSRPs), priors, the sanity check |
| [docs/scoring.md](docs/scoring.md) | LLM triage (prompt, schema, evidence checks, retries) and the deal score |
| [docs/alerts.md](docs/alerts.md) | ntfy pushes, the gate, quiet hours, 👍/👎, follow-ups, the flips log and `hunt eval`, distance and shipping |
| [docs/disks.md](docs/disks.md) | The storage normalizer, Kč/TB pricing, SMART triage, server fit |
| [docs/auctions.md](docs/auctions.md) | nms.gov.cz: reading, LLM extraction, valuation, pushes |
| [docs/watchlists.md](docs/watchlists.md) | Matching, phone commands, default watches |
| [docs/sources.md](docs/sources.md) | Request budgets, Aukro, Facebook, Mironet, sites that were checked and skipped |
| [docs/web.md](docs/web.md) | The LAN website and `/status` |

## Architecture

```
            RSS + category pages            detail pages (new ads only)
pc.bazos.cz ───────────────────► hunt scrape --loop ──────────► Postgres: listings
     ▲                                                              │   price_changes
     └────── detail pages of older ads ── hunt recheck --loop ◄─────┤   scrape_runs
                                                                    │
                                          hunt normalize ───────────┤   normalized
                                                                    │
   Ollama (CT 100) ◄── candidates ── hunt work --loop ◄─────────────┘   fair_prices, deals,
          ▲                               │                             triage
          │               push (deals)    ▼
          │                         ntfy (CT 116) ◄──► phone
          │      follow-up (deals)  ▲     │ 👍/👎/bought (deals-feedback, live stream)
          └──── triage ─ hunt feedback --loop ─────────────────────►  feedback, followups, flips
   nms.gov.cz ── lists, details, PDFs ──► auction step (in the worker) ──► auction_lots,
                                          auction_extractions, auction_alerts
   zbozi.cz ── product pages (weekly) ──► new-price step (in the worker) ──► new_prices
   datart.cz ── category pages (weekly) ─┘
   aukro.cz ── JSON API (each cycle) ──► hunt scrape (HUNTER_SOURCES) ──► listings
   mironet.cz ── outlet listings (6 h) ──┘                                listings ('retail')
```

- `src/deal_hunter/bazos.py`: pure parsers (category page, RSS, detail page), tested against real
  pages saved in `tests/fixtures/`. If Bazoš changes its layout, CI breaks instead of the scraper
  silently storing garbage.
- `cli/`: the `hunt` command, one module per group (`scrape`, `prices`, `worker`, `auctions`, `watch`,
  `flips`) plus `common.py`; `db/cli.py` adds `hunt db`.
- `http.py`: the polite client. `scraper.py`: scrape / backfill / recheck cycles.
- `db/`: raw psycopg 3 (`repository.py`) and numbered SQL migrations (`hunt db migrate`).
- `normalize.py` + `gpu_catalog.yaml`: the GPU normalizer; `office.py`: the office one;
  `storage.py` + `storage_catalog.yaml`: disks. `geo.py` + `cz_postcodes.csv`: distances, shipping
  detection and the pickup rule. `text.py`: shared text helpers (`fold`, `kc`).
- `pricing.py`: fair price v1. `triage.py`: Ollama client, prompt, tolerant parser, deal score.
  `alerts.py`: the worker cycle, gate, ntfy and digest. `health.py`: the worker heartbeat and the
  watchdog.
- `nms.py`: nms.gov.cz parsers (list API, detail page, PDF item lists), tested against saved responses.
  `auctions.py`: LLM extraction and valuation. `auction_cycle.py`: the auction step of the worker.
- `feedback.py`: the feedback service (votes, follow-ups). `followup.py`: the follow-up text, the offer
  and the message to the seller. `listener.py`: the ntfy stream loop (resume, reconnect with backoff)
  shared with the watch listener. `zbozi.py` + `new_price_pages.yaml`, `datart.py`: new prices.
- `sources.py`: the global id scheme every source shares (`make_id`). `aukro.py`: the Aukro JSON API
  parser. `facebook.py`: Playwright + GraphQL capture, its own `hunt scrape-fb --loop` cadence.
  `shops.py`: Mironet's unboxed/bazar outlet items (`retail` prices, never used-price samples).
- `evaluation.py`: flip profit and the `hunt eval` report. `watchlist.py`: the keyword prefilter, the
  LLM check, the worker's scan + push step, and the watch command listener. `web.py`: the LAN site.

## Usage

```bash
uv sync
docker compose up -d                    # local Postgres (dev credentials, see .env.example)
cp .env.example .env
uv run hunt db migrate
uv run hunt scrape --once --dry-run     # parse and print, nothing saved
uv run hunt scrape --once               # one real cycle
uv run hunt backfill --pages 15         # seed history (capped at 15 pages; --section pc|notebook|hdd)
uv run hunt recheck                     # re-check ads not seen for 6 h
uv run hunt normalize [--all]           # map titles to models (GPUs, office PCs/laptops)
uv run hunt review [--section pc]       # unmatched titles, to improve the catalog
uv run hunt stats
uv run hunt prices [--category hdd]     # fair price table (disks with Kč/TB vs new)
uv run hunt price --model "rtx 3080"    # one model, with its recent ads
uv run hunt work                        # one worker cycle (price, triage, score, push, feedback)
uv run hunt deals [--state would_alert] # candidates and what became of them
uv run hunt gate                        # which models may alert yet
uv run hunt triage 224777992            # ask the LLM about some ads (prints, stores nothing)
uv run hunt notify-test                 # one test push
uv run hunt notify-test --ad 224876851  # a real ad as a TEST deal push (photo, 👍 -> TEST follow-up)
uv run hunt feedback [--loop]           # handle taps on deals-feedback (once / the live service)
uv run hunt newprices [--fetch [--all]] # Zboží.cz pages / Datart.cz rows and their lowest offers
uv run hunt prices --no-prior           # raw medians, without the new-price/auction priors
uv run hunt auctions scrape --dry-run   # running IT lots on nms.gov.cz (nothing saved)
uv run hunt auctions scrape --once      # lists + due detail pages, LLM extraction, valuation (no pushes)
uv run hunt auctions list               # running lots with valuations, sold lots: final price/unit vs fair
uv run hunt auctions value 62388        # one lot: items, mapping, valuation breakdown
HUNTER_SOURCES=bazos,aukro uv run hunt scrape --once  # also read Aukro (docs/sources.md)
HUNTER_SOURCES=bazos,mironet uv run hunt scrape --once # also Mironet's outlet items (at most every 6 h)
uv sync --extra fb && uv run playwright install chromium   # only for the fb source
FB_COOKIES_FILE=cookies.json uv run hunt scrape-fb --once  # one fb cycle (no-op if unset)
uv run hunt flip list [--open|--sold]   # the flips log with profit and totals
uv run hunt flip add --ad 224876851 --price 4000   # a purchase made outside the 👍 flow
uv run hunt flip buy-price 224876851 3800          # the haggled buy price
uv run hunt flip sell 224876851 5200 --fees 150    # record a sale (--at YYYY-MM-DD, --notes)
uv run hunt flip fees 224876851 150                # postage, cleaning, parts
uv run hunt watchdog                    # check for stale/failing jobs, push if something is wrong
uv run hunt eval [--days 90]            # states, score bands, 👍 rate, how fast ads vanish, flip profit
uv run hunt web                         # the read-only LAN website (deals, prices, auctions, flips), :8080
uv run hunt watch seed                  # add the default homelab watches (idempotent)
uv run hunt watch add ddr4 16gb max 1500 km 50 #homelab  # add a watch from the CLI
uv run hunt watch list                  # every watch and its id
uv run hunt watch stop 3                # deactivate a watch
uv run hunt watch listen [--loop]       # handle add/list/stop/help commands from the watch ntfy topic
uv run pytest                           # DB tests need DATABASE_URL (they use <db>_test)
```

## Deploy

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
ship like code). Nothing on GitHub can reach into the server. Secrets live only in
`/opt/deal-hunter/.env`: the database password, `NTFY_TOKEN` (user `deal-hunter`),
`NTFY_FEEDBACK_TOKEN` (user `deals-feedback-writer`) and `NTFY_WATCH_TOKEN` (read-only on the `watch`
topic), next to settings such as `HUNTER_ALERTS_ENABLED` and `HUNTER_SOURCES`. Ollama runs on CT 100
(`onboot=1`). If it is off, candidates (and watch matches) simply wait.

### Operations

- **Backups** (`hunter-backup.timer`, 03:30 daily, and before every migration): `deploy/backup.sh`
  writes a `pg_dump -Fc` to `/opt/deal-hunter/backups/` and keeps 14 days.
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
  `curl`. A GitHub API outage is not a failed deploy.

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
- The normalizers are rule-based and deliberately conservative; they read titles only. A title
  that doesn't say the VRAM size of a multi-variant model (RTX 3060 8/12 GB) gets the coarser key
  `rtx 3060`. The same goes for a ThinkPad T14 without a generation or an OptiPlex without a form
  factor. Broken or "for parts" items are still matched; the LLM catches those.
- Office RAM/SSD/CPU come from the title only, and the RAM/SSD price adjustment is one shared
  slope per category, not per model. The CPU is recorded but doesn't affect the price yet.
- Reposts are detected by seller name and title, both of which a seller can change.
- The gate counts data-days per category from the first ad seen. GPU history starts 2026-10-09,
  office history (PCs, laptops) on 2026-10-09 with v0.3.0. Office volume is much lower, so office
  models take longer to reach 15 samples. Disk history starts with v0.7.0 (plus its 15-page backfill).
- `gemma3:12b` is a small model. It misreads things: pickup-only taken for shipping-only, a
  shop's "pay postage up front" taken for a prepay scam. The evidence check removes the worst of
  this, but treat the verdict as a hint, not a judgement.
- Fair prices are medians of asking prices, so a market that is falling shows up late, and a
  "deal" can just be an honest seller in a falling market.
- **Auction lots are sold as-is.** "Zadavatel ani Provozovatel nezaručují funkčnost": no warranty, no
  returns. The valuation assumes units work unless the text says otherwise; the real share of dead
  units is unknown. Inspect before bidding (most lots offer an inspection by appointment).
- **Auction costs we can't see**: registration in the auction system, a returnable deposit (kauce) on
  some lots, VAT (state offices usually sell without VAT; a stated "+ DPH" or "odpočet DPH" only helps a
  VAT payer), and transport. Pickup is in person at the seller's address, anywhere in the country: the
  flat 500 Kč + 2 h is a guess, check the distance.
- **Auction valuations need Bazoš fair prices** for the lot's model. Office history only started on
  2026-10-09, so for weeks most lots will show "no fair price yet"; consumer models and AIOs never get
  one. The resale factor (0.9), selling pace (5 a week) and labour (40 min a unit) are assumptions, not
  measurements.
- The auction LLM reads only the description, attachment names and up to two list PDFs. Scanned PDFs
  have no text; specs per unit are rarely given, so units are priced at the model's reference spec.
- **New prices are a hand-picked sample.** `new_price_pages.yaml` covers 19 models with 1-3 SKUs each;
  pages go stale as cards sell out, and older generations have no new offers at all. Launch MSRPs are
  launch prices (in USD, converted with a fixed rate), not what a card costs today.
- **The priors are assumptions**: the 0.75 used/new fallback, the 1.5 auction-to-retail uplift and the
  prior weights (5 samples; one per auction lot) are guesses until there is data to fit them.
- **The seller message is a template.** It only names the item and asks whether it is still available;
  the item name comes from the words the normalizer recognised, so an odd title gives a short name.
- **Distances are straight lines** from the PSČ's point (the median of its GeoNames places); by road it
  is 20-30 % more. GeoNames has some misplaced codes: three Prague ones are corrected, others elsewhere
  may be off. An ad without a known PSČ or town counts as out of range unless it ships.
- **Shipping is read from the ad**: a regex for clear offers ("pošlu", "Zásilkovna", "PPL", "dobírkou",
  ...) and refusals ("neposílám", "pouze osobní odběr"), the LLM's reading only when the ad is vague, and
  then only if the ad uses a shipping word at all.
- **Disks are thin data.** In the 300 ads of a first fetch (~6 days) only ~12 were 3.5" drives of 8 TB or
  more, now pooled per class rather than per class/capacity key, so a class's fair price (≥ 4 ads) and the
  alert gate (15) still take a couple of weeks; new prices from Zboží help as priors once a class has a
  few ads. The SMR table and the SAS/SATA suffix table are curated by hand from data sheets as remembered
  (see `storage_catalog.yaml`); the less certain suffixes are marked `uncertain` and don't call a drive
  SAS. A title without a part number, or an uncertain suffix, leaves the interface unknown (shown as
  "⚠ check SATA vs SAS"; SMR still shows "CMR?").
- Older and workstation cards (GTX 9xx and earlier, RX 400/500, Quadro, ...) are recognised but
  excluded as `out_of_scope`, so the first backfill (which reaches deep into cheap old stock) shows
  a high exclusion rate.

## Roadmap

- [x] **Phase 1: scraper + storage.** RSS + category pages every 10 min, details for new ads,
      6-hourly recheck for removal, price change history, fixture-tested parsers.
- [x] **Phase 2: GPU normalizer.** YAML catalog (GTX 10/16, RTX 20–50, RX 5000–9000, Arc A/B),
      VRAM variants kept apart, exclusions with reasons, ~200 hand-checked real titles in tests.
- [x] **Ex-office hardware.** PC and notebook sections, business mini/SFF PCs and laptops,
      RAM/SSD/CPU features, 437 hand-checked real titles in tests.
- [x] **Phase 3: fair price v1.** Weighted median of asking prices per model over 60 days: reposts
      collapsed, fast disappearances weighted double, outliers trimmed, confidence from n and
      spread, RAM/SSD adjustment for office hardware.
- [ ] **Phase 3 v2** (after 4–6 weeks of data): a regularized log-linear model (Huber or Ridge on log
      price, shrunk per model, monotonic in RAM/SSD) rather than LightGBM, which extrapolates badly
      on thin data; it must beat the median in a time-split evaluation.
- [x] **Phase 4: LLM triage.** Candidates only. gemma3:12b in Ollama with a JSON schema, an on-disk
      cache, evidence-checked scam signals, and a queue while Ollama is down.
- [x] **Phase 5: alerts.** ntfy pushes gated per model until there is enough data, at most 5 a day,
      quiet hours, repost dedup, 👍/👎 via a write-only feedback topic, and a Sunday digest.
- [x] **Phase 6: UI + ROI tracking.** Deals feed, price history per model, normalizer review,
      and a flips log (bought at, sold at, profit) to show whether it actually pays. The read-only
      part shipped as the plain LAN website (`hunt web`; see [Web UI](docs/web.md)): a deals feed with filters,
      the fair price table, open auctions, a listing detail page with its own price history, and
      a flips page. The flips log is done: `hunt flip` records purchases, haggled prices, fees and
      sales, `hunt eval` and `/flips` show profit, ROI and how the score bands and 👍/👎 compare.
      Still open: normalizer review in the UI (it is CLI-only, `hunt review`), and using the
      numbers to tune the thresholds, which is still done by hand.
- [x] **Phase 7: auctions / office clear-outs.** ÚZSVM state auctions (nms.gov.cz), IT lots via the
      list page's own JSON API, item lists from PDFs, LLM extraction with evidence checks, valuation
      with refurb costs, labour and selling pace, a max sensible bid, pushes before the end, and final
      prices of ended lots as real sale prices. verejnedrazby.cz and drazby.cz checked and skipped
      (no IT lots; drazby.cz disallows the PDFs).
- [x] **Phase 8: richer alerts + new prices.** The ad's photo in pushes; a live feedback stream
      instead of the 5-minute poll; 👍 sends a detailed follow-up (prices vs fair vs new, location,
      views, seller, the LLM's reading, the description, an evidence-checked Czech draft to the seller
      with our offer) with "Open ad" / "Bought ✅" (the start of the flips log); detailed follow-ups for
      auction lots. Launch MSRPs in the GPU catalog; lowest current offers from curated Zboží.cz product
      pages; a used ≥ 95 % of new sanity check; thin models shrunk toward new × used/new ratio (GPUs) or
      state auction prices (office); Fujitsu Esprimo and HP EliteOne/ProOne in the office catalog.
- [x] **Phase 9: distance, a human seller message, hard disks.** Distance from home (Prague) from a
      bundled GeoNames PSČ table, a pickup radius (per category), out-of-range deals only when the ad
      ships and still pays after postage; a fixed natural message to the seller instead of the LLM draft,
      the haggling price shown apart; the Bazoš `/hdd/` section with a storage normalizer (form, capacity,
      SATA/SAS, line, SMR, multi-disk ads), fair prices pooled per class in Kč/TB vs new (Zboží),
      SMART questions for the LLM, disk penalties and a server-fit filter (3.5" SATA, ≥ 8 TB).
- [ ] **Later (disks):** recognise drives inside NMS server lots; SnapRAID parity once a big disk arrives.
- [~] **Phase 10: more sources.** `source`/`external_id` columns, a per-source id scheme, Aukro's
      JSON API live in production (both auctions and fixed-price offers, auction end time stored);
      Sbazar blocked by robots.txt (`Disallow: /` for a generic bot, no code kept); Facebook
      Marketplace via Playwright + the owner's cookies, its own low-and-slow cadence, disabled unless
      configured. See [Sources and politeness](docs/sources.md).
      Shops (2026-10-12): Mironet's unboxed/bazar outlet as a source (opt-in, not enabled yet; `retail` prices kept out
      of the used fair prices, deal candidates and watchlist matches) and Datart as a second
      new-price feed next to Zboží. Alza, TSBohemia and CZC are Cloudflare-/Allegro-blocked and skipped.
- [x] **Phase 10 part 1: watchlists.** Standing "I want X" searches managed from a phone over ntfy
      (`watch`/`list`/`stop`/`help`, Czech or English) or the CLI, a keyword prefilter + LLM check
      against every new listing from any source/section, pushes with their own daily cap, five
      default homelab watches (`hunt watch seed`), five extra Bazoš sections (RAM, network, cases,
      motherboards) at a slower cadence. See [Watchlists](docs/watchlists.md).
- [x] **Operations.** Database backups (daily and before every migration), `hunt watchdog` with a
      worker heartbeat, host-side failure pushes, host scripts and units shipped with each release.

## Credits

- Postal code coordinates: [GeoNames](https://www.geonames.org/) postal code data for the Czech Republic
  (https://download.geonames.org/export/zip/CZ.zip), licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); compacted into
  `src/deal_hunter/cz_postcodes.csv` (one point per PSČ, the median of its places) by `geo.build_table`.
