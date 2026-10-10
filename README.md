# deal-hunter

Watches second-hand ads on [Bazoš.cz](https://pc.bazos.cz/) (graphics cards, plus ex-office mini PCs
and business laptops), works out what each model normally goes for, has a local LLM read the ads that look cheap, and
pushes only the genuinely good deals to a phone (ntfy). It also watches the state's IT auctions on
[nms.gov.cz](https://nms.gov.cz/) (lots like "14× Dell Latitude 5511") and works out the highest bid that
still pays after refurbishing and reselling the units.

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
  (ThinkCentre M, OptiPlex, EliteDesk/ProDesk, Fujitsu Esprimo), HP's business all-in-ones
  (EliteOne/ProOne) and business laptops (ThinkPad T/X/L/P/E, Latitude
  5xxx/7xxx, EliteBook/ProBook) map to a model key such as `thinkpad t480`, `thinkpad t14 gen 3`,
  `optiplex 7060 micro` or `elitedesk 800 g4 mini`; RAM, SSD size and CPU are read from the title
  when present. Everything else in those sections (gaming PCs, MacBooks, consumer laptops,
  workstations) is `out_of_scope`, so `hunt review` only shows business titles it couldn't pin down.
- **Worker** (every 5 min, `hunt work --loop`): fair prices per model, candidate deals, LLM triage
  of the candidates, the deal score, ntfy pushes (once the gate opens) and a weekly digest.
  Details below.
- **Auctions** (inside the worker, low cadence): IT lots from the ÚZSVM state property auctions,
  the LLM's reading of each lot, a valuation with a max sensible bid, pushes before the end, and the
  final prices of ended lots as real sale prices. See "Auctions" below.
- **Feedback** (`hunt feedback --loop`, its own small service): listens to the 👍/👎 topic live. A 👍
  sends a detailed follow-up within seconds (photo, prices, the LLM's reading, the description and a
  drafted Czech message to the seller); "Bought ✅" starts the flips log. See "👍 follow-ups".
- **New prices** (inside the worker, weekly): the lowest current offer on Zboží.cz for ~20 GPU models,
  plus launch MSRPs in the GPU catalog. Shown in pushes, used as a sanity check and as a prior for
  models with few ads. See "New prices".

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

Thin models are then shrunk toward a prior (new price × typical used/new ratio for GPUs, state
auction prices for office hardware); see "New prices and priors". `hunt prices` prints the table
(model, n, median, IQR, confidence, new price, prior; `--no-prior` shows the raw medians); `hunt price --model "rtx 3080"`
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
- **The photo**: a push attaches the ad's first photo (ntfy `attach` with the Bazoš image URL), so
  the phone shows it in the notification. Auction pushes attach the lot's first photo, if it has one.
- **👍/👎 without a public endpoint**: each push carries two ntfy `http` action buttons. They
  POST `{"ad": <id>, "vote": "up"|"down"}` (auction lots: `{"lot": "<id>", ...}`, so a lot id can
  never be mistaken for an ad id) to the separate topic `deals-feedback` on the public ntfy URL,
  using a token of user `deals-feedback-writer`, which can only write that topic. The feedback
  service holds the topic's stream open as `deal-hunter` (read access) and stores votes in
  `feedback` (deduplicated by ntfy message id) as they arrive. These are the labels for v2. A 👍
  also asks for the detailed follow-up (next section).
- **Weekly digest**, Sunday 18:00 Prague: new ads, deals flagged / pushed / dropped, the busiest
  models, median moves of ≥ 5 % against last week's snapshot, your votes. While no model is
  ready it says "still learning" with the data-days so far.
- `hunt notify-test` sends one test push (its 👍/👎 store votes for ad 0). `hunt deals` lists
  candidates and what happened to them.

## 👍 follow-ups

A 👍 means "interested, tell me more". The feedback service (`feedback.py`, `hunt feedback --loop`)
reads `deals-feedback` through ntfy's JSON stream (`GET /deals-feedback/json?since=<last id>`),
so a tap is handled within seconds instead of at the next 5-minute cycle. It remembers the id of the
last message it handled (kv `feedback_last_id`): after a restart it asks for everything since then
(ntfy keeps messages ~12 h), and reconnects with backoff (2 s doubling to 2 min) when the
connection drops.

For a 👍 on a **Bazoš ad** it re-reads the ad page once (the shared Bazoš budget), then pushes:

```
👍 Gigabyte RTX 3060 Gaming OC 12GB                                  [the ad's photo]
💰 4 500 Kč · fair ~6 000 Kč (−25%), n=9 medium · new ~7 490 Kč (Zboží, 3 shops)
🏷 rtx 3060 12gb
📍 Brno 602 00 · posted 09.10. · seen 5 h ago · 👁 87 · seller Petr
🤖 used · defects: noisy fan · receipt/warranty ✓ · flags: none
„Receipt, pickup only, worth a look.“

📝 the description (up to ~1 500 characters, trimmed to fit one ntfy message)

✉️ Message (offer 4 100 Kč):
Dobrý den, mám zájem o Gigabyte RTX 3060 Gaming OC. Je ještě k dispozici? Mohu nabídnout
4 100 Kč při osobním odběru v Brně. S pozdravem
                                                    [Open ad]  [Bought ✅]
```

- The **offer** is ours, not the LLM's: 7 % below asking (`HUNTER_OFFER_DISCOUNT`, clamped to
  5-10 %), rounded down to 50 / 100 / 500 / 1 000 Kč depending on the price, and always 3-10 % below.
- The **message** is drafted by the same local LLM (with its own JSON schema and the disk cache),
  told to be polite, short, formal ("vykání"), to propose exactly our offer and to say nothing else
  about the item. The draft is then checked like the triage's scam quotes: our offer must be in it,
  every other number must appear in the ad, pickup may only be mentioned if the ad offers personal
  pickup, shipping only if the ad mentions shipping, and no links, e-mails or phone numbers. A draft
  that fails is replaced by a fixed template with the same offer ("Dobrý den, mám zájem o …. Je ještě
  k dispozici? Mohu nabídnout … Kč[, s osobním převzetím]. Děkuji za odpověď. S pozdravem").
- If the ad was never triaged (a 👍 on a test push), it is triaged now.
- **Bought ✅** POSTs `{"ad": <id>, "vote": "bought"}`; the service records it in `flips`
  (listing, bought_at, buy_price = the asking price, sold_at/sold_price empty) for the Phase 6 ROI log.
  Edit `buy_price` after haggling.
- If the ad is **gone**, the follow-up says so instead.
- One follow-up per ad (table `followups`): a second 👍 or a replayed message sends nothing. A 👍
  more than 6 hours old (a replay after a long outage) is stored as a vote but gets no follow-up.

For a 👍 on an **auction lot** the follow-up has the item list with each line's fair price and
resale value, the valuation breakdown (revenue, parts, labour, transport/fees), the max bid, the profit
at the next bid, the end time, inspection and pickup, or says the lot has ended.

`hunt notify-test --ad <id>` pushes a real ad as a deal (titled "TEST · …", with its photo); its 👍
carries `"test": true`, so the follow-up is titled "TEST · 👍 …" and nothing is stored as a label or
flip. `hunt feedback` (without `--loop`) handles what is cached on the topic once and exits.

## New prices and priors

Bazoš medians are asking prices from thin data. Two kinds of new price help: as context in pushes, as
a sanity check and as a prior for models with few ads. The LLM never sees the web (prompt injection
from ads, made-up numbers); these are fetched and parsed by code.

**Launch MSRP** (`gpu_catalog.yaml`, `msrp_usd` and `launch`): the US launch MSRP of most GPUs since
the GTX 10 series, from the manufacturers' announcements; models without a well-known MSRP (GTX 1630,
RTX 3060 8 GB, RX 6700, RX 9070 GRE, Arc A310, ...) have none. A dict means one MSRP per VRAM size, and
a pricing key without the VRAM ('rtx 3060') gets none. It is converted for display and the sanity check
only: `USD × HUNTER_USD_CZK (23.5) × 1.21` (US MSRPs exclude sales tax, Czech prices include 21 % VAT).
It is a launch price, not a current one: never a prior.

**Current new price: Zboží.cz** (`zbozi.py`, `new_price_pages.yaml`). Heureka blocks all bots.
Zboží's robots.txt (re-checked 2026-10-10) disallows search (`/hledani/`, `/hledej/`, `?q=`),
multi-choice filters (any URL with a comma), `sId=`/`pId=`/`catOrder=`, the shop click-throughs
(`/action/`, `/clickthru`) and most multi-parameter URLs; product pages (`/vyrobek/<slug>/`) are
allowed and listed in its sitemap. The graphics card category is rendered in the browser (the server
sends no products), so there is no robots-compliant way to look a model up. Instead
**`new_price_pages.yaml` lists 1-3 product pages per model**, for ~20 popular models, picked by hand
once (from the sitemap and from the "similar products" on product pages) among cards that had offers.

- A product page is server-rendered; the parser reads its `__NEXT_DATA__` JSON: name, category,
  lowest offer, number of shops. A page only counts if it is in the graphics card category **and**
  its name, run through our own GPU normalizer, gives the same pricing key (a wrong or recycled URL
  can't price another card).
- A model's new price = the lowest offer across its pages read in the last 30 days, stored in
  `new_prices` (model, url, product, min_price, shops, fetched_at, error).
- Weekly per page (`HUNTER_NEW_PRICE_DAYS`), at most 10 pages per worker cycle, ≥ 6 s apart, the
  honest User-Agent, its own budget (`http_throttle` row 3): ~35 requests a week. A failed or rejected
  page is retried after a day.
- `hunt newprices [--fetch] [--all]` shows (and reads) the pages.

Limits: the list goes stale. Cards go out of stock or end of life (RTX 30/40 cards have few or no
offers left), shops' cheapest SKU changes, and "lowest offer" can be a marketplace seller. The price is
"what the cheapest of these 1-3 SKUs costs", not the cheapest card of that chip in the country. The
page list needs a look every few months (`hunt newprices` shows pages without offers).

**Sanity check.** A used fair price (or the raw median, before a prior) at **95 % or more of the new
price** (Zboží if known, else the launch MSRP in Kč) means the estimate or the mapping is off (a
mis-normalized title, a scalper market, a new card listed as used); candidates of that model are
dropped and `hunt prices` flags it.

**Prior for thin data** (`pricing.apply_priors`). A model with fewer than `HUNTER_ALERT_MIN_SAMPLES`
(15) samples is shrunk toward a prior in log space, the prior counting as k samples:

```
w    = n / (n + k)
fair = exp(w · ln(median) + (1 − w) · ln(prior))

GPUs:   prior = Zboží new price × r,  k = 5
        r = median of (median / new) over models with ≥ 15 samples and a Zboží price
            (at least 3 such models; else 0.6), clipped to [0.35, 0.9]
Office: prior = median per-unit final price of ended state auction lots × 1.5,  k = lots (max 3)
```

Example: 5 ads with a median of 5 400 Kč and a Zboží price of 10 000 Kč give a prior of 6 000 and
w = 0.5, so fair = √(5 400 × 6 000) ≈ 5 692 Kč. The prior is ignored (and noted) when it is more than
2× away from the median either way: then the new-price page or the market is odd. With a prior, the
**gate and the confidence level count n + k** (`n_eff`): 10 ads plus a new price pass the 15-sample
gate. The gate's ≥ 7 days of data rule is unchanged. At 15 samples and above the median stands
alone (a step of at most ~10 % at n = 15, because of the 2× rule).

**State auction prices as an anchor for office hardware.** Ended NMS lots that hold a single,
verified, working-or-untested model with a consistent unit count give per-unit final prices
(e.g. 14 × Latitude 5511 for 63 000 Kč = 4 500 Kč a unit). They are wholesale, as-is prices,
so they are a **lower** anchor: × 1.5 (cleaned, tested, sold one by one) as the prior above, with
each lot counting as one sample (max 3), and with 2 or more lots the per-unit median is also a floor
for the fair price: a Bazoš asking price below what the state auction got for an untested unit is not
a fair price for a working one. The 1.5 is a guess; `hunt auctions list` shows the real ratio as data
comes in.

## Auctions (nms.gov.cz)

Government offices sell old equipment through ÚZSVM's electronic auctions on
[nms.gov.cz](https://nms.gov.cz/) (the same system as nabidkamajetkustatu.gov.cz). The IT category
("Výpočetní technika vč. příslušenství") has had ~15 running lots at a time: single PCs, but also lots
like "Soubor 14 ks HW vybavení - Notebooky" (14 Dell Latitude 5511) with the item list in a PDF.
Ended auctions show the **final price**, so unlike Bazoš this is what hardware really sold for.

**How it is read** (`nms.py`, `auction_cycle.py`). No robots.txt is served (checked 2026-10-09), and we
stay slow anyway: at least 6 s between requests (`HUNTER_AUCTION_MIN_DELAY`, never below 5), an
honest User-Agent, and a request budget of its own (`http_throttle` row 2).

- The list page is rendered in the browser from a JSON API. Every 2.5 h (`HUNTER_AUCTION_LIST_HOURS`)
  we make the same call the page makes, `POST /api/Property/AuctionList`, for the IT category only:
  one request for all running lots, one for the newest 50 ended ones (their final prices).
- A **detail page** (`/Home/AuctionDetail/<id>`, server-rendered) for each new lot: description,
  inspection, seller's address, attachments, current price, bids and the auction window. Attachment
  URLs are stored; the text of at most two PDFs that look like an item list ("seznam", "příloha",
  model names; never the notice or the contract) is read and compacted (serial numbers out,
  identical rows counted: "18× PC přenosný ACER Extensa 2540").
- After a lot's end time the detail page is read **once more** to record the final price. Lots seen
  only after they ended get one detail read if they sold (for the price dataset), none if unsold.
- Before a reminder push the detail page is read again, so the push quotes the current price.

A typical day is ~10 list calls, one detail page per new lot (plus its PDFs) and one after each end:
a few dozen requests.

**LLM extraction** (`auctions.py`). The same Ollama client, disk cache and tolerant parser as the
Bazoš triage, with its own JSON schema. Per lot the model returns the item groups
(`model, kind, quantity, condition, missing parts, RAM/storage/CPU, defects`) and notes (VAT
deductible, pickup, inspection, buyer's premium/fees). Every claim is checked against the lot's text:

- each item must quote the text it came from, and the quote must be in it; otherwise it is
  *unverified* and not valued;
- a quantity above one must be written as a count ("22 ks", "soubor 22", "18× …", "(14)"): the
  model once read the inventory number in "Prodej PC Dell Optiplex 3050 - 2863" as 2 863 PCs;
- "missing storage/charger/battery" only counts if the text talks about disks/chargers/batteries,
  and chargers and batteries only for laptops; "VAT deductible" only if the text mentions DPH; a fee
  only with a quote that names a fee.

Models are mapped to pricing keys by the office and GPU normalizers (`latitude 5511`, `optiplex 3050`,
`esprimo d756`, `eliteone 800 g3`). Consumer models (Acer Extensa) get no key and no value.

**Valuation** (per lot, money in Kč):

```
fair_i     = Bazoš fair price of model i at the unit's spec (+ a 256 GB SSD if it comes without storage)
resale_i   = fair_i × 0.9 (resale factor: medians are asking prices, buyers haggle)
             × 0.25 if broken, × 0.75 if "some units faulty"
revenue    = Σ units_i × resale_i × (1 − 1 % × weeks_to_sell / 2)
weeks_to_sell = priced units / 5 per week (what Bazoš absorbs from one seller)
parts      = Σ units_i × refurb parts per unit (table below)
hours      = priced units × 40 min + 2 h pickup;   labour = hours × 200 Kč/h
fixed      = 500 Kč transport + stated fixed fees;   premium = stated buyer's premium %

profit(bid)   = revenue − parts − labour − fixed − bid × (1 + premium)
max bid       = (revenue × (1 − 30 %) − parts − labour − fixed) / (1 + premium)
profit/hour   = profit(next bid) / hours
```

The next bid is the current price if nobody has bid yet (the starting price is a valid first bid),
else the current price plus the minimum increment. The **max bid** is where the expected profit is still
30 % of the resale revenue (`HUNTER_AUCTION_TARGET_MARGIN`), labour paid at the hourly rate.

| Refurb part (per unit)         | Kč  | Notes                                                        |
|--------------------------------|-----|--------------------------------------------------------------|
| SSD 256 GB (sold without disk) | 500 | the unit is then priced at 256 GB                            |
| Battery (dead / 0 %)           | 600 | aftermarket                                                  |
| Charger (missing)              | 250 | generic 65 W                                                 |
| RAM (missing)                  | 300 | one 8 GB DDR4 module                                         |
| Windows licence                | 0   | see below                                                    |
| Other missing part             | 200 |                                                              |
| Labour                         | 40 min × 200 Kč/h | clean, test, install, photograph, list, answer, hand over |

The Windows line is 0 because business machines carry an OEM key in the BIOS and reactivate on a
clean install. Honest catch: that only holds for the edition on the COA/BIOS key, a refurbisher
licence is a grey area for a private seller, and a machine that shipped with a volume licence has
nothing to reactivate. Selling without an OS or with Linux is the clean option.

**Confidence.** The lot's confidence is the lowest fair-price level (pricing.py) that carries at least a
fifth of the revenue. It drops to *low* when under 70 % of the units are priced, or when the item counts
don't add up to what the lot says ("22 ks"). With no fair price at all there is no valuation.

**Pushes** (same `deals` topic and notifier as Bazoš deals, own style):

```
🏛 Aukce: 22× HP ProBook 645 G4
ends Thu 14:00, now 3 300 Kč (2 bids)
est. resale 22× ~1 800 Kč; max bid ~23 200 Kč, profit ~31 496 Kč, profit/h ~1 890 Kč
medium confidence; ~4.4 weeks to sell 22 units
📍 Praha
```

(That is 22 units at a 2 000 Kč fair price: revenue 22 × 1 800 minus 2.2 % for selling over 4.4 weeks
= 38 729, labour 16.7 h = 3 333, transport 500, so the max bid is 38 729 × 0.7 − 3 833 ≈ 23 200.)
Tapping it opens the lot. A lot **qualifies** when the max bid is at least the next bid, the profit at
the next bid is at least 1 500 Kč (`HUNTER_AUCTION_MIN_PROFIT`), and its confidence is medium or high.
A **low-confidence** lot only qualifies with a huge margin: max bid ≥ 2× the next bid *and* profit
≥ 3× the minimum. Pushes: when a qualifying lot is first found, then ~24 h and ~2 h before the end if it
still qualifies (re-valued at the fresh price). One of each per lot; a lot first found 10 h before the
end skips the 24 h one. Quiet hours hold them like Bazoš pushes. They have their **own allowance** of 3 a
day (`HUNTER_AUCTION_MAX_PER_DAY`) on top of the 5 for Bazoš deals, so neither starves the other.
`HUNTER_ALERTS_ENABLED` is the switch for both; `HUNTER_AUCTIONS=false` turns the whole step off.

**Sold-price dataset.** Ended lots keep `final_price`. `hunt auctions list` shows running lots with their
valuation and sold lots with final price per unit next to our Bazoš fair price for the main model:
what a used business laptop fetches at a state auction vs what Bazoš sellers ask.

**Other auction sites, checked 2026-10-09 and skipped:**

- **verejnedrazby.cz** (Gaute): no robots.txt (404), but there is no IT category (categories are cars,
  real estate, machines, "movitý majetek", ...), and none of its ~20 movable-property lots was IT; the
  rest of its ~160 lots are real estate and vehicles. Finding the rare IT lot would mean reading every
  listing page and detail page all the time. Not worth the requests for now.
- **drazby.cz**: robots.txt allows pages but disallows `*.pdf`, and the dražební vyhláška (the
  document with what is actually sold) is a PDF. Its catalogue had 6 running auctions, all cars, and its
  categories are real estate, cars, machines and "other". No IT lots.

## Being polite

- Only URLs that [robots.txt](https://pc.bazos.cz/robots.txt) allows: the category pages
  (`/graficka/`, `/graficka/20/`, ...), the RSS feed and `/inzerat/<id>/<slug>.php`. No search,
  price filter or ordering parameters (`hledat=`, `cenaod=`, `order=`, ...).
- At least 3 seconds between any two requests, shared across the scraper and the rechecker
  (a row lock in Postgres), an honest User-Agent that links to this repo, timeouts, and
  exponential backoff on 429/5xx (honouring `Retry-After`).
- A normal cycle is 7 requests (3 RSS feeds + 4 category pages) plus one per new ad worth keeping,
  typically under 10 in total, every 10 minutes. `HUNTER_SECTIONS` turns sections off.
- nms.gov.cz has its own, slower budget (≥ 6 s apart, a few dozen requests a day); see "Auctions".
- Zboží.cz: only product pages robots.txt allows, ≥ 6 s apart, ~60 a week; see "New prices".

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
          └──── draft ── hunt feedback --loop ─────────────────────►  feedback, followups, flips
   nms.gov.cz ── lists, details, PDFs ──► auction step (in the worker) ──► auction_lots,
                                          auction_extractions, auction_alerts
   zbozi.cz ── product pages (weekly) ──► new-price step (in the worker) ──► new_prices
```

- `src/deal_hunter/bazos.py`: pure parsers (category page, RSS, detail page), tested against real
  pages saved in `tests/fixtures/`. If Bazoš changes its layout, CI breaks instead of the scraper
  silently storing garbage.
- `http.py`: the polite client. `scraper.py`: scrape / backfill / recheck cycles.
- `db/`: raw psycopg 3 and numbered SQL migrations (`hunt db migrate`).
- `normalize.py` + `gpu_catalog.yaml`: the rule-based GPU normalizer; `office.py`: the office one.
- `pricing.py`: fair price v1. `triage.py`: Ollama client, prompt, tolerant parser, deal score.
  `alerts.py`: the worker cycle, gate, ntfy, feedback and digest.
- `nms.py`: nms.gov.cz parsers (list API, detail page, PDF item lists), tested against saved responses.
  `auctions.py`: LLM extraction and valuation. `auction_cycle.py`: the auction step of the worker.
- `feedback.py`: the feedback service (stream, votes, follow-ups). `followup.py`: the follow-up text,
  the offer and the Czech draft with its evidence check. `zbozi.py` + `new_price_pages.yaml`: new prices.

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
uv run hunt notify-test --ad 224876851  # a real ad as a TEST deal push (photo, 👍 -> TEST follow-up)
uv run hunt feedback [--loop]           # handle taps on deals-feedback (once / the live service)
uv run hunt newprices [--fetch [--all]] # Zboží.cz pages and their lowest offers
uv run hunt prices --no-prior           # raw medians, without the new-price/auction priors
uv run hunt auctions scrape --dry-run   # running IT lots on nms.gov.cz (nothing saved)
uv run hunt auctions scrape --once      # lists + due detail pages, LLM extraction, valuation (no pushes)
uv run hunt auctions list               # running lots with valuations, sold lots: final price/unit vs fair
uv run hunt auctions value 62388        # one lot: items, mapping, valuation breakdown
uv run pytest                           # DB tests need DATABASE_URL (they use <db>_test)
```

## Deploy

Its own compose stack in `/opt/deal-hunter` on the homelab (CT 121): `db` (Postgres 17),
`scraper`, `rechecker`, `worker` (256 MB limit; the LLM cache lives in the `hunterdata` volume;
the auction and new-price steps run inside it) and `feedback` (160 MB limit, the live 👍/👎 listener,
sharing the LLM cache).
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
- **The priors are assumptions**: the 0.6 used/new fallback, the 1.5 auction-to-retail uplift and the
  prior weights (5 samples; one per auction lot) are guesses until there is data to fit them.
- **The seller message is a draft.** It is checked for invented numbers and claims, but read it before
  sending; it is formal and generic on purpose.
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
