# Watchlists

Back to the [README](../README.md).

Standing "I want X" searches, outside the GPU/office/disk pipeline above: not priced, not scored,
just matched and pushed. Two independent pieces (`watchlist.py`).

- **Matching**, inside the worker cycle (`hunt work`, every `HUNTER_WORK_MINUTES`): every active
  watch is checked against its own recent, not-yet-checked listings (`HUNTER_WATCH_SCAN_HOURS`,
  default 48 h) from *any* source and section -- not only the five sections below. A cheap
  keyword/regex prefilter on title + description runs first (`watchlist.prefilter`: include groups
  OR'd, terms within a group AND'd, an exclude list that drops a match outright); only prefilter
  hits go to the LLM. The LLM check (the same local Ollama model as `triage.py`, JSON-schema output,
  temperature 0, disk cache) answers yes/no plus `{product_type, brand, model, condition, price_czk}`
  with an exact quote from the ad as evidence -- a quote that isn't actually in the ad is treated as
  no match, same defence as triage's scam-signal quotes. Every (watch, listing) pair that passes the
  prefilter is recorded in `watch_hits` exactly once (dedups the LLM call); a miss there is never
  stored, so it is simply looked at again next cycle until it ages out of the scan window. A match
  is pushed on the `deals` topic (title, price, distance, the LLM's evidence quote, the watch's tag,
  the source if not Bazoš) respecting quiet hours and its own daily cap (`HUNTER_WATCH_MAX_PER_DAY`,
  separate from `HUNTER_ALERT_MAX_PER_DAY`).
- **Management**, its own small ntfy listener and compose service (`hunt watch listen --loop`, the
  same live-stream pattern as `feedback.py`): reads commands from its own topic
  (`HUNTER_WATCH_TOPIC`, default `watch`, read with `NTFY_WATCH_TOKEN`) and replies on the `deals`
  topic. Only messages on that topic are read at all, and only text that parses as one of the
  commands below does anything; anything else gets a "didn't understand" reply. Tolerant of Czech or
  English, case-insensitive:

  ```
  watch ddr4 2x16gb max 1500 km 50 #homelab   (also: sleduj, add, přidej)
  list                                        (also: seznam, ls)
  stop 3                                      (also: zastav, smazat, delete, remove)
  help                                        (also: nápověda, ?)
  ```

  The same CRUD is available from the CLI: `hunt watch add/list/stop`, plus `hunt watch seed` for
  the defaults below (idempotent -- matched by `query`, safe to run again after an upgrade).

**Default watches** (`watchlist.SEEDS`; owner lives in Prague, so radii are modest): DDR4 UDIMM
2x16GB or 2x32GB non-ECC 3200 (excludes ECC/RDIMM/SODIMM/DDR3/DDR5), an LSI HBA (9207-8i / 9300-8i /
9211-8i, IT mode), a 4U rack case fitting mATX with many 3.5" bays, a 19" rack (rozvaděč) of at
least 12U, and UniFi gear (`#homelab` tag on all five).

**Five extra Bazoš sections** feed the prefilter with ads the main pipeline never sees: RAM
(`/pamet/`, RSS `cat=11`), network gear (`/sit/`, `cat=14`; `/wifi/`, `cat=28`), cases/PSUs
(`/case/`, `cat=16`) and motherboards (`/motherboard/`, `cat=19`) -- plain category/RSS URLs,
allowed by robots.txt the same way the other four sections are (checked 2026-10-11; only
`hledat`/`cenaod`/`cenado`-style search URLs are disallowed, and this app never builds those). They
are read every 45 min (`bazos.Section.cadence_min`, gated in `scraper.scrape_once` via a `kv` entry
per section), not the main 10-minute loop, to keep the request budget low, and marked
`excluded_reason='watch_only'` by the normalizer so they never show up as unmatched GPUs in `hunt
stats`/`hunt review`. They're part of the default `HUNTER_SECTIONS`; drop them from the env var to
scrape only the original four sections (and disable watchlists in practice). Matching Aukro
categories were not added: there is no plain `GET` category page to check against robots.txt the
way Bazoš has, and verifying its JSON API's category slugs for RAM/network/case/motherboard needs
more than a quick look, so this is left for later rather than guessed at and silently wrong.
