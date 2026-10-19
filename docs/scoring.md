# LLM triage and the deal score

Which ads go to the local LLM, what it is asked, how its answers are checked, and the score.

Back to the [README](../README.md).

## LLM triage

A **candidate** is an active ad first seen in the last 3 days, priced at least 20 % below its fair
price (`HUNTER_CANDIDATE_DISCOUNT`), whose model has **medium or high** confidence. Prices under
10 % of fair are treated as placeholders. Only candidates go to the LLM: `gemma3:12b` in Ollama on
CT 100, about 6-10 s per ad. The prompt is in English and the ad is Czech. The client sends a JSON
schema as Ollama's `format`, `temperature 0`, `think: false`, and caches answers on disk by
sha256(model + prompt) in the worker's data volume (`triage.py`, the pattern from bot-arena). The
answer gives condition (new / like_new / used / worn / broken), for_parts, defects,
mining_suspected, warranty_or_receipt, urgent_sale, scam_signals, is_bundle, is_wanted_ad, ships (the
ad offers to send the item; see [Distance from home](alerts.md#distance-from-home)), specs (RAM/SSD/CPU) and a one-line verdict. Hard
disks get extra questions about the drive's health (see [Hard disks](disks.md)). `PROMPT_VERSION` 3.

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
| disk: reallocated / pending sectors, or SAS       | drop    |
| disk: over 50 000 power-on hours                  | × 0.6   |
| disk: nothing about SMART in the ad               | × 0.85  |
| disk: SMR model or line                           | × 0.7   |

A push needs `score ≥ 0.12` (`HUNTER_ALERT_MIN_SCORE`). Example: 30 % under fair at confidence 0.6
with a receipt gives 0.3 × 0.6 × 1.1 = 0.198. Priority follows the discount: ≥ 45 % → 5 (urgent),
≥ 33 % → 4, else 3.
