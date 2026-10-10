"""The worker cycle against a real (throwaway) database, with a fake LLM and a fake ntfy server."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

import httpx

from deal_hunter import alerts, normalize, triage
from deal_hunter.alerts import PRAGUE, Rules
from deal_hunter.bazos import Listing
from deal_hunter.db import repository as repo
from tests.test_triage import GOOD, FakeLLM


class FakeNtfy:
    def __init__(self, feedback=()):
        self.posts, self.feedback = [], list(feedback)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.posts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"msg{len(self.posts)}"})
        lines = "\n".join(json.dumps(e) for e in self.feedback)
        return httpx.Response(200, text=lines)


def rules(**kw):
    hour = datetime.now(PRAGUE).hour
    base = {
        "enabled": True,
        "min_score": 0.12,
        "max_per_day": 5,
        "min_samples": 15,
        "min_days": 7,
        "quiet_start": (hour + 2) % 24,  # quiet hours safely away from "now"
        "quiet_end": (hour + 3) % 24,
        "quiet_bypass": 0.9,
        "candidate_discount": 0.2,
    }
    return Rules(**{**base, **kw})


def make_ntfy(site):
    return alerts.Ntfy(
        "http://ntfy.lan",
        "tk_pub",
        "deals",
        "https://ntfy.example",
        "deals-feedback",
        "tk_fb",
        httpx.MockTransport(site),
    )


def seed(conn, deal_price=6500, n=20):
    """n ordinary RTX 3080 ads from 10 days ago around 10 000 Kč, plus one fresh one at `deal_price`."""
    for i in range(n):
        repo.save_listing(
            conn,
            Listing(
                id=100 + i,
                url=f"https://pc.bazos.cz/inzerat/{100 + i}/x.php",
                title=f"MSI RTX 3080 10GB #{i}",
                price_czk=9500 + 50 * i,
                price_kind="fixed",
                seller=f"seller {i}",
            ),
            detail=True,
        )
    conn.execute("UPDATE listings SET first_seen = now() - interval '10 days'")
    repo.save_listing(
        conn,
        Listing(
            id=999,
            url="https://pc.bazos.cz/inzerat/999/deal.php",
            title="Gigabyte RTX 3080 10GB Vision",
            price_czk=deal_price,
            price_kind="fixed",
            seller="Lucky",
            location="Brno",
            psc="602 00",
            description="Prodám, účtenka.",
            images=["https://www.bazos.cz/img/1/999/999.jpg"],
        ),
        detail=True,
    )
    normalize.run(conn)


def work(conn, site, llm=None, now=None, **kw):
    return alerts.work_once(
        conn, rules(**kw), llm or FakeLLM(json.dumps(GOOD)), None, make_ntfy(site), 5, now
    )


def deal_state(conn):
    return conn.execute("SELECT state, reason FROM deals WHERE listing_id = 999").fetchone()


def test_good_deal_is_pushed_once(conn):
    seed(conn)
    site = FakeNtfy()
    stats = work(conn, site)
    assert stats.candidates == 1 and stats.triaged == 1 and stats.pushed == 1
    assert deal_state(conn) == ("alerted", None)
    push = site.posts[0]
    assert push["topic"] == "deals" and push["click"] == "https://pc.bazos.cz/inzerat/999/deal.php"
    assert "6 500 Kč vs ~" in push["message"] and "Brno" in push["message"] and push["priority"] in (4, 5)
    assert push["attach"] == "https://www.bazos.cz/img/1/999/999.jpg"
    assert "launch $699 (2020)" in push["message"]  # the new price line (MSRP: no Zboží price here)
    assert [a["body"] for a in push["actions"]] == [
        '{"ad": 999, "vote": "up"}',
        '{"ad": 999, "vote": "down"}',
    ]
    row = conn.execute("SELECT status, result->>'one_line_verdict', prompt_version FROM triage").fetchone()
    assert row == ("done", GOOD["one_line_verdict"], triage.PROMPT_VERSION)
    assert conn.execute("SELECT count(*) FROM fair_prices WHERE model = 'rtx 3080 10gb'").fetchone()[0] == 1

    stats = work(conn, site)  # nothing new: no second push, no second LLM call
    assert stats.pushed == 0 and stats.triaged == 0 and len(site.posts) == 1


def test_gated_model_only_records(conn):
    seed(conn)
    site = FakeNtfy()
    stats = work(conn, site, min_samples=50)
    assert stats.would_alert == 1 and not site.posts
    state, reason = deal_state(conn)
    assert state == "would_alert" and reason.startswith("gated: n=")
    # not enough history either
    conn.execute("UPDATE listings SET first_seen = now() - interval '1 day'")
    conn.execute("UPDATE deals SET state = 'ready'")
    work(conn, site)
    assert "days of data" in deal_state(conn)[1]


def test_disabled_switch(conn):
    seed(conn)
    site = FakeNtfy()
    work(conn, site, enabled=False)
    assert deal_state(conn) == ("would_alert", "alerts disabled") and not site.posts


def test_llm_down_queues_and_retries_later(conn):
    seed(conn)
    site = FakeNtfy()
    stats = work(conn, site, llm=FakeLLM(triage.LLMUnavailable("refused")))
    assert stats.llm_down and deal_state(conn)[0] == "triage" and not site.posts
    attempts, later = conn.execute("SELECT attempts, next_try_at > now() FROM triage").fetchone()
    assert attempts == 1 and later
    conn.execute("UPDATE triage SET next_try_at = now()")
    stats = work(conn, site)
    assert stats.pushed == 1


def test_llm_red_flags_drop_the_deal(conn):
    seed(conn)
    site = FakeNtfy()
    conn.execute("UPDATE listings SET description = 'Pošlete zálohu 500 Kč předem na účet.' WHERE id = 999")
    signal = {"signal": "deposit_or_prepay", "quote": "zálohu 500 Kč předem na účet"}
    work(conn, site, llm=FakeLLM(json.dumps({**GOOD, "scam_signals": [signal]})))
    assert deal_state(conn) == ("dropped", "scam signals: deposit_or_prepay") and not site.posts


def test_used_as_dear_as_new_drops_the_candidates(conn):
    seed(conn)
    repo.save_new_price(conn, "rtx 3080 10gb", "https://www.zbozi.cz/vyrobek/x/", "RTX 3080", 10000, 2)
    assert work(conn, FakeNtfy()).candidates == 0  # fair ~10 000 >= 95 % of new 10 000: estimate is off


def test_small_discount_is_no_candidate(conn):
    seed(conn, deal_price=9000)
    assert work(conn, FakeNtfy()).candidates == 0


def test_quiet_hours_hold_until_morning(conn):
    seed(conn)
    site = FakeNtfy()
    hour = datetime.now(PRAGUE).hour
    work(conn, site, quiet_start=hour, quiet_end=(hour + 1) % 24)
    assert deal_state(conn)[0] == "queued" and not site.posts
    work(conn, site)  # quiet hours over
    assert deal_state(conn)[0] == "alerted" and len(site.posts) == 1


def test_daily_cap(conn):
    seed(conn)
    work(conn, FakeNtfy(), max_per_day=0)
    assert deal_state(conn)[0] == "capped"


def test_weekly_digest_on_sunday_evening_once(conn):
    seed(conn)
    site = FakeNtfy()
    sunday = datetime(2026, 10, 18, 16, 30, tzinfo=UTC)  # 18:30 in Prague
    prices = {}
    since = repo.data_since(conn)
    assert not alerts.maybe_send_digest(
        conn, make_ntfy(site), prices, since, rules(), sunday - timedelta(hours=1)
    )
    assert alerts.maybe_send_digest(conn, make_ntfy(site), prices, since, rules(), sunday)
    assert not alerts.maybe_send_digest(
        conn, make_ntfy(site), prices, since, rules(), sunday + timedelta(hours=1)
    )
    assert site.posts[0]["title"] == "deal-hunter: still learning"
    assert "0 of 0 priced models ready" in site.posts[0]["message"]
