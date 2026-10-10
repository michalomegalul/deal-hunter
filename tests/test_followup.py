"""The 👍 follow-up without a database: the offer, the message to the seller, formatting."""

from datetime import UTC, date, datetime, timedelta

import pytest

from deal_hunter import followup
from deal_hunter.auctions import Costs, Extraction, Item, value_lot
from deal_hunter.followup import offer_price, seller_message
from deal_hunter.normalize import classify
from deal_hunter.pricing import FairPrice

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


ASK = "Mohl byste mi prosím sdělit, zda je stále k dispozici?"


def message_for(title: str, section: str) -> str:
    r = classify(title, section)
    return seller_message(
        {"title": title, "model": r.model, "category": r.category, "section": section, "features": r.features}
    )


@pytest.mark.parametrize(
    "section, title, item",
    [
        # the user's own example
        (
            "graficka",
            "Prodám ZOTAC GAMING GeForce RTX 3070 Twin Edge OC 8GB",
            "grafickou kartu ZOTAC RTX 3070",
        ),
        # real titles (tests/fixtures/titles.yaml, office_titles.yaml): brand + model in the seller's spelling
        (
            "graficka",
            "Nová GAINWARD GeForce RTX 5070 Python III - Pečetěno, záruka",
            "grafickou kartu GAINWARD RTX 5070",
        ),
        ("graficka", "PRODÁM RTX 4090 24GB", "grafickou kartu RTX 4090"),
        (
            "graficka",
            "NOVÁ - MSI GeForce RTX 5060 Ti 8G GAMING TRIO OC",
            "grafickou kartu MSI RTX 5060 Ti 8GB",
        ),
        ("graficka", "Nvidia RTX 3080 ti Gigabyte", "grafickou kartu Gigabyte RTX 3080 ti"),
        ("graficka", "RTX3070", "grafickou kartu RTX3070"),
        ("graficka", "Rx 6750xt", "grafickou kartu Rx 6750xt"),
        ("graficka", "PALiT GeForce RTX 4070 Ti GameRock, 12GB GDDR6X", "grafickou kartu PALiT RTX 4070 Ti"),
        (
            "graficka",
            "GeForce GTX 1080 Ti Founders Edition + Accelero Xtreme III",
            "grafickou kartu GTX 1080 Ti",
        ),
        ("graficka", "Gigabyte RTX 3060 Gaming OC 12GB", "grafickou kartu Gigabyte RTX 3060 12GB"),
        ("graficka", "Intel Arc A770 16G", "grafickou kartu Intel Arc A770 16GB"),
        ("graficka", "🔥 ASUS TUF RTX 3080 10GB 🔥 TOP STAV 9000 Kč", "grafickou kartu ASUS RTX 3080 10GB"),
        (
            "pc",
            "Dell Optiplex 5040 SFF - i3-6100, 8GB, 128GB SSD, VÍCE KUSŮ",
            "počítač Dell Optiplex 5040 SFF",
        ),
        ("pc", "HP ProDesk 600 G2 - i3-6100, 4GB DDR4, 500GB HDD, VÍCE KUSŮ", "počítač HP ProDesk 600 G2"),
        ("notebook", "Lenovo Think Pad X13 G2", "notebook Lenovo ThinkPad X13 Gen 2"),
        ("notebook", "DELL Latitude 5490 i5/8GB/256SSD", "notebook DELL Latitude 5490"),
        ("pc", "Lenovo ThinkCentre M720q Tiny i5-8400T", "počítač Lenovo ThinkCentre M720q"),
        ("pc", 'HP EliteOne 800 G3 23" AiO', "all-in-one počítač HP EliteOne 800 G3"),
    ],
)
def test_message_to_the_seller(section, title, item):
    assert message_for(title, section) == f"Dobrý den, zajímám se o {item}. {ASK}"


def test_message_style():
    text = message_for("Prodám ZOTAC GAMING GeForce RTX 3070 Twin Edge OC 8GB", "graficka")
    assert text == (
        "Dobrý den, zajímám se o grafickou kartu ZOTAC RTX 3070. "
        "Mohl byste mi prosím sdělit, zda je stále k dispozici?"
    )
    for banned in ("Mohl(a)", "Děkuji", "Kč", "nabíd", "PRODÁM", "Twin Edge"):
        assert banned not in text
    assert seller_message({"title": "Grafika", "model": None, "category": "gpu"}) == (
        f"Dobrý den, zajímám se o grafickou kartu z vašeho inzerátu. {ASK}"
    )


MESSAGE = f"Dobrý den, zajímám se o grafickou kartu Gigabyte RTX 3060 12GB. {ASK}"

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
    title, message = followup.format_listing(AD, fair_price(), 6000, TRIAGE, MESSAGE, 4100, NOW)
    assert title == "👍 Gigabyte RTX 3060 Gaming OC 12GB"
    lines = message.splitlines()
    assert lines[0] == "💰 4 500 Kč · fair ~6 000 Kč (−25%), n=9 medium · new ~7 490 Kč (Zboží, 3 shops)"
    assert lines[1] == "🏷 rtx 3060 12gb"
    assert lines[2] == "📍 Brno · 182 km · posted 09.10. · seen 5 h ago · 👁 87 · seller Petr"
    assert lines[3].startswith("🤖 used · defects: noisy fan · receipt/warranty ✓ · flags: none")
    assert "„Receipt, pickup only, worth a look.“" in message
    assert "📝 Prodám grafiku" in message and message.endswith(MESSAGE)
    assert "\n\n💬 If negotiating: ~4 100 Kč\n\n✉️ Message:\n" + MESSAGE in message
    assert message.endswith(MESSAGE) and "4 100" not in MESSAGE
    test_title, _ = followup.format_listing(AD, None, None, None, MESSAGE, 4100, NOW, test=True)
    assert test_title.startswith("TEST · 👍")


def test_listing_followup_fits_in_one_ntfy_message():
    long_ad = {**AD, "description": "Popis karty se spoustou detailů. " * 300}
    _, message = followup.format_listing(long_ad, fair_price(), 6000, TRIAGE, MESSAGE, 4100, NOW)
    assert len(message.encode()) <= followup.NTFY_LIMIT
    assert "[…]" in message and message.endswith(MESSAGE)  # the message is never cut
    desc = message.split("📝 ", 1)[1].split("\n\n💬", 1)[0]
    assert len(desc) <= followup.MAX_DESCRIPTION + 4


def test_listing_followup_without_triage_or_price():
    _, message = followup.format_listing(
        {**AD, "model": None, "views": None}, None, None, None, "x", 4100, NOW
    )
    assert message.splitlines()[0] == "💰 4 500 Kč" and "🤖 not triaged" in message
    _, message = followup.format_listing(
        AD, None, None, None, "x", 0, NOW, place="📍 Ostrava · 280 km (ships)"
    )
    assert "💬" not in message and message.splitlines()[2].startswith("📍 Ostrava · 280 km (ships) · posted")


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


def test_disk_followup_and_message():
    from deal_hunter.storage import normalize

    title = "3× Toshiba MG09 18TB enterprise HDD, záruka do 4. 2. 2027"
    r = normalize(title, "Cena 13 500 Kč/ks.")
    ad = {
        **AD,
        "title": title,
        "price_czk": 13500,
        "model": r.model,
        "category": "hdd",
        "features": r.features,
    }
    message = seller_message(ad)
    assert message == f"Dobrý den, zajímám se o disky Toshiba MG09 18TB. {ASK}"
    fp = FairPrice(r.model, "hdd", 9, 9, 15000, 14000, 16000, 0.5, "medium")
    fp.new_price, fp.new_source, fp.new_detail = 17503, "zbozi", "1 shop"
    disk = {"power_on_hours": 12000, "reallocated_or_pending": 0}
    _, text = followup.format_listing(ad, fp, 15000, {**TRIAGE, "disk": disk}, message, 12500, NOW)
    assert "💾 3× 18 TB · 750 Kč/TB (new ~972 Kč/TB) · SATA? · CMR ✓ · SMART: 12 000 h, 0 reallocated" in text
    assert "💬 If negotiating: ~12 500 Kč" in text and text.endswith(message)
