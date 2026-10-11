"""Parsers for Bazoš.cz pages: the category listing, the RSS feed and an ad's detail page.

Pure functions over HTML/XML text, so they are tested against saved fixtures (tests/fixtures/).
We only ever read URLs that robots.txt allows: category pages, the RSS feed and /inzerat/ pages.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import unquote_plus

from selectolax.lexbor import LexborHTMLParser, LexborNode

BASE_URL = "https://pc.bazos.cz"
PAGE_SIZE = 20  # ads per category page; page N lives at /<section>/<N*20>/


@dataclass(frozen=True)
class Section:
    """A Bazoš PC subcategory we watch, and how it is read and normalized."""

    slug: str  # URL path: /graficka/, /pc/, /notebook/, /hdd/
    rss_cat: int  # www.bazos.cz/rss.php?rub=pc&cat=<n>
    pages: int  # category pages read by every scrape cycle (the RSS feed carries the newest 50)
    kind: str  # which normalizer reads its titles: gpu | office | storage
    details: str  # 'all': fetch every new ad's page; 'in_scope': skip ads the normalizer excludes


SECTIONS = {
    "graficka": Section("graficka", 4, 2, "gpu", "all"),  # Grafické karty
    "pc": Section("pc", 12, 1, "office", "in_scope"),  # PC, Počítače
    "notebook": Section("notebook", 10, 1, "office", "in_scope"),  # Notebooky
    "hdd": Section("hdd", 5, 1, "storage", "in_scope"),  # Hard disky, SSD
}
CATEGORY = "graficka"  # the original section; listings scraped before v0.3.0 all come from it
RSS_URL = "https://www.bazos.cz/rss.php?rub=pc&cat=4"  # PC > Grafické karty

AD_URL_RE = re.compile(r"/inzerat/(\d+)/")
DATE_RE = re.compile(r"\[\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*\]")
PSC_RE = re.compile(r"\b(\d{3}\s?\d{2})\b")
DIGITS_RE = re.compile(r"\d+")

# Non-numeric prices Bazoš shows instead of an amount.
PRICE_KINDS = {
    "dohodou": "negotiable",
    "v textu": "in_text",
    "zdarma": "free",
    "nabídněte": "offer",
    "nabidnete": "offer",
}


@dataclass
class Listing:
    """One ad. Category pages fill most fields; the detail page adds description, images, category."""

    id: int
    url: str
    title: str
    price_czk: int | None = None
    price_kind: str = "unknown"
    location: str | None = None
    psc: str | None = None
    posted_at: date | None = None
    views: int | None = None
    seller: str | None = None
    description: str | None = None
    images: list[str] = field(default_factory=list)
    raw_category: str | None = None
    section: str = CATEGORY  # the category page/feed we found it on (graficka | pc | notebook | hdd)


def category_url(page: int, section: str = CATEGORY) -> str:
    """Category page `page` (0-based): /graficka/, /graficka/20/, /graficka/40/, ..."""
    return f"{BASE_URL}/{section}/" + (f"{page * PAGE_SIZE}/" if page else "")


def rss_url(section: str = CATEGORY) -> str:
    return f"https://www.bazos.cz/rss.php?rub=pc&cat={SECTIONS[section].rss_cat}"


def ad_id(url: str) -> int | None:
    m = AD_URL_RE.search(url)
    return int(m.group(1)) if m else None


def parse_price(text: str | None) -> tuple[int | None, str]:
    """'  11 000 Kč' -> (11000, 'fixed'); 'Dohodou' -> (None, 'negotiable'); '' -> (None, 'unknown')."""
    if not text:
        return None, "unknown"
    clean = " ".join(text.split()).lower()
    for word, kind in PRICE_KINDS.items():
        if word in clean:
            return None, kind
    digits = "".join(DIGITS_RE.findall(clean.replace("kč", "")))
    if digits:
        return int(digits), "fixed"
    return None, "unknown"


def parse_date(text: str | None) -> date | None:
    """'- TOP - [9.10. 2026]' -> date(2026, 10, 9)."""
    m = DATE_RE.search(text or "")
    if not m:
        return None
    day, month, year = map(int, m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _text(node: LexborNode | None, sep: str = "") -> str:
    return node.text(separator=sep).strip() if node is not None else ""


def _seller_from_onclick(node: LexborNode | None) -> str | None:
    """The 'rating' action carries the seller's display name: odeslatakci('rating','1','2','David')."""
    if node is None:
        return None
    for span in node.css("span[onclick]"):
        m = re.search(r"odeslatakci\('rating','\d+','\d+','([^']*)'\)", span.attributes.get("onclick") or "")
        if m:
            return unquote_plus(m.group(1)).strip() or None
    return None


def _split_location(parts: list[str]) -> tuple[str | None, str | None]:
    """['Praha 4', '140 00'] (in either order) -> ('Praha 4', '140 00')."""
    location, psc = None, None
    for part in (p.strip() for p in parts):
        if not part:
            continue
        if psc is None and PSC_RE.fullmatch(part):
            psc = part if " " in part else f"{part[:3]} {part[3:]}"
        elif location is None:
            location = part
    return location, psc


def parse_category(html: str) -> list[Listing]:
    """Every ad on a category page, in page order (TOP-promoted ads come first)."""
    tree = LexborHTMLParser(html)
    out = []
    for box in tree.css("div.inzeraty"):
        link = box.css_first("h2.nadpis a") or box.css_first(".nadpis a")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        listing_id = ad_id(href)
        if listing_id is None:
            continue
        price, kind = parse_price(_text(box.css_first("div.inzeratycena")))
        location, psc = _split_location(_text(box.css_first("div.inzeratylok"), "\n").split("\n"))
        views = DIGITS_RE.search(_text(box.css_first("div.inzeratyview")).replace(" ", ""))
        thumb = box.css_first("img.obrazek")
        out.append(
            Listing(
                id=listing_id,
                url=BASE_URL + href if href.startswith("/") else href,
                title=_text(link),
                price_czk=price,
                price_kind=kind,
                location=location,
                psc=psc,
                posted_at=parse_date(_text(box.css_first("span.velikost10"))),
                views=int(views.group()) if views else None,
                seller=_seller_from_onclick(box.css_first("div.inzeratyakce")),
                description=_text(box.css_first("div.popis")) or None,
                images=[thumb.attributes["src"]] if thumb is not None and thumb.attributes.get("src") else [],
            )
        )
    return out


@dataclass
class RssItem:
    id: int
    url: str
    title: str
    price_czk: int | None
    price_kind: str
    published: datetime | None


def parse_rss(xml: str) -> list[RssItem]:
    """Items of the category RSS feed. The title ends with ': <price>' ('...: 2 000', '...: V textu')."""
    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    out = []
    for item in root.iter("item"):
        url = (item.findtext("link") or "").strip()
        listing_id = ad_id(url)
        if listing_id is None:
            continue
        raw_title = (item.findtext("title") or "").strip()
        title, sep, price_text = raw_title.rpartition(": ")
        if not sep:
            title, price_text = raw_title, ""
        price, kind = parse_price(price_text)
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = None
        out.append(RssItem(listing_id, url, title.strip(), price, kind, published))
    return out


def is_removed_page(html: str) -> bool:
    """Bazoš answers a deleted ad with HTTP 410 and 'Inzerát je již vymazán'."""
    return "Inzerát je již vymazán" in html or "<title>Neexistuje" in html


def parse_detail(html: str, url: str) -> Listing | None:
    """The full ad from its detail page, or None if the page isn't an ad (removed, error page)."""
    if is_removed_page(html):
        return None
    tree = LexborHTMLParser(html)
    h1 = tree.css_first("h1.nadpisdetail")
    listing_id = ad_id(url)
    if h1 is None or listing_id is None:
        return None

    header = tree.css_first("div.inzeratydetnadpis")
    desc_node = tree.css_first("div.popisdetail")
    description = desc_node.text(separator="").strip() if desc_node is not None else None

    images = []
    for img in tree.css("div.carousel img"):
        src = img.attributes.get("data-flickity-lazyload") or img.attributes.get("src")
        if src and src not in images:
            images.append(src)

    fields: dict[str, LexborNode] = {}
    info = tree.css_first("td.listadvlevo")
    if info is not None:
        for row in info.css("tr"):
            cells = row.css("td")
            if len(cells) >= 2:
                label = _text(cells[0]).rstrip(":").strip().lower()
                fields[label] = row

    price, kind = parse_price(_text(fields["cena"].css_first("span[translate]")) if "cena" in fields else "")
    location = psc = None
    if "lokalita" in fields:
        location, psc = _split_location([_text(a) for a in fields["lokalita"].css("a")])
    views = None
    if "vidělo" in fields:
        m = DIGITS_RE.search(_text(fields["vidělo"].css("td")[-1]).replace(" ", ""))
        views = int(m.group()) if m else None
    seller = None
    if "jméno" in fields:
        seller = _text(fields["jméno"].css("td")[-1]) or None

    category = None
    crumbs = tree.css("div.drobky a")
    if crumbs:
        href = crumbs[-1].attributes.get("href") or ""
        m = re.search(r"bazos\.cz/([^/]+)/?$", href)
        category = m.group(1) if m else _text(crumbs[-1])

    return Listing(
        id=listing_id,
        url=url,
        title=_text(h1),
        price_czk=price,
        price_kind=kind,
        location=location,
        psc=psc,
        posted_at=parse_date(_text(header)),
        views=views,
        seller=seller,
        description=description,
        images=images,
        raw_category=category,
    )
