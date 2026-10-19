# Sources and politeness

Which sites are read, how often, and the rules that keep it polite.

Back to the [README](../README.md).

## Being polite

- Only URLs that [robots.txt](https://pc.bazos.cz/robots.txt) allows: the category pages
  (`/graficka/`, `/graficka/20/`, ...), the RSS feed and `/inzerat/<id>/<slug>.php`. No search,
  price filter or ordering parameters (`hledat=`, `cenaod=`, `order=`, ...).
- At least 3 seconds between any two requests, shared across the scraper and the rechecker
  (a row lock in Postgres), an honest User-Agent that links to this repo, timeouts, and
  exponential backoff on 429/5xx (honouring `Retry-After`).
- A normal 10-minute cycle reads the four main sections: 9 requests (4 RSS feeds + 5 category
  pages, two of them for graphics cards) plus one per new ad worth keeping, typically under 12. The
  five watch-only sections (RAM, two network sections, cases, motherboards) are read at most every
  45 min, an RSS feed and one category page each plus a detail page per new ad. `HUNTER_SECTIONS`
  turns sections off.
- nms.gov.cz has its own, slower budget (≥ 6 s apart, a few dozen requests a day); see [Auctions](auctions.md).
- Zboží.cz: only product pages robots.txt allows, ≥ 6 s apart, ~50 a week; see [New prices](pricing.md#new-prices-and-priors).
- Datart.cz: two category pages (a few pages each) a week, the same budget; see [New prices](pricing.md#new-prices-and-priors).
- Mironet.cz (only when enabled): outlet-filtered category listings, ≥ 6 s apart, own budget
  (`http_throttle` row 4), one pass of ≤ 11 categories × ≤ 2 pages every 6 h; see [More sources](#more-sources).

## More sources

Production reads Bazoš and Aukro (`HUNTER_SOURCES=bazos,aukro`); Mironet is built but opt-in and not
enabled yet, Facebook only runs when its cookies are configured. The pipeline (normalizer → pricing →
triage → alerts) is source-agnostic: every listing carries a `source` column, and every source maps its
own categories onto the same four sections (`graficka`/`pc`/`notebook`/`hdd`) before anything else sees
it. `HUNTER_SOURCES` turns sources on, comma-separated (default `bazos`). The `/status` page shows
each source's state.

- **Sbazar.cz: blocked.** Its robots.txt is `User-agent: * / Disallow: /` for a generic bot, so there
  is no Sbazar code (an untested parser was deleted); `/status` lists it as "blocked (robots.txt)".
- **Aukro.cz: working.** Its own category pages call a JSON API
  (`POST /backend-web/api/offers/searchItemsCommon`) that robots.txt doesn't restrict; `aukro.py`
  calls it directly instead of scraping HTML. One category slug per section (its `-aukce`/`-bazar`
  URL suffixes turned out to be cosmetic -- the API mixes auctions and fixed-price offers in both
  regardless); each item's own `auction`/`buyNowActive` flags decide `price_kind`: a plain or
  buy-now-active item is `fixed`; a bidding-only item is `auction` with the current bid kept for
  display only (`price_samples` already excludes anything that isn't `fixed`, the same as
  `negotiable`/`in_text`). An auction's `endingTime` is stored in `listings.auction_end_at`.
  `HUNTER_AUKRO_PAGES` (default 1, 60 items/page) controls how many pages per category per cycle.
- **Facebook Marketplace: owner's own session, ban risk accepted.** No public API, so `facebook.py`
  drives headless Chromium (Playwright) with the owner's logged-in cookies -- a Cookie-Editor JSON
  export at the path in `FB_COOKIES_FILE` (root-only on the server, **never committed**), read and
  converted to Playwright's cookie format. It opens a marketplace search per configured query
  (`HUNTER_FB_QUERIES`), captures the page's own GraphQL responses and parses listings out of them,
  never the DOM. Low and slow: a few seconds between queries, and the whole cycle itself runs every
  20-30 minutes with jitter (`hunt scrape-fb --loop`), never on the main 10-minute loop. A login wall
  or checkpoint stops the cycle and sends one ntfy alert ("FB cookies expired"), deduplicated so it
  only fires once per expiry. Disabled entirely -- `run_once` returns immediately, no browser, no
  request -- whenever `FB_COOKIES_FILE` is unset. Playwright is the `fb` extra
  (`uv sync --extra fb && playwright install chromium`), not a dependency of the main install, and
  ships as a separate, much bigger image (`Dockerfile`'s `fb` stage, `:latest-fb`) and compose
  service (`fb-scraper`, its own profile: `docker compose --profile fb up -d fb-scraper`), so running
  it is an explicit choice, not something a plain deploy turns on. The GraphQL parser is tested
  against a hand-made, clearly-marked *synthetic* fixture -- Facebook's schema is unpublished and
  changes often, so this is a best-effort shape, not a captured real response.

- **Mironet.cz outlet: working, opt-in, not enabled in production yet** (`HUNTER_SOURCES=bazos,aukro,mironet`; `shops.py`). Mironet sells
  unboxed returns ("rozbaleno") and used stock ("bazar") with a warranty. Its outlet landing pages
  (`/vyprodej/?v=rozbaleno`, `?v=bazar`) only link to its normal category listings filtered on the
  "Stav" parameter (`/graficke-karty+fc14402/?EXPT=&pn10658[]=Rozbaleno`), which are server-rendered
  HTML: a product id, the name, a spec line, the price with VAT and an outlet badge. Mironet's
  robots.txt (re-checked 2026-10-12) disallows `/ajax/`, `/user/`, `/order/`, `/podpora/`, `/sr2/`, a few
  `/productdetail/` actions and any `sort=` URL; category listings, the filter and `PgID` paging are
  not named, and `shops.allowed()` accepts nothing else (a test checks every URL the code builds
  against a saved copy of the real robots.txt). It lists AI crawlers by name with the same rules and
  blocks a few others outright; deal-hunter is none of them.
  Eleven category/outlet pairs are read: unboxed graphics cards, notebooks, computers, disks and SSDs,
  RAM, motherboards, cases, power supplies and switches, plus bazar computers and power supplies
  (there is no bazar graphics card or disk right now, and an empty outlet is a valid answer). The
  first four map onto the canonical sections, so an unboxed GPU, a 22 TB SAS disk or a ThinkPad gets
  a pricing key and can become a deal candidate (a push says "via Mironet (outlet, with warranty)");
  the rest map onto the watch-only sections (RAM, network, cases, motherboards), so watchlists can
  match them and nothing else looks at them. A page whose products carry no badge of the expected
  outlet (the filter was ignored) is an error, never a source of listings.
  **Their prices are shelf prices, not used-market ones:** shop listings are stored with
  `price_kind = 'retail'`, and `price_samples` (the input to every fair price) reads only
  `price_kind = 'fixed'`, so an unboxed card at close to new price can't inflate a model's used fair
  price. Candidates (`active_priced`) and watchlists do read `retail`. Read at most every
  `HUNTER_SHOP_HOURS` (6; the same kv gate as the slow Bazoš sections), up to `HUNTER_SHOP_PAGES` (2)
  pages per category, ≥ `HUNTER_SHOP_MIN_DELAY` (6.5, never below 6) seconds apart on their own
  budget (`http_throttle` row 4). There is no per-item page to re-check: an item missing from a
  completely read category is marked gone (`gone_reason = 'not_listed'`), and shop items are never
  handed to the Bazoš rechecker. It stays off by default so a plain deploy behaves as before; turning
  it on adds ~11 requests every 6 hours.
- **Not used: Alza.cz, TSBohemia.cz, CZC.cz, Datart's "last pieces" and Sbazar/Heureka.** Checked
  2026-10-12: Alza.cz and TSBohemia.cz answer every request, robots.txt included, with a Cloudflare bot
  challenge (HTTP 403), and CZC.cz now redirects to Allegro, which does the same. This project never
  bypasses bot protection (the rule that also ruled out Heureka and Sbazar), so there is no Alza
  comparison; Datart and Zboží are the new-price sources. Datart's "last pieces"
  (`/vyprodej-poslednich-kusu.html`) is allowed and parses, but it is 98 pages of new stock of every
  kind in no useful order, so it is not read; Datart has no outlet category page we could find (its
  bazar items have product pages only).

Every source keeps the site's own id in `listings.external_id`; `listings.id` (the app's internal
key, used by every join) is that id directly for Bazoš (unchanged, so existing rows didn't move) and
a deterministic hash of `(source, external_id)` for everything else, so two sources reusing the same
native id can never collide (`sources.make_id`). `(source, external_id)` has its own unique index --
that is what actually enforces "ids unique per source". Pricing pools samples across every source
that feeds a model with used prices (more data, same fair price); alerts always show which source a push came from
(a "via Aukro" / "via FB Marketplace" / "via Mironet (outlet, with warranty)" line) unless it's
Bazoš, to keep today's pushes unchanged. Pricing pools samples of used goods only: a shop's prices
are not samples (see Mironet above).
