"""NMS (nms.gov.cz) parsers against real responses saved on 2026-10-09."""

import json
from datetime import UTC, datetime

import httpx

from deal_hunter import nms
from deal_hunter.http import PoliteClient
from tests.conftest import FIXTURES


def load_list(name):
    return json.loads((FIXTURES / f"nms_list_{name}.json").read_text(encoding="utf-8"))


def detail(lot_id):
    return nms.parse_detail((FIXTURES / f"nms_detail_{lot_id}.html").read_text(encoding="utf-8"), lot_id)


def test_list_body_filters_to_it():
    body = nms.list_body("closed", 2)
    assert body["CategoryId"] == 4 and body["ListType"] == "closed" and body["Page"] == 2


def test_active_list():
    lots = nms.parse_list(load_list("active"))
    assert len(lots) == 14
    assert all(lot.status == "active" for lot in lots)
    by_id = {lot.lot_id: lot for lot in lots}
    notebooks = by_id["62785"]
    assert notebooks.title == "Aukce 19ks notebooků"
    assert notebooks.price == 7600 and notebooks.bids == 1 and notebooks.min_increment == 300
    assert notebooks.end_at == datetime(2026, 10, 14, 8, 0, tzinfo=UTC)  # 10:00 CEST
    assert notebooks.url == "https://nms.gov.cz/Home/AuctionDetail/62785"
    assert by_id["62222"].price == 21650 and by_id["62222"].bids == 6
    assert by_id["62881"].end_at == datetime(2026, 10, 26, 11, 0, tzinfo=UTC)  # 12:00 CET, after DST


def test_closed_list_final_prices():
    lots = {lot.lot_id: lot for lot in nms.parse_list(load_list("closed"))}
    assert len(lots) == 50
    sold = lots["62388"]
    assert (sold.status, sold.final_price, sold.bids) == ("ended", 63000, 1)
    assert lots["62385"].status == "unsold" and lots["62385"].final_price is None
    assert lots["61608"].final_price == 2738


def test_parse_price():
    assert nms.parse_price("18 500,00") == 18500
    assert nms.parse_price("7\xa0600,00 Kč") == 7600
    assert nms.parse_price("2 738,60") == 2739
    assert nms.parse_price(None) is None and nms.parse_price("-") is None


def test_detail_active_lot_with_attachments():
    lot = detail(62785)
    assert lot.status == "active" and lot.title == "Aukce 19ks notebooků"
    assert (lot.price, lot.start_price, lot.min_increment, lot.bids) == (7600, 7600, 300, 1)
    assert lot.final_price is None
    assert lot.start_at == datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    assert lot.end_at == datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
    assert lot.institution == "Generální inspekce bezpečnostních sborů"
    assert "bez paměťových disků" in lot.description
    assert "Šilhavým" in lot.inspection
    assert lot.address == "Skokanská 2311/3, 16900 Praha"
    names = [a["name"] for a in lot.attachments]
    assert names[1] == "3-příloha č.1 seznam nab.majetku NTB.pdf" and len(names) == 4
    assert lot.attachments[1]["url"].startswith("https://nms.gov.cz/api/Property/Attachment/")
    assert [a["name"] for a in nms.list_attachments(lot.attachments)] == [
        "3-příloha č.1 seznam nab.majetku NTB.pdf"
    ]


def test_detail_single_pc():
    lot = detail(62495)
    assert lot.title == "Prodej PC Dell Optiplex 3050 - 2863"
    assert (lot.status, lot.price, lot.bids, lot.min_increment) == ("active", 1700, 0, 20)
    assert "I5-6500" in lot.description and "bez OS" in lot.description
    assert nms.list_attachments(lot.attachments) == []  # the notice and the contract only


def test_detail_ended_with_final_price():
    lot = detail(62388)
    assert lot.status == "ended"
    assert lot.final_price == 63000 and lot.price == 63000 and lot.start_price == 63000
    assert lot.end_at == datetime(2026, 9, 28, 17, 0, tzinfo=UTC)
    # the same three files are listed twice on the page; kept once
    assert [a["name"] for a in lot.attachments] == [
        "5.část aukce - NTB Dell Latitude 5511 (14).pdf",
        "aukcni_vyhlaška_IT HW - NTB 5.pdf",
        "návrh kupní smlouvy.doc",
    ]
    assert [a["name"] for a in nms.list_attachments(lot.attachments)] == [
        "5.část aukce - NTB Dell Latitude 5511 (14).pdf"
    ]
    assert len(lot.images) == 2


def test_detail_ended_single_item_and_unsold_lot():
    hp = detail(61608)
    assert (hp.status, hp.final_price, hp.start_price, hp.bids) == ("ended", 2738, 2688, 1)
    assert "EliteBook 8540p" in hp.description
    unsold = detail(62385)
    assert unsold.status == "unsold" and unsold.final_price is None and unsold.start_price == 99000


def test_pdf_item_list_is_compacted():
    text = nms.pdf_text((FIXTURES / "nms_attachment_62785.pdf").read_bytes())
    assert "NXEFHEC0057290D6B03400" in text
    short = nms.compact_list(text)
    assert "18× PC přenosný ACER Extensa 2540" in short
    assert "NTB ACER ASPIRE E1-731 + brašna" in short
    assert "NXEF" not in short and len(short) < 200
    assert nms.pdf_text(b"not a pdf") == ""


def test_polite_client_posts_json():
    seen = []

    def handler(request):
        seen.append((request.method, json.loads(request.content), request.headers["User-Agent"]))
        return httpx.Response(200, json={"Auctions": []}, headers={"Content-Type": "application/json"})

    client = PoliteClient("ua", min_delay=5, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    page = client.post_json(nms.LIST_API, nms.list_body())
    assert page.status == 200 and json.loads(page.text) == {"Auctions": []}
    assert seen[0][0] == "POST" and seen[0][1]["CategoryId"] == 4 and seen[0][2] == "ua"
    assert page.content_type == "application/json"
