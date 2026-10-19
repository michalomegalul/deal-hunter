# Architecture

Back to the [README](../README.md).

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
