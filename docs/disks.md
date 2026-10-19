# Hard disks

Back to the [README](../README.md).

The main interest at the moment: more storage for the home server. It has an Intel SATA controller with
6 ports (2 used by 2 TB Barracudas) and **no SAS HBA**, so the target is **3.5" SATA CMR drives of 8 TB
and more**, NAS or enterprise lines.

**Scraping.** The Bazoš section "Hard disky, SSD" (`/hdd/`, RSS `cat=5`) under the same polite rules:
one RSS feed and one category page per cycle, a detail page only for new drives that could fit (3.5",
desktop externals, form unknown; no SSDs or 2.5" drives). `hunt backfill --section hdd` seeds history
once (15 pages: ~300 ads, ~70 detail pages).

**Normalizer** (`storage.py`, `storage_catalog.yaml`, ~300 hand-checked real titles in
`tests/fixtures/disk_titles.yaml`). From the title, and the description when there is one:

| feature        | how                                                                                   |
|----------------|---------------------------------------------------------------------------------------|
| kind           | `hdd_35`, `hdd_25`, `hdd` (form unknown), `ssd_sata`, `ssd_nvme`, `ssd`: words (3,5", "do notebooku", NVMe, M.2 SATA), part numbers, lines |
| capacity_tb    | "8TB", "8 TB", "8000 GB", "1,92TB"; or the part number (ST8000…, WD80…, MG09ACA18TE) |
| interface      | `sata` / `sas` / unknown: `sas` only when **certain** - the word "SAS"/"12Gb/s" in the text, the HGST/WD Ultrastar "S" marker (ALS6, ...), a Seagate NM suffix the catalog marks `certain` (ST16000NM002G, ...), or Toshiba MGxx**S**CA; a suffix remembered but not checked against a data sheet (`uncertain` in the catalog) or any other case is left unknown, not guessed - shown as "⚠ check SATA vs SAS" |
| line, class    | WD Red / Red Plus / Red Pro, Purple, Gold, Ultrastar, Seagate IronWolf / Pro, Exos, Constellation, SkyHawk, Barracuda, Toshiba N300 / MG / X300 / P300, Synology HAT; class `nas`, `enterprise`, `surveillance`, `desktop`; shuckable desktop externals (WD Elements / My Book, Seagate Expansion / Backup Plus) apart |
| smr            | a table of models (WD40EFAX, ST8000DM004, … SMR; WD40EFRX, ST2000DM001, … CMR) and line defaults (WD Red 2-6 TB and Barracuda 2-8 TB SMR, the NAS/enterprise lines CMR) |
| quantity       | "4× …", "3 ks", "dva disky"; with `price_per` = `unit` ("7 000 Kč/ks", "cena za kus"), `lot` ("za vše", "cena za oba") or `unknown` |

Pricing keys are **class, pooled across capacities** for a 3.5" HDD with a known class: `hdd35 nas`,
`hdd35 enterprise`, `hdd35 desktop`, `hdd35 surveillance`, `external` (desktop externals, also 3.5") -
a single capacity rarely gets the 4 ads a fair price needs, and these are priced in **Kč/TB** instead
of Kč (see "Fair price" below). SAS drives keep their own capacity-specific key, `hdd35 enterprise sas
4tb` (a market of their own, and rarer now that the interface call is stricter) - as do a 3.5" drive
with no known line (`hdd35 8tb`), 2.5" (`hdd25 1tb`, `external25 2tb`) and SSDs (`ssd nvme 1tb`). The
part number is kept as a feature, not in the key. Excluded: wanted ads, broken / for parts ("vadné
sektory"; not "0 vadných sektorů"), frames / caddies / boxes / cables / adapters, NAS boxes and multi-bay
enclosures, mixed lots (several sizes or brands), laptop drives under 1 TB, IDE, external SSDs, memory
cards, tapes. In the first fetch of 300 ads: 212 matched, 73 excluded, 15 unmatched; 11 were 3.5" drives
of 8 TB+ that aren't SAS.

**Fair price**, as for everything else (`pricing.py`), from the **price of one drive**: a lot price is
split over its drives, and a multi-disk ad that doesn't say whether the price is per piece is left out
of pricing and candidates. For a pooled class the estimate itself runs on Kč/TB (each sample's price
divided by its own capacity), the same robust median/IQR/confidence as any other model, just flat per
class - checked against the Zboží new-price numbers below for a capacity trend and found too noisy and
non-monotonic to support one. A listing's fair price is that Kč/TB median × its own capacity. Shown as
Kč/TB for pooled classes, Kč and Kč/TB for the rest (`hunt prices --category hdd`). **New prices** from
curated Zboží.cz pages for ~10 popular drives (Exos X18 12/16/18 TB, Exos X20, IronWolf 8/12 TB,
IronWolf Pro 8/16 TB, Red Plus 8/12 TB, Toshiba MG09 18 TB, MG10 20 TB), checked against the internal
hard disk category and the storage normalizer; for a pooled class the per-capacity Zboží offers are
turned into one Kč/TB figure (the median across that class's capacities), used as a prior with the disk
used/new ratio (0.6 until there is data) and shown as "new ~972 Kč/TB" in pushes. On 2026-10-10 new
drives cost ~950-1 150 Kč/TB, with no consistent size trend (16 TB enterprise was the cheapest per TB
of the four capacities checked, not the dearest).

**Triage.** Disk candidates get extra questions (`triage.DISK_PROMPT`, its own schema): power-on hours,
reallocated or pending sectors (a number, "some" or 0), health %, a SMART screenshot mentioned, tested,
warranty until, from a NAS or server. Every answer needs the ad behind it: the hours and health must be
numbers in the ad (with Czech thousands joined), sector claims need words about sectors, the flags need
words about SMART / testing / warranty / NAS. Penalties on top of the usual ones: sectors > 0 or SAS →
drop; > 50 000 h → × 0.6; nothing about SMART → × 0.85; SMR → × 0.7.

**Server fit** (`storage.server_fit`, before the LLM): a 3.5" (or desktop external) SATA drive of at least
`HUNTER_HDD_MIN_TB` (8) TB that isn't host-managed SMR. Disk candidates that don't fit (SAS, 2.5", SSD,
too small, form unknown) skip the LLM and are kept as `would_alert` with the reason ("SAS drive: not
usable on this server (SATA controller, no SAS HBA)", "server fit: 6 TB < 8 TB"). CMR is preferred
through the SMR penalty, not required. Disks ship easily: 300 km by default (`HUNTER_PICKUP_KM_HDD`,
overridable).

```
3× Toshiba MG09 18TB enterprise HDD, záruka do 4. 2. 2027              [the ad's photo]
3× 8 900 Kč/ks vs ~12 000 Kč fair (−26%), n=9, medium confidence
hdd35 enterprise · new ~973 Kč/TB (Zboží, 2 keys, 3 shops)
💾 3× 18 TB · 494 Kč/TB (new ~973 Kč/TB) · SATA ✓ · CMR ✓ · SMART: 12 400 h, 0 reallocated/pending
🤖 Three healthy enterprise drives with warranty, worth a look.
📍 Praha 4 · 2 km
```
