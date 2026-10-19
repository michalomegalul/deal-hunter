# Alerts, follow-ups and flips

Pushes to the phone, the pickup radius, the detailed follow-up after a 👍, and the flips log.

Back to the [README](../README.md).

## Alerts (ntfy)

- Pushes go to topic `deals` on `ntfy.dobsinsky.dev`. The worker publishes over the LAN
  (`http://192.168.4.36`) as user `deal-hunter`. The message holds the title, price vs fair
  (−X %), n and confidence, the LLM verdict and the place with its distance ("📍 Ostrava · 280 km
  (ships)"); tapping it opens the ad. Disk pushes add a line with the capacity, Kč/TB vs new, the
  SATA/CMR badges and the SMART summary.
- **The gate**: a model can only push once it has **≥ 15 priced samples** and its category
  (GPUs, office, disks) has **≥ 7 days** of data. Until then, deals that would have been pushed are
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
  models, median moves of ≥ 5 % against last week's snapshot, deals kept back as out of pickup range
  ("Out of pickup range (would-alert): 3, e.g. Ostrava 280 km"), your votes. While no model is
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
📍 Brno · 182 km (ships) · posted 09.10. · seen 5 h ago · 👁 87 · seller Petr
🤖 used · defects: noisy fan · receipt/warranty ✓ · flags: none
„Receipt, pickup only, worth a look.“

📝 the description (up to ~1 500 characters, trimmed to fit one ntfy message)

💬 If negotiating: ~4 100 Kč

✉️ Message:
Dobrý den, zajímám se o grafickou kartu Gigabyte RTX 3060 12GB. Mohl byste mi prosím sdělit, zda je
stále k dispozici?
                                                    [Open ad]  [Bought ✅]

✉️ Message for the seller                     (a second, separate push: only the text)
Dobrý den, zajímám se o grafickou kartu Gigabyte RTX 3060 12GB. Mohl byste mi prosím sdělit, zda je
stále k dispozici?
```

- The message also arrives **as its own push** with nothing else in it, so a long-press in the ntfy app
  copies exactly the text to paste into Bazoš; tapping that push opens the ad.

- The **message** is a fixed template, not an LLM draft (it has to sound like a person and
  must never say anything the ad doesn't): "Dobrý den, zajímám se o {noun} {item}. Mohl byste mi prosím
  sdělit, zda je stále k dispozici?" No offer, no "Děkuji" line. The noun is the category's accusative:
  "grafickou kartu", "notebook", "počítač", "all-in-one počítač", "disk" ("disky" for several),
  "externí disk", "SSD". The item is the brand and model cleaned from the title, in the seller's own
  spelling ("Prodám ZOTAC GAMING GeForce RTX 3070 Twin Edge OC 8GB" → "ZOTAC RTX 3070",
  "3× Toshiba MG09 18TB enterprise HDD" → "Toshiba MG09 18TB"): only words the normalizer recognised,
  so "PRODÁM", prices, emoji and sales talk never get in. An ad without a model says "... z vašeho
  inzerátu".
- The **offer** ("💬 If negotiating") is shown next to the message, not in it: 7 % below asking
  (`HUNTER_OFFER_DISCOUNT`, clamped to 5-10 %), rounded down to 50 / 100 / 500 / 1 000 Kč depending on
  the price, and always 3-10 % below. It is for the conversation once the seller answers.
- Disk follow-ups add the disk line (capacity, Kč/TB vs new, SATA/CMR badges, SMART).
- If the ad was never triaged (a 👍 on a test push), it is triaged now.
- **Bought ✅** POSTs `{"ad": <id>, "vote": "bought"}`; the service records it in `flips`
  (listing, bought_at, buy_price = the asking price, category, sold_at/sold_price empty). After haggling,
  fix the price with `hunt flip buy-price <ad> <Kč>`; when it sells, `hunt flip sell` (see "Flips log
  and evaluation").
- If the ad is **gone**, the follow-up says so instead.
- One follow-up per ad (table `followups`): a second 👍 or a replayed message sends nothing. A 👍
  more than 6 hours old (a replay after a long outage) is stored as a vote but gets no follow-up.

For a 👍 on an **auction lot** the follow-up has the item list with each line's fair price and
resale value, the valuation breakdown (revenue, parts, labour, transport/fees), the max bid, the profit
at the next bid, the end time, inspection and pickup, or says the lot has ended.

`hunt notify-test --ad <id>` pushes a real ad as a deal (titled "TEST · …", with its photo); its 👍
carries `"test": true`, so the follow-up is titled "TEST · 👍 …" and nothing is stored as a label or
flip. `hunt feedback` (without `--loop`) handles what is cached on the topic once and exits.

## Flips log and evaluation

The `flips` table is the record of what you bought and sold. A row starts from **Bought ✅** or from
`hunt flip add`; the rest is filled in from the CLI. `buy_price` starts as the asking price at the time
of the tap and is meant to be corrected after haggling.

```bash
hunt flip list [--open | --sold]                 # buy, sell, fees, profit, days held, totals
hunt flip add --ad 224876851 --price 4000 [--at 2026-10-01] [--notes "..."]   # bought outside the 👍 flow
hunt flip buy-price 224876851 3800               # the haggled price
hunt flip fees 224876851 150                     # postage, cleaning, parts
hunt flip sell 224876851 5200 [--fees 150] [--at 2026-10-12] [--notes "..."]
```

- Profit is `sold_price − buy_price − fees` (missing fees count as 0). It is empty until the flip is
  sold and has both prices.
- `hunt flip list` ends with the money invested in open flips (buy price plus fees), the realised
  profit of the sold ones, and ROI = realised profit / (buy price + fees) of those same flips.
- `hunt flip add` takes the title, model and category from the ad when we have it; the ad may be
  unknown. A manual flip gets a `manual:<uuid>` source id.
- An ad id that is not in the log gives a one-line error and a non-zero exit. A repeated `sell`
  overwrites the earlier one, so a typo can be fixed.

`hunt eval [--days 90]` measures the alert pipeline over the deals flagged in the last N days, as plain
text tables, for all categories together and for each category:

- deals by state (`alerted`, `would_alert`, `queued`, `capped`, `low_score`, `dropped`, other);
- deals by score band (below 0.12, 0.12-0.2, 0.2-0.3, 0.3 and up, and unscored ones), with the
  number of 👍 and 👎 among the alerted ads and the 👍 rate;
- the share of ads that disappeared within 24 h and within 72 h of `first_seen`, for `alerted`,
  `would_alert` and `low_score` ads. This is the proxy for "was it really a good deal": if `low_score`
  ads vanish as fast as alerted ones, `HUNTER_ALERT_MIN_SCORE` is too strict. Ads younger than the
  window that are still up are left out of that share;
- flips bought in the period, how many sold, realised profit and ROI per category.

Votes on auction lots and test pushes are not counted (test pushes are never stored as votes; a
`listing_id` of 0 from older data is ignored too). `gone_at` is when a recheck noticed the ad was
gone: within about 2 hours for deal candidates younger than 72 h (the fast recheck, see
[Sources](sources.md)), up to ~12 hours for everything else. An ad also vanishes when the seller gives up, so treat the
shares as a comparison between states, not as absolute numbers. The same tables are on the `/flips` page.

## Distance from home

Home is Prague (`HUNTER_HOME_PSC`, else `HUNTER_HOME_LAT`/`HUNTER_HOME_LON`, default the centre at
50.0755, 14.4378). `geo.py` turns an ad's PSČ into a point with a bundled table of the ~2 700 Czech
postal codes (`cz_postcodes.csv`, built from the [GeoNames](https://www.geonames.org/) postal code dump,
CC BY 4.0); without a PSČ it tries the town name ("Praha 4", "Ústí nad Labem", the town part of
"Brno-Trnitá"), and a name that several far-apart places share ("Lhota") gives nothing. The
straight-line distance is stored per listing (`distance_km`, `distance_src`) by the worker and recomputed
when the home or the ad's place changes.

The rule, applied to deals that are otherwise ready to push:

- **within the pickup radius** (`HUNTER_PICKUP_KM`, default 40 km; per category with
  `HUNTER_PICKUP_KM_HDD`, `_GPU`, `_OFFICE_PC`, `_OFFICE_LAPTOP` or `_OFFICE`; `HDD` defaults to
  300 km on its own - disks ship easily - without needing the env var set): as before;
- **outside it, or the distance is unknown**: only if the ad **ships** and the deal still scores
  after postage: the score is recomputed with `HUNTER_SHIPPING_CZK` (150 Kč, split over the drives of a
  multi-disk ad) added to the price, and must stay ≥ `HUNTER_ALERT_MIN_SCORE`;
- otherwise the deal is kept as `would_alert` with the reason ("out of range: Ostrava 275 km > 40 km,
  no shipping" / "... ships, but score 0.090 after 150 Kč shipping"), so the radius can be tuned later.

**Does it ship?** A regex on the title and description first: clear offers ("pošlu", "zašlu",
"Zásilkovna", "Balíkovna", "PPL", "DPD", "dobírka", "poštou", "zaslání"; but not "pošlu fotky / SMART
výpis") and clear refusals ("neposílám", "pouze osobní odběr"). Only when the ad says neither does the
LLM's `ships` answer count, and only if the ad uses some word about sending. The decision is stored on
the deal (`deals.ships`). `HUNTER_TRAVEL_CZK_PER_KM` (default 0) adds "~X Kč there and back" next to the
distance; it is display only. `HUNTER_PICKUP_KM=0` turns the rule off.
