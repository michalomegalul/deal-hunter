"""The auction step against a real (throwaway) database, a fake nms.gov.cz, a fake LLM and a fake ntfy."""

import json
import os
from datetime import UTC, datetime

import pytest

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set (start it with `docker compose up -d db`)", allow_module_level=True)

import httpx

from deal_hunter import auction_cycle, auctions
from deal_hunter.auction_cycle import AuctionRules
from deal_hunter.auctions import Costs
from deal_hunter.db import repository as repo
from deal_hunter.http import PoliteClient
from deal_hunter.scraper import DbThrottle
from tests.conftest import FIXTURES
from tests.test_auctions import answer, fair, item
from tests.test_worker import FakeNtfy, make_ntfy


class FakeSite:
    """nms.gov.cz from the saved fixtures. Unknown lots get the single-PC page."""

    def __init__(self):
        self.calls, self.details = [], {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/api/Property/AuctionList":
            body = json.loads(request.content)
            name = "active" if body["ListType"] == "active" else "closed"
            return httpx.Response(200, text=(FIXTURES / f"nms_list_{name}.json").read_text(encoding="utf-8"))
        if path.startswith("/api/Property/Attachment/"):
            pdf = (FIXTURES / "nms_attachment_62785.pdf").read_bytes()
            return httpx.Response(200, content=pdf, headers={"Content-Type": "application/pdf"})
        if path.startswith("/Home/AuctionDetail/"):
            lot_id = path.rsplit("/", 1)[1]
            page = self.details.get(lot_id, lot_id)
            f = FIXTURES / f"nms_detail_{page}.html"
            if not f.exists():
                f = FIXTURES / "nms_detail_62495.html"
            return httpx.Response(200, text=f.read_text(encoding="utf-8"))
        return httpx.Response(404)


class LotLLM:
    """Answers by lot: lot 62785 is '19 Latitude 5511' (to have something that qualifies)."""

    model = "fake:1b"

    def __init__(self):
        self.calls = 0

    def chat(self, system, user, schema):
        self.calls += 1
        assert "items" in schema["properties"]
        if "Auction lot 62785 " in user:
            data = answer(item(quantity=19, quote="Aukce 19ks notebooků", missing=["storage"]))
        elif "Latitude 5511 (14)" in user:
            data = answer(item())
        else:
            data = answer()
        return json.dumps(data), {"prompt_tokens": 10, "output_tokens": 5, "seconds": 0.1}


def rules(**kw):
    base = {
        "enabled": True,
        "alerts": True,
        "list_hours": 2.5,
        "details_per_cycle": 100,
        "extract_per_cycle": 100,
        "max_per_day": 3,
        "costs": Costs(),
    }
    return AuctionRules(**{**base, **kw})


def client(site, conn=None):
    throttle = DbThrottle(conn, 5, sleep=lambda s: None, row=2) if conn is not None else None
    return PoliteClient(
        "ua", min_delay=5, transport=httpx.MockTransport(site), sleep=lambda s: None, throttle=throttle
    )


PRICES = {"latitude 5511": fair(median=7000)}
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def test_scrape_extract_value_push_and_final_price(conn, tmp_path):
    site, ntfy_site, llm = FakeSite(), FakeNtfy(), LotLLM()
    ntfy = make_ntfy(ntfy_site)
    http = client(site, conn)

    stats = auction_cycle.tick(conn, http, llm, tmp_path, ntfy, PRICES, rules(), quiet=False, now=T0)
    assert stats.listed and stats.new == 64  # 14 running + 50 ended
    lot = repo.get_lot(conn, "nms", "62785")
    assert lot["status"] == "active" and lot["current_price"] == 7600 and lot["start_price"] == 7600
    assert "18× PC přenosný ACER Extensa 2540" in lot["attachment_text"]
    assert lot["detail_at"] is not None and not lot["final_checked"]
    sold = repo.get_lot(conn, "nms", "62388")
    assert sold["final_price"] == 63000 and sold["final_checked"]  # first seen after its end
    unsold = repo.get_lot(conn, "nms", "62385")
    assert unsold["status"] == "unsold" and unsold["detail_at"] is None  # no detail for unsold history
    assert stats.extracted > 0 and stats.qualifying >= 1
    assert lot["lot_id"] == "62785"
    lot = repo.get_lot(conn, "nms", "62785")
    assert lot["qualifies"] and lot["valuation"]["confidence"] == "medium"
    assert len(ntfy_site.posts) == 1
    push = ntfy_site.posts[0]
    assert push["title"] == "🏛 Aukce: 19× Dell Latitude 5511"
    assert push["click"] == "https://nms.gov.cz/Home/AuctionDetail/62785"
    assert push["tags"] == ["classical_building"]
    assert repo.auction_alert_kinds(conn, "nms", "62785") == {"first"}

    # Nothing due an hour later: no list read, no details, no push, no LLM call.
    site.calls.clear()
    calls = llm.calls
    later = auction_cycle.tick(conn, http, llm, tmp_path, ntfy, PRICES, rules(), False, T0.replace(hour=13))
    assert site.calls == [] and later.pushed == 0 and llm.calls == calls

    # ~22 h before the end: the day reminder, after a fresh look at the detail page.
    day = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    auction_cycle.tick(conn, http, llm, tmp_path, ntfy, PRICES, rules(), False, day)
    assert ("GET", "/Home/AuctionDetail/62785") in site.calls
    assert repo.auction_alert_kinds(conn, "nms", "62785") == {"first", "day"}
    assert ntfy_site.posts[-1]["message"].startswith("⏰ ends tomorrow")

    # Quiet hours hold the final reminder; afterwards it goes out with priority 4.
    final = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
    n = len(ntfy_site.posts)
    auction_cycle.tick(conn, http, llm, tmp_path, ntfy, PRICES, rules(), True, final)
    assert len(ntfy_site.posts) == n
    auction_cycle.tick(conn, http, llm, tmp_path, ntfy, PRICES, rules(), False, final)
    assert ntfy_site.posts[-1]["priority"] == 4
    assert repo.auction_alert_kinds(conn, "nms", "62785") == {"first", "day", "final"}

    # After the end: one more detail fetch records the final price (here: the ended page of 62388).
    site.details["62785"] = "62388"
    after = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    auction_cycle.tick(conn, http, llm, tmp_path, ntfy, PRICES, rules(), False, after)
    lot = repo.get_lot(conn, "nms", "62785")
    assert lot["status"] == "ended" and lot["final_price"] == 63000 and lot["final_checked"]


def test_cap_and_disabled_alerts(conn, tmp_path):
    site, ntfy_site = FakeSite(), FakeNtfy()
    ntfy = make_ntfy(ntfy_site)
    off = auction_cycle.tick(
        conn, client(site), LotLLM(), tmp_path, ntfy, PRICES, rules(alerts=False), False, T0
    )
    assert off.qualifying >= 1 and ntfy_site.posts == []
    capped = auction_cycle.tick(
        conn, client(site), LotLLM(), tmp_path, ntfy, PRICES, rules(max_per_day=0), False, T0
    )
    assert capped.pushed == 0 and ntfy_site.posts == []


def test_llm_down_waits(conn, tmp_path):
    from deal_hunter.triage import LLMUnavailable

    class Down:
        model = "fake:1b"

        def chat(self, *a):
            raise LLMUnavailable("connection refused")

    stats = auction_cycle.tick(conn, client(FakeSite()), Down(), tmp_path, None, PRICES, rules(), False, T0)
    assert stats.llm_down and stats.extracted == 0
    row = conn.execute("SELECT status, attempts FROM auction_extractions LIMIT 1").fetchone()
    assert row == ("pending", 1)


def test_detail_gone_is_not_fetched_again(conn, tmp_path):
    class Gone(FakeSite):
        def __call__(self, request):
            if request.url.path.startswith("/Home/AuctionDetail/"):
                self.calls.append(("GET", request.url.path))
                return httpx.Response(302, headers={"Location": "/Home/Error"})
            return super().__call__(request)

    site = Gone()
    auction_cycle.tick(conn, client(site), None, tmp_path, None, PRICES, rules(), False, T0)
    assert repo.get_lot(conn, "nms", "62785")["status"] == "gone"
    site.calls.clear()
    auction_cycle.tick(conn, client(site), None, tmp_path, None, PRICES, rules(), False, T0.replace(hour=13))
    assert site.calls == []
    assert auctions.PROMPT_VERSION >= 1
