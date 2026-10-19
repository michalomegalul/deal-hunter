# Honest limitations

Back to the [README](../README.md).

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
  models take longer to reach 15 samples. Disk history starts with v0.7.0 (plus its 15-page backfill).
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
- **The priors are assumptions**: the 0.75 used/new fallback, the 1.5 auction-to-retail uplift and the
  prior weights (5 samples; one per auction lot) are guesses until there is data to fit them.
- **The seller message is a template.** It only names the item and asks whether it is still available;
  the item name comes from the words the normalizer recognised, so an odd title gives a short name.
- **Distances are straight lines** from the PSČ's point (the median of its GeoNames places); by road it
  is 20-30 % more. GeoNames has some misplaced codes: three Prague ones are corrected, others elsewhere
  may be off. An ad without a known PSČ or town counts as out of range unless it ships.
- **Shipping is read from the ad**: a regex for clear offers ("pošlu", "Zásilkovna", "PPL", "dobírkou",
  ...) and refusals ("neposílám", "pouze osobní odběr"), the LLM's reading only when the ad is vague, and
  then only if the ad uses a shipping word at all.
- **Disks are thin data.** In the 300 ads of a first fetch (~6 days) only ~12 were 3.5" drives of 8 TB or
  more, now pooled per class rather than per class/capacity key, so a class's fair price (≥ 4 ads) and the
  alert gate (15) still take a couple of weeks; new prices from Zboží help as priors once a class has a
  few ads. The SMR table and the SAS/SATA suffix table are curated by hand from data sheets as remembered
  (see `storage_catalog.yaml`); the less certain suffixes are marked `uncertain` and don't call a drive
  SAS. A title without a part number, or an uncertain suffix, leaves the interface unknown (shown as
  "⚠ check SATA vs SAS"; SMR still shows "CMR?").
- Older and workstation cards (GTX 9xx and earlier, RX 400/500, Quadro, ...) are recognised but
  excluded as `out_of_scope`, so the first backfill (which reaches deep into cheap old stock) shows
  a high exclusion rate.
