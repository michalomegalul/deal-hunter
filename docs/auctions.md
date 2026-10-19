# State auctions

Back to the [README](../README.md).

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
