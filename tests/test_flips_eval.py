"""The flips log (`hunt flip ...`), `hunt eval` and the /flips page against a real (throwaway) database."""

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

from deal_hunter import cli, evaluation, web
from deal_hunter.alerts import Vote
from deal_hunter.bazos import Listing
from deal_hunter.db import repository as repo

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def hunt(conn, test_url, monkeypatch, capsys):
    """Run `hunt <args>` against the test database; returns (stdout) or raises SystemExit."""
    monkeypatch.setenv("DATABASE_URL", test_url)

    def run(*argv: str) -> str:
        monkeypatch.setattr(sys, "argv", ["hunt", *argv])
        capsys.readouterr()
        cli.main()
        return capsys.readouterr().out

    return run


def ad(conn, listing_id, title="Gigabyte RTX 3060 12GB", category="gpu", model="rtx 3060 12gb", price=4500):
    repo.save_listing(
        conn,
        Listing(
            id=listing_id,
            url=f"https://pc.bazos.cz/inzerat/{listing_id}/x.php",
            title=title,
            price_czk=price,
            price_kind="fixed",
        ),
        detail=True,
    )
    conn.execute(
        "INSERT INTO normalized (listing_id, category, model, normalizer_version) VALUES (%s, %s, %s, 1)",
        (listing_id, category, model),
    )


def deal(conn, listing_id, state, score, age_hours=200, gone_after_hours=None, category="gpu"):
    """A deal on a listing first seen `age_hours` ago; gone `gone_after_hours` after that (None = still up)."""
    ad(conn, listing_id, category=category)
    first_seen = NOW - timedelta(hours=age_hours)
    gone_at = first_seen + timedelta(hours=gone_after_hours) if gone_after_hours is not None else None
    conn.execute(
        "UPDATE listings SET first_seen = %s, gone_at = %s, gone_reason = %s WHERE id = %s",
        (first_seen, gone_at, "http_404" if gone_at else None, listing_id),
    )
    conn.execute(
        "INSERT INTO deals (listing_id, model, category, price, fair, discount, confidence, score, state, "
        "first_flagged) VALUES (%s, 'rtx 3060 12gb', %s, 4500, 6000, 0.25, 0.6, %s, %s, %s)",
        (listing_id, category, score, state, NOW - timedelta(hours=age_hours)),
    )


def vote(conn, listing_id, kind, source_id):
    conn.execute(
        "INSERT INTO feedback (listing_id, vote, source_id, voted_at) VALUES (%s, %s, %s, now())",
        (listing_id, kind, source_id),
    )


# --- hunt flip -------------------------------------------------------------------------------------------


def test_bought_tap_records_the_category(conn):
    ad(conn, 11)
    repo.save_votes(conn, [Vote("listing", "11", "bought", "tap1", NOW)])
    (flip,) = repo.list_flips(conn)
    assert (flip["listing_id"], flip["buy_price"], flip["category"]) == (11, 4500, "gpu")


def test_flip_add_fills_title_and_model_from_the_ad(conn, hunt):
    ad(conn, 12)
    assert "Logged ad 12" in hunt("flip", "add", "--ad", "12", "--price", "4000", "--notes", "pickup in Brno")
    (flip,) = repo.list_flips(conn)
    assert flip["title"] == "Gigabyte RTX 3060 12GB"
    assert (flip["model"], flip["category"], flip["buy_price"], flip["notes"]) == (
        "rtx 3060 12gb",
        "gpu",
        4000,
        "pickup in Brno",
    )
    (source_id,) = conn.execute("SELECT source_id FROM flips").fetchone()
    assert source_id.startswith("manual:")


def test_flip_add_for_an_ad_we_never_saw_and_with_a_date(conn, hunt):
    hunt("flip", "add", "--ad", "777", "--price", "100", "--at", "2026-09-01")
    (flip,) = repo.list_flips(conn)
    assert (flip["title"], flip["model"], flip["category"]) == (None, None, None)
    assert flip["bought_at"].date().isoformat() == "2026-09-01"


def test_flip_add_twice_is_an_error(conn, hunt):
    hunt("flip", "add", "--ad", "12", "--price", "4000")
    with pytest.raises(SystemExit, match="already in the flips log"):
        hunt("flip", "add", "--ad", "12", "--price", "3000")
    assert len(repo.list_flips(conn)) == 1


def test_buy_price_fees_and_sell_update_the_flip(conn, hunt):
    hunt("flip", "add", "--ad", "12", "--price", "4000")
    hunt("flip", "buy-price", "12", "3800", "--notes", "haggled")
    hunt("flip", "fees", "12", "150")
    out = hunt("flip", "sell", "12", "5200", "--at", "2026-10-01")
    assert "sold_price=5200" in out
    (flip,) = repo.list_flips(conn)
    assert (flip["buy_price"], flip["fees"], flip["sold_price"], flip["notes"]) == (
        3800,
        150,
        5200,
        "haggled",
    )
    assert flip["sold_at"].date().isoformat() == "2026-10-01"
    assert evaluation.flip_profit(flip) == 5200 - 3800 - 150


def test_sell_with_fees_and_default_date_keeps_notes(conn, hunt):
    hunt("flip", "add", "--ad", "12", "--price", "4000", "--notes", "keep me")
    hunt("flip", "sell", "12", "5000", "--fees", "200")
    (flip,) = repo.list_flips(conn)
    assert (flip["fees"], flip["notes"]) == (200, "keep me")  # notes untouched without --notes
    assert abs(flip["sold_at"] - datetime.now(UTC)) < timedelta(minutes=5)


@pytest.mark.parametrize(
    "argv",
    [
        ("buy-price", "999", "100"),
        ("sell", "999", "100"),
        ("fees", "999", "10"),
    ],
)
def test_unknown_listing_id_is_a_clear_error(hunt, argv):
    with pytest.raises(SystemExit, match="No flip for ad 999") as exc:
        hunt("flip", *argv)
    assert exc.value.code != 0 and isinstance(exc.value.code, str)


@pytest.mark.parametrize(
    "argv",
    [
        ("sell", "12", "abc"),
        ("sell", "12", "-5"),
        ("sell", "12", "100", "--at", "2026-13-40"),
        ("sell", "12", "100", "--at", "yesterday"),
        ("list", "--open", "--sold"),
    ],
)
def test_bad_input_is_a_usage_error_not_a_traceback(hunt, argv):
    with pytest.raises(SystemExit) as exc:
        hunt("flip", *argv)
    assert exc.value.code == 2


def test_update_flip_rejects_unknown_columns(conn):
    with pytest.raises(ValueError):
        repo.update_flip(conn, 1, source_id="x")
    with pytest.raises(ValueError):
        repo.update_flip(conn, 1)


def test_flip_list_shows_profit_and_totals_and_filters(conn, hunt):
    hunt("flip", "add", "--ad", "1", "--price", "1000", "--at", "2026-09-01")
    hunt("flip", "sell", "1", "1500", "--fees", "100", "--at", "2026-09-11")
    hunt("flip", "add", "--ad", "2", "--price", "2000")
    out = hunt("flip", "list")
    assert "2 flip(s): 1 open, 1 sold" in out
    assert "Invested in open flips 2 000 Kč" in out
    assert "realised profit 400 Kč on 1 100 Kč spent, ROI 36%" in out
    assert " 10 d " in out  # days held of the sold one
    open_out = hunt("flip", "list", "--open")
    assert "1 flip(s): 1 open, 0 sold" in open_out and "ROI -" in open_out
    sold_out = hunt("flip", "list", "--sold")
    assert "1 flip(s): 0 open, 1 sold" in sold_out and "profit      400" in sold_out


def test_flip_list_empty(hunt):
    assert "0 flip(s)" in hunt("flip", "list")


# --- pure math -------------------------------------------------------------------------------------------


def _flip(buy, sold=None, fees=None, bought_at=NOW - timedelta(days=3), sold_at=None):
    return {
        "buy_price": buy,
        "sold_price": sold,
        "fees": fees,
        "bought_at": bought_at,
        "sold_at": sold_at if sold_at is not None else (NOW if sold is not None else None),
        "category": "gpu",
    }


def test_profit_and_days_held():
    assert evaluation.flip_profit(_flip(1000, 1500)) == 500
    assert evaluation.flip_profit(_flip(1000, 1500, fees=120)) == 380
    assert evaluation.flip_profit(_flip(1000)) is None  # not sold
    assert evaluation.flip_profit(_flip(None, 1500)) is None  # buy price unknown
    assert evaluation.flip_profit(_flip(1000, None, sold_at=NOW)) is None  # sold without a price
    assert evaluation.days_held(_flip(1000), NOW) == 3
    assert evaluation.days_held(_flip(1000, 1500, sold_at=NOW - timedelta(days=1)), NOW) == 2
    assert evaluation.days_held(_flip(1000, bought_at=NOW + timedelta(days=1)), NOW) == 0


def test_totals_and_roi():
    t = evaluation.flip_totals([_flip(1000, 1500, fees=100), _flip(2000, 2000), _flip(3000), _flip(None)])
    assert (t["open"], t["sold"], t["invested"]) == (2, 2, 3000)
    assert (t["profit"], t["cost"]) == (400, 3100)
    assert t["roi"] == pytest.approx(400 / 3100)
    empty = evaluation.flip_totals([])
    assert empty["roi"] is None and empty["profit"] == 0


@pytest.mark.parametrize(
    ("score", "band"),
    [
        (None, "unscored"),
        (-0.1, "<0.12"),
        (0.0, "<0.12"),
        (0.119, "<0.12"),
        (0.12, "0.12-0.2"),
        (0.2, "0.2-0.3"),
        (0.299, "0.2-0.3"),
        (0.3, ">=0.3"),
        (0.9, ">=0.3"),
    ],
)
def test_band_of(score, band):
    assert evaluation.band_of(score) == band


def test_gone_within_boundaries():
    first = NOW - timedelta(hours=100)

    def d(gone_after=None, first_seen=first):
        gone = first_seen + timedelta(hours=gone_after) if gone_after is not None else None
        return {"first_seen": first_seen, "gone_at": gone}

    assert evaluation.gone_within(d(5), 24, NOW) is True
    assert evaluation.gone_within(d(24), 24, NOW) is True  # exactly on the boundary
    assert evaluation.gone_within(d(25), 24, NOW) is False  # went later than the window
    assert evaluation.gone_within(d(None), 24, NOW) is False  # old enough and still up
    young = NOW - timedelta(hours=10)
    assert evaluation.gone_within(d(None, young), 24, NOW) is None  # too young to tell
    assert evaluation.gone_within(d(3, young), 24, NOW) is True  # gone already, no need to wait


# --- hunt eval -------------------------------------------------------------------------------------------


def test_eval_tables_bands_votes_and_vanishing_speed(conn):
    # alerted: one gone in 5 h (👍), one gone in 60 h (👎), one still up
    deal(conn, 1, "alerted", 0.35, gone_after_hours=5)
    deal(conn, 2, "alerted", 0.25, gone_after_hours=60)
    deal(conn, 3, "alerted", 0.15)
    # low_score: one gone in 10 h, one still up, one too young to judge for 72 h
    deal(conn, 4, "low_score", 0.05, gone_after_hours=10)
    deal(conn, 5, "low_score", 0.08)
    deal(conn, 6, "low_score", 0.09, age_hours=30)
    deal(conn, 7, "dropped", None)
    deal(conn, 8, "would_alert", 0.13, category="hdd", gone_after_hours=2)
    vote(conn, 1, "up", "v1")
    vote(conn, 2, "down", "v2")
    vote(conn, 3, "up", "v3")
    # test push and a lot vote never count
    vote(conn, 0, "up", "test1")
    conn.execute("INSERT INTO feedback (lot_id, vote, source_id, voted_at) VALUES ('5', 'up', 'lot1', now())")

    deals = repo.eval_deals(conn, NOW - timedelta(days=90))
    assert len(deals) == 8
    by_id = {d["listing_id"]: d for d in deals}
    assert (by_id[1]["has_up"], by_id[1]["has_down"], by_id[4]["has_up"]) == (True, False, False)

    tables = {t.title.split(" (")[0]: t for t in evaluation.eval_tables(deals, [], NOW)}
    states = {r[0]: r for r in tables["Deals by state"].rows}
    assert states["all"] == ["all", "8", "3", "1", "0", "0", "3", "1", "0"]
    assert states["hdd"][1] == "1"

    bands = {(r[0], r[1]): r for r in tables["Score bands"].rows}
    assert bands[("gpu", ">=0.3")] == ["gpu", ">=0.3", "1", "1", "1", "0", "100%", "1/1 100%", "1/1 100%"]
    assert bands[("gpu", "0.2-0.3")][3:9] == ["1", "0", "1", "0%", "0/1 0%", "1/1 100%"]
    assert bands[("gpu", "<0.12")][2:4] == ["3", "0"]
    assert bands[("gpu", "<0.12")][7] == "1/3 33%"  # the 30 h old one still counts for 24 h, not for 72 h
    assert bands[("gpu", "<0.12")][8] == "1/2 50%"
    assert bands[("gpu", "unscored")][2] == "1"
    assert bands[("all", "0.12-0.2")][2] == "2"  # alerted 0.15 (gpu) + would_alert 0.13 (hdd)

    speed = {(r[0], r[1]): r for r in tables["Gone within 24 h / 72 h of first seen"].rows}
    assert speed[("gpu", "alerted")][2:] == ["3", "1/3 33%", "2/3 67%"]
    assert speed[("gpu", "low_score")][2:] == ["3", "1/3 33%", "1/2 50%"]
    assert ("gpu", "dropped") not in speed

    text = evaluation.format_text(list(tables.values()))
    assert "Deals by state" in text and "alerted" in text


def test_eval_respects_the_period_and_skips_empty_groups(conn):
    deal(conn, 1, "alerted", 0.35)
    conn.execute("UPDATE deals SET first_flagged = %s", (NOW - timedelta(days=120),))
    assert repo.eval_deals(conn, NOW - timedelta(days=90)) == []
    assert len(repo.eval_deals(conn, NOW - timedelta(days=200))) == 1
    tables = evaluation.eval_tables([], [], NOW)
    assert tables[1].rows == [] and tables[2].rows == []
    assert tables[0].rows == [["all", "0", "0", "0", "0", "0", "0", "0", "0"]]


def test_eval_flips_per_category_and_other_states(conn):
    deal(conn, 1, "alerted", 0.35)
    deal(conn, 2, "ready", 0.35)
    deals = repo.eval_deals(conn, NOW - timedelta(days=90))
    assert deals[1]["bought"] is False
    flips = [
        {**_flip(1000, 1500), "category": "gpu"},
        {**_flip(2000), "category": None},
    ]
    tables = evaluation.eval_tables(deals, flips, NOW)
    assert tables[0].rows[0][-1] == "1"  # 'ready' lands in other
    assert tables[3].rows == [
        ["all", "2", "1", "500", "50%"],
        ["gpu", "1", "1", "500", "50%"],
        ["unknown", "1", "0", "0", "-"],
    ]


def test_eval_marks_bought_ads(conn):
    deal(conn, 1, "alerted", 0.35)
    repo.save_votes(conn, [Vote("listing", "1", "bought", "tap", NOW)])
    (row,) = repo.eval_deals(conn, NOW - timedelta(days=90))
    assert row["bought"] is True


def test_hunt_eval_prints_the_report(conn, hunt):
    first_seen = datetime.now(UTC) - timedelta(days=5)
    ad(conn, 1)
    conn.execute("UPDATE listings SET first_seen = %s", (first_seen,))
    conn.execute(
        "INSERT INTO deals (listing_id, model, category, price, fair, discount, confidence, score, state) "
        "VALUES (1, 'rtx 3060 12gb', 'gpu', 4500, 6000, 0.25, 0.6, 0.2, 'alerted')"
    )
    hunt("flip", "add", "--ad", "1", "--price", "4000")
    hunt("flip", "sell", "1", "5000")
    out = hunt("eval", "--days", "30")
    assert "last 30 days (1 deals)" in out
    assert "Deals by state" in out and "0.2-0.3" in out and "ROI" in out
    assert "1000" in out and "25%" in out  # profit 1000 on 4000 spent


# --- /flips ---------------------------------------------------------------------------------------------


def test_flips_page_renders_escapes_and_totals(conn):
    ad(conn, 1, title="<img src=x onerror=alert(1)> RTX")
    repo.add_flip(conn, 1, 1000, NOW - timedelta(days=5), "<b>notes</b>")
    repo.update_flip(conn, 1, sold_price=1500, sold_at=NOW - timedelta(days=1), fees=100)
    repo.add_flip(conn, 2, 700, NOW - timedelta(days=2), None)
    repo.add_flip(conn, 3, 700, NOW - timedelta(days=2), None)
    repo.update_flip(conn, 3, sold_price=500, sold_at=NOW)  # a loss
    html = web.render_flips(conn, NOW)
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert "<b>notes</b>" not in html and "&lt;b&gt;notes&lt;/b&gt;" in html
    assert "2 open" not in html and "1 open, 2 sold" in html
    assert "Invested in open flips 700 Kč" in html
    assert 'class="good">400 Kč' in html and 'class="bad">-200 Kč' in html
    assert "Alert evaluation, last 90 days" in html and "Deals by state" in html


def test_flips_page_empty(conn):
    html = web.render_flips(conn)
    assert "No flips yet" in html and "0 open, 0 sold" in html


def test_flips_route_and_nav():
    assert '<a href="/flips">Flips</a>' in web.NAV
