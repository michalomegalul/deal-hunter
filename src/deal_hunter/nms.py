"""Parsers for NMS, the state property marketplace of ÚZSVM (nms.gov.cz, also nabidkamajetkustatu.gov.cz).

Government offices sell what they no longer need there in electronic auctions, IT equipment included:
single PCs, but also lots like "Soubor 14 ks HW vybavení - Notebooky" (14 Dell Latitude 5511).
Ended auctions show the price the lot really sold for ("Konečná cena"), which Bazoš never does.

How the site is read (no robots.txt is served; we keep a slow, honest pace anyway):

- The list page (/Home/Auctions) is rendered in the browser from a JSON API. We call that same API,
  `POST /api/Property/AuctionList`, with the body the page itself sends, filtered to the IT
  category ("Výpočetní technika vč. příslušenství", id 4): one request returns every active IT lot.
  The same call with `ListType=closed` returns the newest ended IT lots with their final price.
- A lot's detail page (/Home/AuctionDetail/<id>) is server-rendered HTML: description, inspection,
  attachments, current or final price and the auction window.
- Attachments (/api/Property/Attachment/<uuid>) are often the actual item list (a PDF with a row
  per laptop). We keep their URLs and read the text of at most two list-like PDFs per lot.

Pure functions over the responses, tested against saved fixtures (tests/fixtures/nms_*).
"""

import html
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser, LexborNode

SOURCE = "nms"
BASE_URL = "https://nms.gov.cz"
LIST_API = f"{BASE_URL}/api/Property/AuctionList"
IT_CATEGORY = 4  # "Výpočetní technika vč. příslušenství"
PRAGUE = ZoneInfo("Europe/Prague")


def detail_url(lot_id: str | int) -> str:
    return f"{BASE_URL}/Home/AuctionDetail/{lot_id}"


def list_body(list_type: str = "active", page: int = 1, page_size: int = 50) -> dict:
    """The JSON the list page posts (see its `auctionListOptions`), for the IT category only."""
    return {
        "ListType": list_type,  # active | closed
        "Page": page,
        "PageSize": page_size,
        "Order": "Default",
        "OrderDesc": "true",
        "CategoryId": IT_CATEGORY,
        "Fulltext": "",
        "OrgId": "",
        "OrganizationType": 0,
        "OrganizationId": 0,
        "LocalityId": 0,
        "MunicipialityId": 0,
        "CadastreId": 0,
        "AuctionModeId": 0,
        "ContactZipCode": "",
        "PropertyAuthor": "",
    }


@dataclass
class Lot:
    """One auction. The list API fills the summary; the detail page adds the rest."""

    lot_id: str
    url: str
    title: str
    status: str  # prepared | active | ended (sold) | unsold
    category: str | None = None
    price: int | None = None  # current price (active) or the final price (ended)
    final_price: int | None = None
    start_price: int | None = None  # "Nejnižší podání"
    min_increment: int | None = None
    bids: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    institution: str | None = None
    district: str | None = None
    zip_code: str | None = None
    description: str | None = None
    inspection: str | None = None
    address: str | None = None  # the selling office's address (pickup is usually there)
    conditions: str | None = None
    attachments: list[dict] = field(default_factory=list)  # [{"name", "url"}]
    images: list[str] = field(default_factory=list)


# --- small helpers --------------------------------------------------------------------------------

PRICE_RE = re.compile(r"\d[\d\s\xa0]*(?:,\d+)?")


def parse_price(text) -> int | None:
    """'18 500,00' / '7\xa0600,00 Kč' -> 18500 / 7600."""
    if text is None:
        return None
    m = PRICE_RE.search(str(text))
    if not m:
        return None
    whole, _, frac = re.sub(r"[\s\xa0]", "", m.group()).partition(",")
    value = int(whole) + (float(f"0.{frac}") if frac else 0)
    return round(value)


def utc(text: str | None) -> datetime | None:
    """The list API's dates ('2026-10-26T11:00:00') are UTC without a zone."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19]).replace(tzinfo=UTC)
    except ValueError:
        return None


WINDOW_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})")


def parse_window(text: str) -> tuple[datetime | None, datetime | None]:
    """'30.09.2026 10:00 - 14.10.2026 10:00' (Prague time) -> (start, end) in UTC."""
    found = []
    for d, mo, y, h, mi in WINDOW_RE.findall(text or ""):
        local = datetime(int(y), int(mo), int(d), int(h), int(mi), tzinfo=PRAGUE)
        found.append(local.astimezone(UTC))
    return (found[0] if found else None), (found[1] if len(found) > 1 else None)


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = html.unescape(text).replace("\xa0", " ").replace("\r", "")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out = "\n".join(line for line in lines if line).strip()
    return out or None


# --- the list API ---------------------------------------------------------------------------------


def list_status(item: dict) -> str:
    """AuctionStatus 0 = prepared, 1 = running, 2 = ended ('Realizovaná cena'), 3 = not realised.

    An ended auction without a price or without bids found no buyer.
    """
    code = item.get("AuctionStatus")
    if code == 0:
        return "prepared"
    if code == 1:
        return "active"
    if code == 2 and item.get("Price") and (item.get("NbrOfBids") or 0) > 0:
        return "ended"
    return "unsold"


def parse_list(data: dict) -> list[Lot]:
    """Lots from one AuctionList response (the base64 thumbnails are ignored)."""
    lots = []
    for item in data.get("Auctions") or []:
        lot_id = str(item["Id"])
        status = list_status(item)
        price = parse_price(item.get("Price"))
        lots.append(
            Lot(
                lot_id=lot_id,
                url=detail_url(lot_id),
                title=" ".join(str(item.get("Name") or "").split()),
                status=status,
                category=item.get("CategoryName"),
                price=price,
                final_price=price if status == "ended" else None,
                min_increment=parse_price(item.get("MinimalIncrement")),
                bids=item.get("NbrOfBids"),
                start_at=utc(item.get("StartDate")),
                end_at=utc(item.get("EndDate")),
                institution=(item.get("Organization") or {}).get("u04Name"),
                district=item.get("DistrictName"),
                zip_code=(item.get("ContactZipCode") or "").strip() or None,
                description=_clean(item.get("Description")),
            )
        )
    return lots


# --- the detail page ------------------------------------------------------------------------------


def _value_of(label: LexborNode) -> LexborNode | None:
    """The value cell next to a `<label>`: the label sits in a column div, the value in the next one."""
    cell = label.parent
    while cell is not None and cell.tag != "div":
        cell = cell.parent
    if cell is None:
        return None
    node = cell.next
    while node is not None and node.tag != "div":
        node = node.next
    return node


def _text(node: LexborNode | None) -> str | None:
    if node is None:
        return None
    return _clean(node.text(separator="\n"))


def detail_fields(doc: LexborHTMLParser) -> dict[str, str]:
    """{label without the colon: value text} for every labelled row on the page (first one wins)."""
    out: dict[str, str] = {}
    for label in doc.css("label"):
        name = " ".join(label.text().split()).rstrip(":").strip()
        if not name or name in out:
            continue
        value = _text(_value_of(label))
        if value:
            out[name] = value
    return out


def parse_detail(text: str, lot_id: str | int) -> Lot:
    """Everything the detail page says about one lot."""
    doc = LexborHTMLParser(text)
    fields = detail_fields(doc)
    heading = next((h.text(strip=True) for h in doc.css("h3.text-primary")), "")
    final = parse_price(fields.get("Konečná cena"))
    bids = parse_price(fields.get("Počet příhozů"))
    if "ukončen" in heading.lower():
        status = "ended" if final and (bids or 0) > 0 else "unsold"
    elif "aktivní" in heading.lower():
        status = "active"
    elif "připravovan" in heading.lower():
        status = "prepared"
    else:
        status = heading.lower() or "unknown"
    start_at, end_at = parse_window(fields.get("Termín aukce", ""))
    title_node = doc.css_first("h2")
    address = None
    for label in doc.css("label"):
        if label.text(strip=True).startswith("Kontaktní údaje"):
            cell = _value_of(label)
            ps = cell.css("p") if cell is not None else []
            address = _clean(ps[-1].text()) if ps else None
            break
    attachments, seen = [], set()
    for a in doc.css('li a[href^="/api/Property/Attachment/"]'):
        href = a.attributes.get("href") or ""
        name = " ".join(a.text().split())
        if name in seen:
            continue  # the same files are listed under "k nabídce" and "k aukci"
        seen.add(name)
        attachments.append({"name": name, "url": BASE_URL + href})
    images = []
    for a in doc.css('a[data-lightbox][href^="/api/Property/Attachment/"]'):
        url = BASE_URL + (a.attributes.get("href") or "")
        if url not in images:
            images.append(url)
    lot_id = str(lot_id)
    return Lot(
        lot_id=lot_id,
        url=detail_url(lot_id),
        title=" ".join(title_node.text().split()) if title_node else "",
        status=status,
        category=fields.get("Kategorie"),
        price=final if final else parse_price(fields.get("Aktuální cena")),
        final_price=final if status == "ended" else None,
        start_price=parse_price(fields.get("Nejnižší podání")),
        min_increment=parse_price(fields.get("Minimální navýšení ceny")),
        bids=bids,
        start_at=start_at,
        end_at=end_at,
        institution=fields.get("Instituce"),
        description=fields.get("Popis"),
        inspection=fields.get("Možnosti prohlídky"),
        address=address,
        conditions=(fields.get("Podmínky aukce") or "")[:2000] or None,
        attachments=attachments,
        images=images,
    )


# --- attachments ----------------------------------------------------------------------------------

LIST_LIKE = re.compile(
    r"seznam|soupis|specifikac|p[rř][ií]loh|polo[zž]k|ntb|notebook|po[cč][ií]ta[cč]|\bpc\b|dell|lenovo|\bhp\b"
    r"|fujitsu|acer|asus|thinkpad|latitude|optiplex|elitebook|probook|monitor",
    re.IGNORECASE,
)
NOT_LIST = re.compile(r"smlouv|vyhl|podm[ií]nk|plná moc|formul|prohl[aá][sš]en|kauc", re.IGNORECASE)
MAX_PDFS = 2
MAX_PDF_BYTES = 4_000_000
MAX_PDF_PAGES = 10


def list_attachments(attachments: list[dict]) -> list[dict]:
    """The (at most two) PDFs that look like an item list, not the auction notice or the contract."""
    picked = []
    for a in attachments:
        name = a.get("name") or ""
        if not name.lower().endswith(".pdf") or NOT_LIST.search(name):
            continue
        if LIST_LIKE.search(name):
            picked.append(a)
    return picked[:MAX_PDFS]


def pdf_text(content: bytes) -> str:
    """The text of a PDF's first pages (empty for a scan or a broken file)."""
    from pypdf import PdfReader

    if not content or len(content) > MAX_PDF_BYTES:
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
    except Exception:  # noqa: BLE001 - a broken PDF just means no list text
        return ""


SERIAL = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9-]{8,}\b|\b\d{8,}\b")


def compact_list(text: str, limit: int = 4000) -> str:
    """An item list made short for the LLM: serial numbers out, identical rows counted.

    'PC přenosný ACER Extensa 2540 NXEFHEC0057290D6B03400' x 18 -> '18× PC přenosný ACER Extensa 2540'.
    Rows without a letter (row numbers, years on their own line) are dropped.
    """
    counts: dict[str, int] = {}
    for line in text.splitlines():
        line = " ".join(SERIAL.sub(" ", line).split())
        if len(line) < 4 or not re.search(r"[^\W\d_]", line):
            continue
        counts[line] = counts.get(line, 0) + 1
    out = "\n".join(f"{n}× {line}" if n > 1 else line for line, n in counts.items())
    return out[:limit]
