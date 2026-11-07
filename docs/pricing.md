# Pricing

How a fair price is estimated from asking prices, and where new prices and priors come from.

Back to the [README](../README.md).

## Fair price (v1)

Per canonical model (`rtx 3080 10gb`, `thinkpad t480`, `optiplex 7060 micro`, `hdd35 nas 8tb`, ...), from matched ads
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
auction prices for office hardware); see [New prices and priors](#new-prices-and-priors). `hunt prices` prints the table
(model, n, median, IQR, confidence, new price, prior; `--no-prior` shows the raw medians); `hunt price --model "rtx 3080"`
shows one model (prefix match, so both VRAM sizes) with its recent ads. A daily snapshot goes to
`fair_prices` for trends. **v2** is not built yet: with this little data, a regularized log-linear
model (Huber or Ridge on log price, shrunk per model, monotonic in RAM/SSD) is preferred over
LightGBM, and it has to beat the median in a time-split evaluation.

## Apple and consumer/gaming laptops

`apple.py` and `laptop_spec.py` price what office.py always excluded as `out_of_scope`: MacBooks,
iMacs, Mac minis/Studios, and consumer/gaming laptops (IdeaPad/Legion/Yoga, Pavilion/Omen/Victus,
Aspire/Nitro/Predator, Vivobook/ROG/TUF, MSI, ...). Both get `spec_adjust: true` in the category
tree, so the same RAM/SSD log-price slope `pricing.py` already fits for office hardware (one shared
slope per doubling, shrunk toward a prior, clipped to 0-35 %) applies to them too.

**Apple** keys on the chip and screen size for Apple Silicon (`macbook air m2 13`, `macbook pro m1
pro 14`, `imac m1 24`, `mac mini m2`, `mac studio m2 max`; any `Mx`, not just M1-M4, so a future chip
still matches), falling back to an Intel key (`macbook pro intel 2017 13`, or `macbook air intel 13`
with no year stated) only when that's actually safe: a year up to 2019, 2020 for a 16" Pro (the only
Intel model still sold that year), or the title itself says "Intel"/"i5"/"i7"/"i9"/"Core Duo" --
which also breaks the tie for the one year both an Intel *and* an M1 Mac were sold new (2020, 13"
Air/Pro). Without a safe year or that signal (any 2021+ year, a 24" iMac with no chip, a bare 13"
Air/Pro with neither), it's left unmatched rather than guessed: silently keying a current-generation
Apple Silicon Mac as a cheap old Intel one inflates that Intel bucket's fair price, exactly the kind
of wrong-model mistake this whole pipeline is built to avoid. A bare 'MacBook'/'iMac'/'Mac mini' with
nothing to pin a key to is unmatched too, same as a bare 'Dell OptiPlex' in office.py.

**Consumer/gaming laptops** have no stable catalog of SKUs the way ThinkPads do (far too many Lenovo
IdeaPad/Legion model numbers alone), so the pricing key is a **spec bucket** instead of a model
number -- what actually drives resale value is the CPU tier+generation and the GPU:

```
laptop i7 g12 rtx 4060   Core i7, 12th gen, RTX 4060
laptop r5 5000 igpu      Ryzen 5, 5000 series, no discrete GPU
laptop rtx 5070          GPU only -- the ad never named a CPU (common in "wow spec" gaming ads)
```

CPU bucket: `i{3,5,7,9} g{gen}` (classic Core, generation read from the model number: `1135G7` ->
11, `12700H` -> 12, `8265U` -> 8), `ultra{5,7,9} s{100,200}` (Core Ultra, by hundred-series --
Meteor/Lunar Lake vs Arrow Lake), `r{3,5,7,9} {series}` (Ryzen, by thousand-series: `5600H` -> 5000),
`rai{7,9} {series}` (Ryzen AI, by hundred-series). A bare tier with no model number at all still
gives the tier alone (`r5`), coarser. GPU bucket: the exact model name from `gpu_catalog.yaml`
(normalize.py's desktop GPU catalog, reused as-is -- no VRAM suffix, since a laptop GPU's VRAM often
differs from its desktop namesake and the catalog only knows the desktop one), plus a handful of
laptop-exclusive chips the desktop catalog has no entry for (RTX 2050/3050 Ti/4050, GTX 1650 Ti); no
discrete GPU found -> `igpu`. The CPU component is *dropped*, not placeholder-filled, when the title
names no CPU at all -- hence the GPU-only key above. These buckets were sized from the real titles
available (see the generator script's commentary; no production sample was reachable while writing
this): coarse enough that a handful of ads reach each bucket, at the cost of mixing somewhat
different laptops (a budget RTX 3050 config and a maxed-out one) under the same `rtx 3050`-style key.
Category: `herni` (a discrete GPU was found, or a gaming-line brand name is in the title: Legion,
ROG, TUF, Nitro, Predator, Omen, Victus, MSI, Aorus, Alienware, Loq, ...) or `ostatni` otherwise. A
title with none of a CPU, a discrete GPU or a gaming-line name ("Notebook Asus Vivobook 15 x507m")
is left unmatched -- conservative, same as office.py.

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

**Current new price: Zboží.cz** (`zbozi.py`, `new_price_pages.yaml`; disks too, see [Hard disks](disks.md)). Heureka blocks all bots.
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
  honest User-Agent, its own budget (`http_throttle` row 3): ~50 requests a week (34 GPU + 15 disk pages). A failed or rejected
  page is retried after a day.
- `hunt newprices [--fetch] [--all]` shows (and reads) the pages.

**Current new price: Datart.cz** (`datart.py`; a second feed, complementing Zboží).
Datart's category pages are server-rendered and list every product as schema.org JSON-LD (name,
price with VAT, URL), so nothing is curated: the product names go through the same GPU and disk
normalizers as Zboží's, and every product that yields a pricing key becomes a `new_prices` row with
`shop = 'datart'` (disks as per-class, per-capacity keys such as `hdd35 nas 8tb`; 2.5" drives, SSDs
and cards the catalog doesn't price are skipped). Datart's robots.txt (re-checked 2026-10-12)
disallows the cart, the customer area, `/vyhledavani` (search), `/filter/` URLs (bar a few sort orders)
and `?tab=`, `?listType=`, `?orderBY`, `?do=` and similar parameters; plain category pages and
`?page=N` are allowed, and `datart.allowed()` accepts nothing else. Two categories
(`/graficke-karty.html`, `/interni-pevne-disky.html`), up to `HUNTER_DATART_PAGES` (4) pages each,
read weekly with Zboží's budget (`HUNTER_NEW_PRICE_DAYS`, ≥ 6 s apart, `http_throttle` row 3): at most
8 requests a week. The pages are in popularity order, so a model that is no longer sold new (most
RTX 30 cards) is usually not on them and keeps its Zboží page. A failed read is retried after a day.

**A model's new price is the lowest fresh row of any shop** (`repository.latest_new_offers`: each
page's or product's latest read within 30 days, errors ignored; Zboží wins a tie, since it compares
several shops). The shop is kept: pushes, `hunt prices` and the web UI say `(Zboží, 3 shops)` or
`(Datart, 1 shop)`. For a pooled disk class (Kč/TB) the shop shown is Datart only if every key of the
class came from Datart, else Zboží. A Datart price is a single shop's shelf price, so it is rarely
below Zboží's lowest of several; it mostly fills models Zboží has no curated page for (current cards:
RTX 50, RX 9000) and the capacities missing from `new_price_pages.yaml`. `hunt newprices` lists a
Datart row only where it is the model's new price.

Limits: the list goes stale. Cards go out of stock or end of life (RTX 30/40 cards have few or no
offers left), shops' cheapest SKU changes, and "lowest offer" can be a marketplace seller. The price is
"what the cheapest of these 1-3 SKUs costs", not the cheapest card of that chip in the country. The
page list needs a look every few months (`hunt newprices` shows pages without offers).

**Sanity check.** A used fair price (or the raw median, before a prior) at **95 % or more of the new
price** (Zboží or Datart if known, else the launch MSRP in Kč) means the estimate or the mapping is off (a
mis-normalized title, a scalper market, a new card listed as used); candidates of that model are
dropped and `hunt prices` flags it.

**Prior for thin data** (`pricing.apply_priors`). A model with fewer than `HUNTER_ALERT_MIN_SAMPLES`
(15) samples is shrunk toward a prior in log space, the prior counting as k samples:

```
w    = n / (n + k)
fair = exp(w · ln(median) + (1 − w) · ln(prior))

GPUs:   prior = new price (Zboží or Datart, the lower) × r,  k = 5
        r = median of (median / new) over models with ≥ 15 samples and a Zboží or Datart price
            (at least 3 such models; else 0.75), clipped to [0.35, 0.9]
Disks:  the same, with r from disk keys only (else 0.6)
Office: prior = median per-unit final price of ended state auction lots × 1.5,  k = lots (max 3)
```

Example: 5 ads with a median of 5 400 Kč and a Zboží price of 10 000 Kč give a prior of 7 500 and
w = 0.5, so fair = √(5 400 × 7 500) ≈ 6 364 Kč. The prior is ignored (and noted) when it is more than
2× away from the median either way: then the new-price page or the market is odd. With a prior, the
**gate counts n + k** (`n_eff`): 10 ads plus a new price pass the 15-sample gate. The **confidence
level is not raised** by a prior: it stays what the ads alone support. A model whose ads are already
at ≥ 95 % of new (the sanity flag) gets **no prior**, so the flag isn't hidden by the shrinkage. The gate's ≥ 7 days of data rule is unchanged. At 15 samples and above the median stands
alone (a step of at most ~10 % at n = 15, because of the 2× rule).

**State auction prices as an anchor for office hardware.** Ended NMS lots that hold a single,
verified, working-or-untested model with a consistent unit count give per-unit final prices
(e.g. 14 × Latitude 5511 for 63 000 Kč = 4 500 Kč a unit). They are wholesale, as-is prices,
so they are a **lower** anchor: × 1.5 (cleaned, tested, sold one by one) as the prior above, with
each lot counting as one sample (max 3), and with 2 or more lots the per-unit median is also a floor
for the fair price: a Bazoš asking price below what the state auction got for an untested unit is not
a fair price for a working one. The 1.5 is a guess; `hunt auctions list` shows the real ratio as data
comes in.
