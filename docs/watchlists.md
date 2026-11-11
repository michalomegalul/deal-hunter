# Watchlists

Back to the [README](../README.md).

Standing "I want X" searches, outside the GPU/office/disk pipeline above: not priced, not scored,
just matched and pushed. Two independent pieces (`watchlist.py`).

- **Matching**, inside the worker cycle (`hunt work`, every `HUNTER_WORK_MINUTES`): every active
  watch is checked against its own recent, not-yet-checked listings (`HUNTER_WATCH_SCAN_HOURS`,
  default 48 h) from *any* source and section -- not only the five sections below. A cheap
  keyword/regex prefilter on title + description runs first (`watchlist.prefilter`: include groups
  OR'd, terms within a group AND'd, an exclude list that drops a match outright); only prefilter
  hits go to the LLM. Exclude terms match from the start of a word, and "SO-DIMM"/"non-ECC" count as
  one word, so `sodimm` catches "SO-DIMM" and `ecc` doesn't drop a "non-ECC" kit. Before the LLM, the
  watch's own limits apply (`watchlist.within_limits`): over `max_price`, beyond `radius_km`, or
  outside its `categories` (when set) is recorded in `watch_hits` as no match with the reason
  (`result.skipped`) and never asked about. The listing's own price and distance are used; an unknown
  price ("dohodou") or distance passes. The LLM check (the same local Ollama model as `triage.py`, JSON-schema output,
  temperature 0, disk cache) answers yes/no plus `{product_type, brand, model, condition, price_czk}`
  with an exact quote from the ad as evidence -- a quote that isn't actually in the ad is treated as
  no match, same defence as triage's scam-signal quotes. It also answers `accessory_only` (the ad
  sells only a mount, cover, cable, PoE injector or power supply for such an item); that is no match
  either. Asked inside the `matches` rule alone, gemma3 said yes to those for a broad watch like
  "UniFi gear"; the separate question it answers well. Every (watch, listing) pair that passes the
  prefilter is recorded in `watch_hits` exactly once (dedups the LLM call); a miss there is never
  stored, so it is simply looked at again next cycle until it ages out of the scan window. Listings
  are read newest first (at most 300 per watch and cycle), so those re-read misses never keep a new ad
  waiting. A match
  is pushed on the `deals` topic (title, price, distance, the LLM's evidence quote, the watch's tag,
  the source if not Bazoš) respecting quiet hours and its own daily cap (`HUNTER_WATCH_MAX_PER_DAY`,
  separate from `HUNTER_ALERT_MAX_PER_DAY`). The limits are checked again just before the push (the
  price may have gone up), and a match is never pushed once its ad is gone or it is more than 24 h
  old. A muted watch (see below) is still scanned, matched and recorded, just left pending instead of
  pushed, so it is pushed once the mute lapses rather than lost. The push itself carries three ntfy
  action buttons -- "Stop", 👍, 👎 -- that POST a plain-text `stop <id>` / `vote <watch_id>-<listing_id>
  up|down` command straight back to the watch topic (`watchlist.watch_hit_actions`), the same
  HTTP-action mechanism as the deals feedback buttons (`alerts.Ntfy.feedback_action`), just posting a
  command instead of a JSON vote. They need their own write-capable token (`NTFY_WATCH_WRITE_TOKEN`,
  separate from the listener's read-only `NTFY_WATCH_TOKEN`); without it, no buttons are attached. A
  vote is a last-write-wins flag on the hit (`watch_hits.vote`), no history kept.
- **Management**, its own small ntfy listener and compose service (`hunt watch listen --loop`, the
  same live-stream pattern as `feedback.py`): reads commands from its own topic
  (`HUNTER_WATCH_TOPIC`, default `watch`, read with `NTFY_WATCH_TOKEN`) and replies on the `deals`
  topic. Only messages on that topic are read at all, and only text that parses as one of the
  commands below does anything; anything else gets a "didn't understand" reply. Tolerant of Czech or
  English, case-insensitive:

  ```
  watch ddr4 2x16gb -notebook -vadna max 1500 km 50 #homelab   (also: sleduj, add, přidej)
  list                                        (also: seznam, ls)
  stop 3                                      (also: zastav, smazat, delete, remove)
  start 3                                     (also: obnov, spustit)
  mute ddr4 7d                                (or: mute 3 7d -- also: ztlum, ztlumit)
  vote 3-501 up                               (or: vote 3-501 down -- normally tapped from a push)
  help                                        (also: nápověda, ?)
  ```

  A `-word` token anywhere in an `add` query is an exclude term, folded into the same
  `keywords.exclude` list the seeded watches already use (`watchlist.prefilter`) -- so
  `watch rtx 3060 -notebook -vadna` behaves exactly like a hand-written exclude list. `mute` takes
  either a watch id or free words matched against existing queries, plus a duration ending in `d`
  (days), `h` (hours) or `w` (weeks); a muted watch keeps matching and recording hits, it just isn't
  pushed until the mute lapses. `start` re-activates a watch `stop` had deactivated.

  The same CRUD is available from the CLI: `hunt watch add/list/stop/start/mute`, plus `hunt watch seed` for
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
