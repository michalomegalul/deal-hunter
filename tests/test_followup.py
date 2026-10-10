"""The 👍 follow-up without a database: the offer, the Czech draft and its evidence check, formatting."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from deal_hunter import followup, triage
from deal_hunter.auctions import Costs, Extraction, Item, value_lot
from deal_hunter.followup import check_draft, numbers, offer_price, template_draft
from deal_hunter.pricing import FairPrice
from tests.test_triage import FakeLLM

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
AD = {
    "id": 224876851,
    "title": "Gigabyte RTX 3060 Gaming OC 12GB",
    "price_czk": 4500,
    "url": "https://pc.bazos.cz/inzerat/224876851/rtx-3060.php",
    "location": "Brno",
    "psc": "602 00",
    "posted_at": date(2026, 10, 9),
    "first_seen": NOW - timedelta(hours=5),
    "views": 87,
    "seller": "Petr",
    "model": "rtx 3060 12gb",
    "description": "Prodám grafiku, 2 roky používaná na hraní, netěžila. Účtenka z Alzy. Pouze osobní odběr Brno.",
    "images": ["https://www.bazos.cz/img/1/851/224876851.jpg"],
}


@pytest.mark.parametrize(
    "price, offer",
    [(4500, 4100), (6500, 6000), (1200, 1100), (999, 900), (45000, 41500), (150000, 139000), (100, 50)],
)
def test_offer_is_a_round_number_5_to_10_percent_below(price, offer):
    got = offer_price(price)
    assert got == offer
    if price >= 1000:
        assert 0.90 <= got / price <= 0.97


def test_offer_respects_the_configured_discount():
    assert offer_price(10000, 0.05) == 9500 and offer_price(10000, 0.10) == 9000


def test_numbers_join_czech_thousands():
    assert numbers("Nabízím 4 100 Kč, RTX 3060 12GB") == {4100, 3060, 12}
    assert numbers("cena 4.100,- nebo 4 000") == {4100, 4000}


GOOD_DRAFT = (
    "Dobrý den, mám zájem o Gigabyte RTX 3060 Gaming OC. Je ještě k dispozici? "
    "Mohu nabídnout 4 100 Kč při osobním odběru v Brně. S pozdravem"
)


def test_a_faithful_draft_passes():
    assert check_draft(GOOD_DRAFT, AD, 4100, pickup=True) == []


@pytest.mark.parametrize(
    "draft, problem",
    [
        (GOOD_DRAFT.replace("4 100", "4 000"), "the offer 4100 is not in it"),
        (GOOD_DRAFT + " Karta má 3 roky záruku.", "numbers not in the ad: [3]"),
        ("Dobrý den, nabízím 4 100 Kč, pošlete to prosím na dobírku. Děkuji", "talks about shipping"),
        ("Dobrý den, nabízím 4 100 Kč, viz https://example.com. Děkuji", "link, e-mail or phone number"),
        ("Dobrý den, " + "moc " * 200 + "nabízím 4 100 Kč.", "too long"),
        ("", "empty"),
    ],
)
def test_a_draft_that_invents_facts_is_rejected(draft, problem):
    problems = check_draft(draft, AD, 4100, pickup=True)
    assert any(p.startswith(problem) for p in problems), problems


def test_pickup_only_when_the_ad_says_so():
    ad = {**AD, "description": "Prodám grafiku, funkční."}
    assert not followup.pickup_allowed(ad["description"])
    assert followup.pickup_allowed(AD["description"])
    assert "talks about pickup, the ad doesn't offer it" in check_draft(GOOD_DRAFT, ad, 4100, pickup=False)


def test_the_template_never_says_more_than_the_ad():
    for pickup in (True, False):
        text = template_draft(AD, 4100, pickup)
        assert text.startswith("Dobrý den, mám zájem o Gigabyte RTX 3060 Gaming OC 12GB.")
        assert "4 100 Kč" in text and ("osobním" in text) is pickup
        assert check_draft(text, AD, 4100, pickup) == []
    assert "Rtx 3060 Super Cena" in template_draft({**AD, "title": "RTX 3060 SUPER CENA!!!"}, 4100, False)


def test_draft_from_the_llm_or_the_template():
    llm = FakeLLM(json.dumps({"message": GOOD_DRAFT}))
    text, how, _ = followup.draft_message(llm, AD, 4100, None)
    assert (text, how) == (GOOD_DRAFT, "llm")
    system, user = llm.calls[0]
    assert "Offer price to propose: 4 100 Kč" in user and "pickup_in_person: yes" in user
    assert "do not" in system.lower()

    invented = json.dumps({"message": "Dobrý den, nabízím 4 100 Kč, karta je 1 rok stará."})
    text, how, notes = followup.draft_message(FakeLLM(invented), AD, 4100, None)
    assert how == "template" and text == template_draft(AD, 4100, True)
    assert any("numbers not in the ad" in n for n in notes)

    text, how, _ = followup.draft_message(FakeLLM(triage.LLMUnavailable("down")), AD, 4100, None)
    assert how == "template"
    assert followup.draft_message(None, AD, 4100, None)[1] == "template"
    assert followup.draft_message(FakeLLM("not json at all"), AD, 4100, None)[1] == "template"


TRIAGE = {
    "condition": "used",
    "for_parts": False,
    "defects": ["noisy fan"],
    "mining_suspected": False,
    "warranty_or_receipt": True,
    "urgent_sale": False,
    "scam_signals": [],
    "is_bundle": False,
    "is_wanted_ad": False,
    "one_line_verdict": "Receipt, pickup only, worth a look.",
}


def fair_price():
    fp = FairPrice("rtx 3060 12gb", "gpu", 9, 9, 6000, 5500, 6500, 0.5, "medium")
    fp.new_price, fp.new_source, fp.new_detail = 7490, "zbozi", "3 shops"
    return fp


def test_listing_followup_text():
    title, message = followup.format_listing(AD, fair_price(), 6000, TRIAGE, GOOD_DRAFT, 4100, NOW)
    assert title == "👍 Gigabyte RTX 3060 Gaming OC 12GB"
    lines = message.splitlines()
    assert lines[0] == "💰 4 500 Kč · fair ~6 000 Kč (−25%), n=9 medium · new ~7 490 Kč (Zboží, 3 shops)"
    assert lines[1] == "🏷 rtx 3060 12gb"
    assert lines[2] == "📍 Brno 602 00 · posted 09.10. · seen 5 h ago · 👁 87 · seller Petr"
    assert lines[3].startswith("🤖 used · defects: noisy fan · receipt/warranty ✓ · flags: none")
    assert "„Receipt, pickup only, worth a look.“" in message
    assert "📝 Prodám grafiku" in message and message.endswith(GOOD_DRAFT)
    assert "✉️ Message (offer 4 100 Kč):" in message
    test_title, _ = followup.format_listing(AD, None, None, None, GOOD_DRAFT, 4100, NOW, test=True)
    assert test_title.startswith("TEST · 👍")


def test_listing_followup_fits_in_one_ntfy_message():
    long_ad = {**AD, "description": "Popis karty se spoustou detailů. " * 300}
    _, message = followup.format_listing(long_ad, fair_price(), 6000, TRIAGE, GOOD_DRAFT, 4100, NOW)
    assert len(message.encode()) <= followup.NTFY_LIMIT
    assert "[…]" in message and message.endswith(GOOD_DRAFT)  # the draft is never cut
    desc = message.split("📝 ", 1)[1].split("\n\n✉️", 1)[0]
    assert len(desc) <= followup.MAX_DESCRIPTION + 4


def test_listing_followup_without_triage_or_price():
    _, message = followup.format_listing(
        {**AD, "model": None, "views": None}, None, None, None, "x", 4100, NOW
    )
    assert message.splitlines()[0] == "💰 4 500 Kč" and "🤖 not triaged" in message


def test_gone_text():
    title, message = followup.format_gone("listing", "RTX 3060", NOW)
    assert title == "👍 gone: RTX 3060" and "no longer available (since 10.10. 12:00)" in message


def test_lot_followup_text():
    x = Extraction(
        items=[Item("Dell Latitude 5511", "laptop", 14, "untested", ["charger"], key="latitude 5511")],
        inspection="po domluvě v Praze",
        pickup="Praha 4",
        notes="Sold as-is.",
        stated_units=14,
    )
    prices = {
        "latitude 5511": FairPrice("latitude 5511", "office_laptop", 12, 12, 6000, 5500, 6500, 0.5, "medium")
    }
    v = value_lot(x, prices, 30000, 1000, 2, Costs())
    lot = {"lot_id": "62388", "title": "Soubor 14 ks HW vybavení - Notebooky V.", "bids": 2, "url": "u"}
    end = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)
    title, message = followup.format_lot(lot, x, v, end)
    assert title == "👍 🏛 14× Dell Latitude 5511"
    assert "⏰ ends Wed 14.10. 14:00" in message
    assert (
        "now 30 000 Kč (2 bids), next bid 31 000 Kč" in message
        and f"max bid ~{followup.kc(v.max_bid)}" in message
    )
    assert "• 14× Dell Latitude 5511 (untested, missing charger) → fair 6 000 Kč (medium, n=12)" in message
    assert "Valuation: revenue" in message and "labour" in message
    assert "🔍 inspection: po domluvě v Praze" in message and "📍 pickup: Praha 4" in message
