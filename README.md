# deal-hunter

Watches second-hand ads on [Bazoš.cz](https://pc.bazos.cz/) (graphics cards, plus ex-office mini PCs
and business laptops), works out what each model normally goes for, has a local LLM read the ads that look cheap, and
pushes only the genuinely good deals to a phone (ntfy).

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
- **Worker** (every 5 min, `hunt work --loop`): fair prices per model, candidate deals, LLM triage
  of the candidates, the deal score, ntfy pushes (once the gate opens), 👍/👎 feedback and a
  weekly digest. Details below.

## Fair price (v1)

Per canonical model (`rtx 3080 10gb`, `thinkpad t480`, `optiplex 7060 micro`, ...), from matched ads
with a numeric price seen in the last 60 days (`pricing.py`):

1. **Reposts collapse** to one sample: same model, same seller or same title, price within 5 %.
2. **Probably-sold ads weigh double**: an ad that disappeared within 72 h of first being seen.
3. **Outliers go**: under 10 % or over 300 % of a first-pass median (typos, "1 Kč", boxes).
4. **Estimate**: the weighted median, with the weighted 25th-75th percentile range (IQR).
5. **Confidence** = `n/(n+5) × 1/(1 + (IQR/median ÷ 0.25)²)`. More samples and a tighter spread
   mean more trust. Levels: **high** ≥ 0.6 (and n ≥ 10), **medium** ≥ 0.35 (and n ≥ 6), **low**
   otherwise; under 4 samples there is no estimate.

Office hardware also depends on RAM and SSD. For each category (office PCs, office laptops) the
worker fits one shared log-price slope **per doubling** of RAM and of SSD. The fit uses
differences within each model (least squares on `log price − model mean` against
`log2 GB − model mean`), is shrunk toward a prior of +12 % (RAM) and +6 % (SSD) with the weight of
20 samples, and is clipped to 0-35 %. Samples are moved to their model's reference spec (median
RAM/SSD) before the median is taken. A specific ad's fair price is then moved back to its own spec,
and an unknown spec stays unadjusted. Keys without a generation or form factor (`thinkpad t14`,
`optiplex 7050`) mix variants; their wide spread shows up as lower confidence.

`hunt prices` prints the table (model, n, median, IQR, confidence); `hunt price --model "rtx 3080"`
shows one model (prefix match, so both VRAM sizes) with its recent ads. A daily snapshot goes to
`fair_prices` for trends. **v2** (LightGBM on log price, after 4-6 weeks of data) is not built yet.

## LLM triage

A **candidate** is an active ad first seen in the last 3 days, priced at least 20 % below its fair
price (`HUNTER_CANDIDATE_DISCOUNT`), whose model has **medium or high** confidence. Prices under
10 % of fair are treated as placeholders. Only candidates go to the LLM: `gemma3:12b` in Ollama on
CT 100, about 6-10 s per ad. The prompt is in English and the ad is Czech. The client sends a JSON
schema as Ollama's `format`, `temperature 0`, `think: false`, and caches answers on disk by
sha256(model + prompt) in the worker's data volume (`triage.py`, the pattern from bot-arena). The
answer gives condition (new / like_new / used / worn / broken), for_parts, defects,
mining_suspected, warranty_or_receipt, urgent_sale, scam_signals, is_bundle, is_wanted_ad, specs
(RAM/SSD/CPU) and a one-line verdict.

Small models like to tick boxes, so a **scam signal only counts with evidence**. The model has to
quote the ad, and the quote must really appear in it. A "shipping only" quote that actually offers
personal pickup ("pouze osobní odběr") is thrown out. A "shipping only" or "prepay" quote that says
nothing about shipping or payment is relabelled "other". Answers are stored in `triage` with the LLM
model and `PROMPT_VERSION`; bumping either triages again. If Ollama is down, the candidate stays
queued and is retried with backoff (10 min doubling up to 6 h). Scraping never waits for it. Two
unreadable answers in a row give a safe fallback (`failed`, never pushed).

## Deal score

```
score = discount × confidence × penalty          discount = 1 − price / fair (at the ad's spec)
```

| LLM says                                          | penalty |
|---------------------------------------------------|---------|
| wanted ad, for parts / broken                     | drop    |
| scam: shipping only, deposit / prepayment         | drop    |
| scam: too good, copied text, other (soft)         | × 0.6   |
| bundle (the fair price is for the item alone)     | × 0.5   |
| worn                                              | × 0.7   |
| any defects listed                                | × 0.8   |
| mining suspected                                  | × 0.8   |
| warranty or receipt                               | × 1.1   |

A push needs `score ≥ 0.12` (`HUNTER_ALERT_MIN_SCORE`). Example: 30 % under fair at confidence 0.6
with a receipt gives 0.3 × 0.6 × 1.1 = 0.198. Priority follows the discount: ≥ 45 % → 5 (urgent),
≥ 33 % → 4, else 3.

## Alerts (ntfy)

- Pushes go to topic `deals` on `ntfy.dobsinsky.dev`. The worker publishes over the LAN
  (`http://192.168.4.36`) as user `deal-hunter`. The message holds the title, price vs fair
  (−X %), n and confidence, the LLM verdict and the location; tapping it opens the ad.
- **The gate**: a model can only push once it has **≥ 15 priced samples** and its category
  (GPUs, office) has **≥ 7 days** of data. Until then, deals that would have been pushed are
  stored as `would_alert`, so alerts turn themselves on as data accumulates. `hunt gate` shows
  which models are ready. `HUNTER_ALERTS_ENABLED` is the global switch on top.
- At most 5 pushes a day (`HUNTER_ALERT_MAX_PER_DAY`). The same ad is never pushed twice, and
  reposts (same model + same seller or title, price within 5 %, within 14 days) are skipped.
- **Quiet hours** 23:00-07:00 Europe/Prague: deals wait in `queued` and go out in the morning if
  the ad still exists (for up to 24 h). A score ≥ 0.3 goes through at night anyway.
- **👍/👎 without a public endpoint**: each push carries two ntfy `http` action buttons. They
  POST `{"ad": <id>, "vote": "up"|"down"}` to the separate topic `deals-feedback` on the public
  ntfy URL, using a token of user `deals-feedback-writer`, which can only write that topic. The
  worker polls the topic as `deal-hunter` (read access) every cycle and stores votes in
  `feedback` (deduplicated by ntfy message id). These are the labels for v2.
- **Weekly digest**, Sunday 18:00 Prague: new ads, deals flagged / pushed / dropped, the busiest
  models, median moves of ≥ 5 % against last week's snapshot, your votes. While no model is
  ready it says "still learning" with the data-days so far.
- `hunt notify-test` sends one test push (its 👍/👎 store votes for ad 0). `hunt deals` lists
  candidates and what happened to them.

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
                                          hunt normalize ───────────┤   normalized
                                                                    │
   Ollama (CT 100) ◄── candidates ── hunt work --loop ◄─────────────┘   fair_prices, deals,
                                          │        ▲                    triage, feedback
                          push (deals)    ▼        │ 👍/👎 (deals-feedback)
                                    ntfy (CT 116) ──► phone
```

- `src/deal_hunter/bazos.py`: pure parsers (category page, RSS, detail page), tested against real
  pages saved in `tests/fixtures/`. If Bazoš changes its layout, CI breaks instead of the scraper
  silently storing garbage.
- `http.py`: the polite client. `scraper.py`: scrape / backfill / recheck cycles.
- `db/`: raw psycopg 3 and numbered SQL migrations (`hunt db migrate`).
- `normalize.py` + `gpu_catalog.yaml`: the rule-based GPU normalizer; `office.py`: the office one.
- `pricing.py`: fair price v1. `triage.py`: Ollama client, prompt, tolerant parser, deal score.
  `alerts.py`: the worker cycle, gate, ntfy, feedback and digest.

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
uv run hunt normalize [--all]           # map titles to models (GPUs, office PCs/laptops)
uv run hunt review [--section pc]       # unmatched titles, to improve the catalog
uv run hunt stats
uv run hunt prices [--category office]  # fair price table
uv run hunt price --model "rtx 3080"    # one model, with its recent ads
uv run hunt work                        # one worker cycle (price, triage, score, push, feedback)
uv run hunt deals [--state would_alert] # candidates and what became of them
uv run hunt gate                        # which models may alert yet
uv run hunt triage 224777992            # ask the LLM about some ads (prints, stores nothing)
uv run hunt notify-test                 # one test push
uv run pytest                           # DB tests need DATABASE_URL (they use <db>_test)
```

## Deploy

Its own compose stack in `/opt/deal-hunter` on the homelab (CT 121): `db` (Postgres 17),
`scraper`, `rechecker` and `worker` (256 MB limit; the LLM cache lives in the `hunterdata` volume).
Publishing a GitHub Release builds the image and pushes it to GHCR. `hunter-deploy.timer` notices
the new release within 5 minutes, pulls it, migrates and restarts the services
(`deploy/deploy.sh`). Nothing on GitHub can reach into the server. Secrets live only in
`/opt/deal-hunter/.env`: the database password, `NTFY_TOKEN` (user `deal-hunter`) and
`NTFY_FEEDBACK_TOKEN` (user `deals-feedback-writer`), next to `HUNTER_ALERTS_ENABLED`. Ollama runs
on CT 100 (`onboot=1`). If it is off, candidates simply wait.

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
  models take longer to reach 15 samples.
- `gemma3:12b` is a small model. It misreads things: pickup-only taken for shipping-only, a
  shop's "pay postage up front" taken for a prepay scam. The evidence check removes the worst of
  this, but treat the verdict as a hint, not a judgement.
- Fair prices are medians of asking prices, so a market that is falling shows up late, and a
  "deal" can just be an honest seller in a falling market.
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
- [ ] **Phase 3 v2** (after 4–6 weeks of data): LightGBM on log price that must beat the median in a
      time-split evaluation.
- [x] **Phase 4: LLM triage.** Candidates only. gemma3:12b in Ollama with a JSON schema, an on-disk
      cache, evidence-checked scam signals, and a queue while Ollama is down.
- [x] **Phase 5: alerts.** ntfy pushes gated per model until there is enough data, at most 5 a day,
      quiet hours, repost dedup, 👍/👎 via a write-only feedback topic, and a Sunday digest.
- [ ] **Phase 6: UI + ROI tracking.** Deals feed, price history per model, normalizer review,
      and a flips log (bought at, sold at, profit) to show whether it actually pays.
