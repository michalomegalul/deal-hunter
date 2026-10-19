# deal-hunter

A personal tool that finds underpriced second-hand computer hardware in Czechia and sends the good
finds to my phone.

It watches [Bazoš.cz](https://pc.bazos.cz/) and [Aukro](https://aukro.cz/) for graphics cards,
ex-office PCs and business laptops, and big hard disks for a home server. For every model it works out
what it normally sells for. When an ad is clearly cheaper, a local LLM reads it to catch broken items,
scams and misleading prices, and only the deals that survive are pushed to the phone via
[ntfy](https://ntfy.sh/), with the photo, the price against the usual one, the distance from home and
👍/👎 buttons. A 👍 brings the full details and a ready-to-send message to the seller.

It also:

- values the state's IT auctions on [nms.gov.cz](https://nms.gov.cz/) (lots like "14× Dell Latitude
  5511") and says what the most worth bidding is after refurbishing and reselling the units;
- keeps standing searches ("DDR4 2×16GB under 1 500 Kč") that I can add from the phone;
- logs what I bought and sold, and checks whether the alerts actually lead to good buys;
- shows it all on a small read-only web page on the home network.

It is polite to the sites it reads: only what robots.txt allows, a few seconds between requests, and
no getting around bot protection.

## How it works

1. **Scrape** new ads every 10 minutes and notice when they disappear (a fast disappearance is the
   best available hint that something sold).
2. **Normalize** titles to models (`rtx 3060 12gb`, `thinkpad t480`, `hdd35 nas`) with conservative
   rules; anything unclear is left out.
3. **Price** each model from the last 60 days of ads: a robust median, adjusted for RAM/SSD on office
   hardware, with new-shop prices as a fallback for rare models.
4. **Triage** ads that look at least 20 % cheap with `gemma3:12b` in Ollama. Every claim it makes has to
   be backed by a quote from the ad.
5. **Score and push** the deals: discount × confidence × penalties, a daily cap, quiet hours, and a
   pickup radius unless the seller ships.

## Running it

```bash
uv sync
docker compose up -d          # local Postgres
cp .env.example .env
uv run hunt db migrate
uv run hunt scrape --once     # one scrape cycle
uv run hunt work              # one worker cycle: price, triage, score, push
uv run hunt --help            # everything else
uv run pytest                 # database tests need DATABASE_URL
```

In production it runs as a Docker Compose stack on a homelab server and deploys itself from GitHub
releases; see [Deploy and operations](docs/operations.md).

## More detail

| | |
|---|---|
| [Pricing](docs/pricing.md) | Fair prices, new prices and priors |
| [LLM triage and scoring](docs/scoring.md) | The prompt, evidence checks and the deal score |
| [Alerts](docs/alerts.md) | Pushes, follow-ups, the flips log, distance and shipping |
| [Hard disks](docs/disks.md) | The disk normalizer, Kč/TB pricing, SMART checks |
| [State auctions](docs/auctions.md) | Reading and valuing nms.gov.cz lots |
| [Watchlists](docs/watchlists.md) | Standing searches managed from the phone |
| [Sources](docs/sources.md) | Which sites are read, how often, and which were ruled out |
| [Web UI](docs/web.md) | The LAN website and `/status` |
| [Architecture](docs/architecture.md) | Data flow and a map of the modules |
| [Deploy and operations](docs/operations.md) | The server, backups and the watchdog |
| [Limitations](docs/limitations.md) | What the numbers can and can't tell you |

## Credits

Postal code coordinates come from [GeoNames](https://www.geonames.org/) postal code data for the Czech
Republic, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), compacted into
`src/deal_hunter/cz_postcodes.csv`.
