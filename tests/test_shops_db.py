"""Shop outlets (Mironet) and the Datart new-price feed against a real (throwaway) database.

Pages are the real ones saved on 2026-10-12 (tests/fixtures/mironet_*.html, datart_*.html), served
through httpx's MockTransport; the few hand-made inputs are marked in their tests.
"""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

import httpx
import psycopg

from deal_hunter import alerts, config, datart, normalize, pricing, scraper, shops, sources, watchlist
from deal_hunter.alerts import PRAGUE
from deal_hunter.bazos import Listing
from deal_hunter.cli import prices as cli_prices
from deal_hunter.cli import scrape as cli_scrape
from deal_hunter.db import repository as repo
from deal_hunter.geo import DistanceRules
from deal_hunter.http import PoliteClient
from tests.conftest import fixture_text
from tests.test_triage import FakeLLM
from tests.test_watchlist_db import FakeNtfy, make_ntfy

GRAFICKA, _, _, _, HDD = (shops.CATEGORIES[i] for i in range(5))
GPU, DISK = datart.CATEGORIES


class Site:
    """{url: (status, text)}; anything else is a 404. Remembers what was asked for."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        status, text = self.pages.get(str(request.url), (404, ""))
        return httpx.Response(status, text=text)


def client(site) -> PoliteClient:
    return PoliteClient(
        "test", min_delay=0, transport=httpx.MockTransport(site), sleep=lambda s: None, retries=1
    )


def ok(name_or_text, real=True):
    return 200, fixture_text(name_or_text) if real else name_or_text


# --- Datart: the second new-price feed ---------------------------------------------------------------


def datart_site(**over):
    pages = {
        datart.category_url(GPU, 1): ok("datart_graficke_karty.html"),
        datart.category_url(GPU, 2): ok("<html>no products</html>", real=False),  # past the last page
        datart.category_url(DISK, 1): ok("datart_interni_disky.html"),
        datart.category_url(DISK, 2): ok("<html>no products</html>", real=False),
    }
    pages.update(over)
    return Site(pages)


def test_datart_feed_is_stored_per_product_and_read_weekly(conn):
    site = datart_site()
    stats = datart.fetch_due(client(site), conn, 7, 4)
    # 4 pages read (each category stops at its empty second page); 3 cards + 4 disks stored
    assert (stats.fetched, stats.priced, stats.rejected, stats.errors) == (4, 7, 3, 0)
    assert site.urls == [datart.category_url(c, p) for c in (GPU, DISK) for p in (1, 2)]
    offers = repo.latest_new_offers(conn)
    assert offers["rtx 5080"] == (43990, 1, "datart")
    assert offers["hdd35 nas 8tb"] == (9499, 1, "datart")  # the cheaper of two 8 TB NAS disks
    rows = repo.new_price_rows(conn)
    assert {r["shop"] for r in rows} == {"datart"} and len(rows) == 7
    # fresh for a week; then due again
    assert datart.fetch_due(client(datart_site()), conn, 7, 4).fetched == 0
    week = datetime.now(UTC) + timedelta(days=8)
    assert datart.fetch_due(client(datart_site()), conn, 7, 4, week).fetched == 4
    # 0 days = always due (`hunt newprices --all`)
    assert datart.fetch_due(client(datart_site()), conn, 0, 4).fetched == 4


def test_datart_failed_read_waits_a_day_and_keeps_the_last_good_prices(conn):
    datart.fetch_due(client(datart_site()), conn, 7, 4)
    week = datetime.now(UTC) + timedelta(days=8)
    broken = datart_site(**{datart.category_url(GPU, 1): (403, "blocked")})
    stats = datart.fetch_due(client(broken), conn, 7, 4, week)
    assert stats.errors == 1 and stats.fetched == 3  # the cards failed, the disks were still read
    assert repo.latest_new_offers(conn)["rtx 5080"][0] == 43990  # the last good read is still there
    # a failure is not retried for 24 h (the success time was not renewed, so the feed stays due after)
    assert datart.fetch_due(client(broken), conn, 7, 4, week + timedelta(hours=2)).fetched == 0
    assert datart.fetch_due(client(broken), conn, 7, 4, week + timedelta(hours=25)).errors == 1


def test_datart_network_error_and_empty_first_page_are_errors(conn):
    def down(request):
        raise httpx.ConnectError("no route")

    stats = datart.fetch_due(
        PoliteClient("t", min_delay=0, transport=httpx.MockTransport(down), sleep=lambda s: None, retries=1),
        conn,
        7,
        4,
    )
    assert stats.errors == 2 and stats.fetched == 0  # one per category
    empty = datart_site(**{datart.category_url(GPU, 1): ok("<html>no list</html>", real=False)})
    stats = datart.fetch_due(client(empty), conn, 0, 4)
    assert stats.errors == 1 and stats.priced == 4  # a 200 with no products on page 1 is an error


def test_datart_page_budget(conn):
    site = datart_site(**{datart.category_url(GPU, 2): ok("datart_graficke_karty.html")})
    stats = datart.fetch_due(client(site), conn, 7, 1)  # one page per category
    assert stats.fetched == 2
    assert datart.category_url(GPU, 2) not in site.urls


def test_datart_refuses_urls_robots_txt_names(conn, monkeypatch):
    monkeypatch.setattr(datart, "CATEGORIES", (datart.Category("/vyhledavani", "graficka"),))
    with pytest.raises(ValueError, match="not an allowed URL"):
        datart.fetch_due(client(Site({})), conn, 7, 1)


def zbozi_price(conn, model, price, shops_n=3, url=None):
    repo.save_new_price(
        conn, model, url or f"https://www.zbozi.cz/vyrobek/{model.replace(' ', '-')}/", "p", price, shops_n
    )


def test_new_price_is_the_lowest_across_shops(conn):
    zbozi_price(conn, "rtx 5080", 44000)
    datart.fetch_due(client(datart_site()), conn, 7, 4)
    assert repo.latest_new_offers(conn)["rtx 5080"] == (43990, 1, "datart")  # Datart is 10 Kč cheaper
    zbozi_price(conn, "rtx 5080", 43990, url="https://www.zbozi.cz/vyrobek/other/")
    assert repo.latest_new_offers(conn)["rtx 5080"] == (43990, 3, "zbozi")  # a tie shows Zboží
    zbozi_price(conn, "rtx 5080", 40000, url="https://www.zbozi.cz/vyrobek/cheaper/")
    assert repo.latest_new_offers(conn)["rtx 5080"] == (40000, 3, "zbozi")
    # a page that failed, and one read more than 30 days ago, count for nothing
    repo.save_new_price(conn, "rtx 5080", "https://www.zbozi.cz/vyrobek/gone/", None, None, None, "HTTP 500")
    conn.execute("UPDATE new_prices SET fetched_at = now() - interval '31 days' WHERE url LIKE '%cheaper%'")
    assert repo.latest_new_offers(conn)["rtx 5080"] == (43990, 3, "zbozi")
    # each page's latest read counts: Datart's product got dearer, so Zboží's 43 990 now stands alone
    repo.save_new_price(conn, "rtx 5080", datart.category_url(GPU, 1), "x", None, 1, "x", shop="datart")
    assert repo.new_price_fetched_at(conn, "datart") and "https://www.zbozi.cz/vyrobek/other/" not in (
        repo.new_price_fetched_at(conn, "datart")
    )
    assert "https://www.zbozi.cz/vyrobek/other/" in repo.new_price_fetched_at(conn)  # Zboží's own, by default


def used_gpu_ads(conn, n=20, model_title="MSI RTX 3080 10GB", base=9500):
    for i in range(n):
        repo.save_listing(
            conn,
            Listing(
                id=100 + i,
                url=f"https://pc.bazos.cz/inzerat/{100 + i}/x.php",
                title=f"{model_title} #{i}",
                price_czk=base + 50 * i,
                price_kind="fixed",
                seller=f"seller {i}",
            ),
            detail=True,
        )
    conn.execute("UPDATE listings SET first_seen = now() - interval '10 days'")


def test_datart_price_becomes_the_priors_new_price_and_is_shown_as_datart(conn):
    used_gpu_ads(conn, 5, "MSI RTX 5080 16GB", base=30000)
    normalize.run(conn)
    zbozi_price(conn, "rtx 5080", 47000)
    datart.fetch_due(client(datart_site()), conn, 7, 4)
    fp = pricing.run(conn, save=False)["rtx 5080"]
    assert (fp.new_price, fp.new_source, fp.new_detail) == (43990, "datart", "1 shop")
    assert fp.prior == round(43990 * pricing.NEW_RATIO_FALLBACK)  # priors read it like a Zboží price
    assert "new ~43 990 Kč (Datart, 1 shop)" in alerts.new_text(fp)
    zbozi_price(conn, "rtx 5080", 43000, url="https://www.zbozi.cz/vyrobek/cheaper/")
    fp = pricing.run(conn, save=False)["rtx 5080"]
    assert fp.new_source == "zbozi" and "(Zboží, 3 shops)" in alerts.new_text(fp)


def test_disk_class_new_price_names_its_shop():
    offers = {"hdd35 nas 8tb": (9000, 1), "hdd35 nas 12tb": (12000, 3), "hdd35 desktop 4tb": (4000, 1)}
    per_tb = pricing.disk_new_per_tb(offers, {"hdd35 nas 8tb": "datart", "hdd35 desktop 4tb": "datart"})
    assert per_tb["hdd35 desktop"].source == "datart"  # one key, quoted by Datart
    assert per_tb["hdd35 nas"].source == "zbozi"  # a mix of shops is shown as Zboží, with the key count
    assert per_tb["hdd35 nas"].detail == "2 keys, 4 shops"
    assert pricing.disk_new_per_tb(offers)["hdd35 nas"].source == "zbozi"  # no shop map: all Zboží


# --- shop outlets as listings ------------------------------------------------------------------------


def mironet_site(gpu=None, **over):
    pages = {
        shops.category_url(GRAFICKA): ok("mironet_rozbaleno_graficka.html") if gpu is None else gpu,
        shops.category_url(HDD): ok("mironet_rozbaleno_hdd.html"),
    }
    pages.update(over)
    return Site(pages)


def scrape_shops(conn, site, categories=(GRAFICKA,), pages=2, hours=0):
    return scraper.scrape_shops_once(client(site), conn, pages, hours, categories)


def mironet_rows(conn):
    return {
        r[0]: r[1:]
        for r in conn.execute(
            "SELECT external_id, price_kind, price_czk, section, gone_reason, gone_at IS NOT NULL "
            "FROM listings WHERE source = 'mironet'"
        )
    }


def test_outlet_items_are_saved_as_retail_listings(conn):
    stats = scrape_shops(conn, mironet_site(), (GRAFICKA, HDD))
    assert (stats.requests, stats.seen, stats.new, stats.errors) == (2, 6, 6, [])
    rows = mironet_rows(conn)
    assert rows["1031826"] == ("retail", 6433, "graficka", None, False)
    assert rows["809984"] == ("retail", 39303, "hdd", None, False)
    normalize.run(conn)
    models = dict(conn.execute("SELECT listing_id, model FROM normalized WHERE category = 'gpu'").fetchall())
    assert models[sources.make_id("mironet", "1031826")] == "arc a750"
    # the same pass again updates, it does not duplicate
    again = scrape_shops(conn, mironet_site(), (GRAFICKA, HDD))
    assert (again.new, again.updated) == (0, 6)


def test_shop_pass_is_gated_by_its_cadence(conn):
    site = mironet_site()
    first = scrape_shops(conn, site, hours=6)
    assert first.requests == 1
    second = scrape_shops(conn, site, hours=6)  # six hours have not passed
    assert second.requests == 0 and len(site.urls) == 1
    assert conn.execute("SELECT count(*) FROM scrape_runs WHERE kind = 'scrape_shops'").fetchone() == (1,)
    conn.execute(
        "UPDATE kv SET value = to_jsonb((now() - interval '7 hours')::text) WHERE key = 'section_last_scraped:shop:mironet'"
    )
    assert scrape_shops(conn, site, hours=6).requests == 1


def test_items_that_left_the_outlet_are_retired_and_come_back(conn):
    page = fixture_text("mironet_rozbaleno_graficka.html")
    starts = [m.start() for m in shops.BLOCK.finditer(page)]
    without_quadro = page[: starts[0]] + page[starts[1] :]
    scrape_shops(conn, mironet_site())
    stats = scrape_shops(conn, mironet_site(gpu=ok(without_quadro, real=False)))
    assert stats.gone == 1
    rows = mironet_rows(conn)
    assert rows["839754"][3:] == ("not_listed", True) and rows["1031826"][4] is False
    scrape_shops(conn, mironet_site())  # listed again: seeing it clears gone_at
    assert mironet_rows(conn)["839754"][4] is False


def test_an_empty_outlet_listing_retires_everything_in_it(conn):
    scrape_shops(conn, mironet_site())
    empty = fixture_text("mironet_bazar_graficka_empty.html")
    stats = scrape_shops(conn, mironet_site(gpu=ok(empty, real=False)))
    assert stats.gone == 2 and not stats.errors


@pytest.mark.parametrize(
    "gpu",
    [
        (403, "blocked"),  # HTTP error
        ok("mironet_graficka_unfiltered.html"),  # products, but none of the outlet: the filter was ignored
    ],
)
def test_a_bad_page_never_retires_anything(conn, gpu):
    scrape_shops(conn, mironet_site())
    stats = scrape_shops(conn, mironet_site(gpu=gpu))
    assert stats.gone == 0 and len(stats.errors) == 1
    assert [v[4] for v in mironet_rows(conn).values()] == [False, False]


def test_a_network_error_is_recorded_and_retires_nothing(conn):
    scrape_shops(conn, mironet_site())

    def down(request):
        raise httpx.ConnectError("no route")

    flaky = PoliteClient(
        "t", min_delay=0, transport=httpx.MockTransport(down), sleep=lambda s: None, retries=1
    )
    stats = scraper.scrape_shops_once(flaky, conn, 2, 0, (GRAFICKA,))
    assert stats.gone == 0 and "no route" in stats.errors[0]
    assert all(v[4] is False for v in mironet_rows(conn).values())


def test_paging_follows_next_links_up_to_the_page_budget(conn):
    page = fixture_text("mironet_rozbaleno_graficka.html")
    with_next = page + '<a href="?PgID=2">2</a>'
    site = mironet_site(
        gpu=ok(with_next, real=False),
        **{shops.category_url(GRAFICKA, 2): ok("mironet_rozbaleno_graficka.html")},
    )
    scrape_shops(conn, site, pages=2)
    assert site.urls == [shops.category_url(GRAFICKA, 1), shops.category_url(GRAFICKA, 2)]
    # budget of one page with more to read: the category is incomplete, so nothing may be retired
    starts = [m.start() for m in shops.BLOCK.finditer(page)]
    short = page[: starts[0]] + page[starts[1] :] + '<a href="?PgID=2">2</a>'
    assert scrape_shops(conn, mironet_site(gpu=ok(short, real=False)), pages=1).gone == 0


def test_shop_urls_are_checked_before_they_are_fetched(conn):
    bad = shops.Category("/ajax/x", "rozbaleno", "graficka")
    site = Site({})
    with pytest.raises(ValueError, match="not an allowed URL"):
        scrape_shops(conn, site, (bad,))
    assert site.urls == []


def test_shop_items_are_never_rechecked_as_bazos_ads(conn):
    used_gpu_ads(conn, 1, "Intel Arc A750 8GB")
    scrape_shops(conn, mironet_site())
    normalize.run(conn)
    conn.execute("UPDATE listings SET last_seen = now() - interval '2 days'")
    due = repo.due_for_recheck(conn, 1, 100)
    assert [i for i, _ in due] == [100]  # the Bazoš ad only; the Mironet "Arc A750" is not asked about


# --- price samples, candidates and watchlists -------------------------------------------------------


def outlet_gpu(listing_id, title, price, source="mironet", kind="retail", section="graficka"):
    return Listing(
        id=listing_id,
        source=source,
        external_id=str(listing_id),
        url=f"https://www.mironet.cz/{listing_id}+dp{listing_id}/",
        title=title,
        price_czk=price,
        price_kind=kind,
        section=section,
        description="Doprava po celé ČR.",
    )


def test_shop_prices_stay_out_of_used_fair_prices_but_make_candidates(conn):
    used_gpu_ads(conn)  # 20 used RTX 3080 10GB ads, 9 500-10 450 Kč
    cheap = outlet_gpu(9001, "Rozbaleno - MSI GeForce RTX 3080 10GB", 6900)
    dear = outlet_gpu(9002, "Rozbaleno - ASUS GeForce RTX 3080 10GB", 30000)
    for item in (cheap, dear):
        repo.save_listing(conn, item, detail=True)
    normalize.run(conn)

    samples = repo.price_samples(conn, 60)
    assert len(samples) == 20 and {s.listing_id for s in samples} == set(range(100, 120))  # no 9001, 9002
    prices = pricing.run(conn, save=False)
    assert prices["rtx 3080 10gb"].n == 20 and 9500 < prices["rtx 3080 10gb"].median < 10500

    # ... yet they are looked at as deals: the cheap one is a candidate, the dear one is not
    assert {row[0] for row in repo.active_priced(conn, 3)} >= {9001, 9002}
    rules = alerts.Rules(
        enabled=True,
        min_score=0.12,
        max_per_day=5,
        min_samples=15,
        min_days=7,
        quiet_start=23,
        quiet_end=7,
        quiet_bypass=0.3,
        candidate_discount=0.2,
        distance=DistanceRules(),
    )
    assert alerts.find_candidates(conn, prices, rules) == 1
    (deal,) = repo.deals_in_state(conn, "triage")
    assert (deal["listing_id"], deal["price"], deal["source"]) == (9001, 6900, "mironet")
    message = alerts.format_alert(deal, prices["rtx 3080 10gb"])[1]
    assert "via Mironet (outlet, with warranty)" in message


def test_only_fixed_prices_are_samples(conn):
    """Each price kind next to 'retail': only 'fixed' feeds the fair price (the kinds are listed in
    repository.price_samples's SQL), and the same ad as 'retail' or as 'fixed' differs only there."""
    for i, kind in enumerate(["fixed", "retail", "auction", "negotiable"]):
        repo.save_listing(
            conn, outlet_gpu(500 + i, f"MSI RTX 3080 10GB #{i}", 9000, source="bazos", kind=kind), detail=True
        )
    normalize.run(conn)
    assert [s.listing_id for s in repo.price_samples(conn, 60)] == [500]


def test_watchlists_match_shop_outlet_items(conn):
    scrape_shops(conn, mironet_site(), (HDD,))
    normalize.run(conn)
    # the disk category is a canonical section and the RAM one a watch-only section: both reach watches
    repo.insert_watch(conn, "ultrastar", {"include": [["ultrastar"]]}, tag="homelab")
    answer = {
        "matches": True,
        "product_type": "HDD",
        "brand": "WD",
        "model": "Ultrastar DC HC570",
        "condition": "unboxed",
        "price_czk": 39303,
        "evidence": "WD Ultrastar DC HC570 22TB",
    }
    stats = watchlist.scan_and_match(conn, FakeLLM(json.dumps(answer)), None, hours=48)
    assert stats.matched == 1
    (hit,) = repo.pending_watch_matches(conn)
    assert hit["source"] == "mironet" and hit["price_czk"] == 39303
    site = FakeNtfy()
    now = datetime(2026, 10, 12, 14, 0, tzinfo=PRAGUE)
    assert watchlist.push_matches(conn, make_ntfy(site), now, 23, 7, max_per_day=10) == 1
    assert "via Mironet (outlet, with warranty)" in site.posts[0]["message"]
    assert "39 303 Kč" in site.posts[0]["message"]


def test_watch_only_shop_sections_are_stored_and_not_priced(conn):
    repo.save_listing(
        conn, outlet_gpu(9100, "Rozbaleno - Kingston FURY 32GB DDR5", 2900, section="pamet"), True
    )
    normalize.run(conn)
    assert conn.execute(
        "SELECT model, excluded_reason FROM normalized WHERE listing_id = 9100"
    ).fetchone() == (
        None,
        "watch_only",
    )
    assert repo.active_priced(conn, 3) == []


def test_watch_push_label_falls_back_to_the_source_name(conn):
    hit = {"query": "q", "title": "t", "price_czk": 1, "source": "newshop", "result": {}}
    assert watchlist.format_watch_push(hit)[1].endswith("via newshop")


# --- migration, throttle, settings ---------------------------------------------------------------------


def test_migration_adds_the_shop_column_and_throttle_row(conn):
    repo.save_new_price(conn, "rtx 3060", "https://www.zbozi.cz/vyrobek/a/", "p", 100, 1)
    assert conn.execute("SELECT shop FROM new_prices").fetchone() == ("zbozi",)  # the default
    assert {r[0] for r in conn.execute("SELECT id FROM http_throttle")} >= {1, 2, 3, 4}
    waits = []
    scraper.DbThrottle(conn, 0.0, sleep=waits.append, row=4).wait_turn()  # row 4 exists and can be taken
    assert waits == []


def test_settings_defaults_and_floors(monkeypatch):
    for name in (
        "HUNTER_SOURCES",
        "HUNTER_SHOP_PAGES",
        "HUNTER_SHOP_HOURS",
        "HUNTER_SHOP_MIN_DELAY",
        "HUNTER_DATART_PAGES",
    ):
        monkeypatch.delenv(name, raising=False)
    s = config.load_settings()
    assert s.sources == ("bazos",)  # shops are opt-in
    assert (s.shop_pages, s.shop_hours, s.shop_min_delay, s.datart_pages) == (2, 6.0, 6.5, 4)
    monkeypatch.setenv("HUNTER_SHOP_MIN_DELAY", "1")
    assert config.load_settings().shop_min_delay == 6.0  # never below 6 s


def test_shop_client_identifies_itself_and_throttles_on_row_4(conn, monkeypatch):
    monkeypatch.delenv("HUNTER_USER_AGENT", raising=False)
    s = config.load_settings()
    http = cli_scrape._shop_http(s, conn)
    assert http._client.headers["User-Agent"] == config.SHOP_USER_AGENT
    assert (http.min_delay, http.throttle.row, http.throttle.min_delay) == (6.5, 4, 6.5)
    assert cli_scrape._shop_http(s).throttle is None
    monkeypatch.setenv("HUNTER_USER_AGENT", "custom-agent")
    assert cli_scrape._shop_http(config.load_settings())._client.headers["User-Agent"] == "custom-agent"


def test_scrape_runs_the_shop_pass_only_when_enabled(conn, test_url, monkeypatch, capsys):
    calls = []

    def fake_pass(client, db, pages, hours):
        calls.append((pages, hours))
        stats = repo.RunStats("scrape_shops")
        stats.requests = len(calls) - 1  # the first pass was not due: it made no requests
        return stats

    monkeypatch.setattr(scraper, "scrape_shops_once", fake_pass)
    monkeypatch.setattr(repo, "connect", lambda *a, **k: psycopg.connect(test_url, autocommit=True))
    monkeypatch.setattr(cli_scrape, "_with_db", lambda work: work(None, conn))
    monkeypatch.setattr(normalize, "run", lambda c: 0)
    args = argparse.Namespace(pages=None, dry_run=False, loop=False)

    monkeypatch.setenv("HUNTER_SOURCES", "mironet")
    cli_scrape.cmd_scrape(args)
    assert calls == [(2, 6.0)] and "scrape_shops" not in capsys.readouterr().out  # not due: nothing printed
    cli_scrape.cmd_scrape(args)
    assert "scrape_shops: 1 requests" in capsys.readouterr().out
    monkeypatch.setenv("HUNTER_SOURCES", "bazos,nope")
    with pytest.raises(SystemExit, match="Unknown HUNTER_SOURCES"):
        cli_scrape.cmd_scrape(args)


def test_newprices_command_lists_datart_only_where_it_is_the_new_price(conn, test_url, monkeypatch, capsys):
    zbozi_price(conn, "rtx 5080", 43000)
    datart.fetch_due(client(datart_site()), conn, 7, 4)
    monkeypatch.setattr(repo, "connect", lambda *a, **k: psycopg.connect(test_url, autocommit=True))
    cli_prices.cmd_newprices(argparse.Namespace(fetch=False, all=False, limit=10))
    out = capsys.readouterr().out
    lines = out.splitlines()
    (rtx,) = [ln for ln in lines if "rtx 5080" in ln]
    assert (
        rtx.startswith("* ") and "43 000" in rtx and " zbozi " in rtx
    )  # Zboží is cheaper than Datart's 43 990
    # of Datart's two 8 TB NAS disks only the cheaper one, the model's new price, is listed
    (nas8,) = [ln for ln in lines if "hdd35 nas 8tb" in ln]
    assert nas8.startswith("* ") and "9 499" in nas8 and " datart " in nas8


def test_newprices_fetch_reads_zbozi_and_datart(test_url, monkeypatch, capsys):
    from deal_hunter import zbozi

    monkeypatch.setattr(repo, "connect", lambda *a, **k: psycopg.connect(test_url, autocommit=True))
    monkeypatch.setattr(zbozi, "pages", dict)  # no curated Zboží pages: only Datart is read
    monkeypatch.setattr(
        cli_prices, "make_new_price_http", lambda settings, throttle_conn=None: client(datart_site())
    )
    cli_prices.cmd_newprices(argparse.Namespace(fetch=True, all=True, limit=10))
    out = capsys.readouterr().out
    assert "new prices: 4 pages read, 7 with offers, 3 rejected, 0 errors" in out
    assert "* rtx 5080" in out and "datart" in out
