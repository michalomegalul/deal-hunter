"""Distance from home: Czech postal codes (PSČ) and town names to coordinates, and the pickup rule.

Every Bazoš ad shows a town and a PSČ. `cz_postcodes.csv` maps each PSČ to a point (the median of
its GeoNames places) and lists the places that use it. It is built from the GeoNames postal code
dump for CZ (https://download.geonames.org/export/zip/CZ.zip, CC BY 4.0, credited in the README)
with `build_table`; ~2 700 codes.

Lookup order for an ad: its PSČ; else its town name (`location`, e.g. "Praha 4", "Ústí nad Labem"),
matched against the place names (and the town part of "Brno-Trnitá" / "Praha 4-Krč"); a name used
by places far apart ("Lhota") is ambiguous and gives nothing. No match = unknown distance.

Distances are straight-line (great circle) kilometres; by road it is typically 20-30 % more.
"""

import csv
import io
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from importlib import resources

TABLE = "cz_postcodes.csv"
PRAGUE_CENTRE = (50.0755, 14.4378)
AMBIGUOUS_KM = 25.0  # a town name is a location when most of its places lie within this of their median
PRAGUE_RADIUS_KM = 25.0  # every Prague PSČ (1xx xx) lies within this of the centre
TOWN_SHARE = 0.8  # "most": GeoNames has a few misplaced points (e.g. a Prague district in South Bohemia)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def plain(text: str) -> str:
    """'Ústí nad Labem' -> 'usti nad labem'."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def norm_psc(psc: str | None) -> str | None:
    digits = re.sub(r"\D", "", psc or "")
    return digits if len(digits) == 5 else None


def town_keys(place: str) -> list[str]:
    """The names a place answers to: 'Praha 4-Krč' -> ['praha 4 krc', 'praha 4', 'praha']."""
    keys = [plain(place)]
    if "-" in place:
        keys.append(plain(place.split("-", 1)[0]))
    m = re.match(r"^(.*?)\s+\d+$", place.split("-", 1)[0].strip())
    if m:
        keys.append(plain(m.group(1)))
    return [k for i, k in enumerate(keys) if k and k not in keys[:i]]


def _median(values: list[float]) -> float:
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def median_point(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Per-coordinate median: robust to the odd misplaced GeoNames point."""
    return _median([p[0] for p in points]), _median([p[1] for p in points])


# --- the bundled table ------------------------------------------------------------------------------


def build_table(geonames_txt: str) -> str:
    """The bundled CSV from GeoNames' CZ.txt (tab separated: country, code, place, ..., lat, lon, acc)."""
    points: dict[str, list[tuple[float, float]]] = defaultdict(list)
    places: dict[str, list[str]] = defaultdict(list)
    for line in geonames_txt.splitlines():
        cols = line.split("\t")
        if len(cols) < 11 or cols[0] != "CZ":
            continue
        psc = norm_psc(cols[1])
        if psc is None:
            continue
        points[psc].append((float(cols[9]), float(cols[10])))
        if cols[2] not in places[psc]:
            places[psc].append(cols[2])
    out = io.StringIO()
    out.write(
        "# PSČ -> point (median of its places) and place names, from the GeoNames postal code dump for CZ\n"
        "# (https://download.geonames.org/export/zip/, CC BY 4.0, www.geonames.org). Built by geo.build_table.\n"
    )
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["psc", "lat", "lon", "places"])
    centres = {psc: median_point(pts) for psc, pts in points.items()}
    # Prague's codes (1xx xx) all lie within the city, but GeoNames puts a few of them (153 00 Radotín,
    # 156 00 Zbraslav, 197 00 Kbely) 70-160 km away. Those get the median of Prague's codes. Elsewhere the
    # first digits are not a tight area (66x xx is both Brno-venkov and Znojmo), so nothing is moved.
    prague = median_point([pt for psc, pt in centres.items() if psc.startswith("1")])
    for psc in sorted(points):
        lat, lon = centres[psc]
        if psc.startswith("1") and haversine_km((lat, lon), PRAGUE_CENTRE) > PRAGUE_RADIUS_KM:
            lat, lon = prague
        w.writerow([psc, f"{lat:.4f}", f"{lon:.4f}", "|".join(places[psc])])
    return out.getvalue()


@dataclass(frozen=True)
class Gazetteer:
    by_psc: dict[str, tuple[float, float]]
    by_town: dict[str, tuple[float, float]]  # unambiguous town names only


@cache
def gazetteer() -> Gazetteer:
    text = resources.files("deal_hunter").joinpath(TABLE).read_text(encoding="utf-8")
    rows = csv.DictReader(line for line in text.splitlines() if not line.startswith("#"))
    by_psc: dict[str, tuple[float, float]] = {}
    towns: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        point = (float(row["lat"]), float(row["lon"]))
        by_psc[row["psc"]] = point
        seen = set()
        for place in row["places"].split("|"):
            for key in town_keys(place):
                if key not in seen:
                    towns[key].append(point)
                    seen.add(key)
    by_town = {}
    for key, pts in towns.items():
        centre = median_point(pts)
        near = sum(haversine_km(centre, p) <= AMBIGUOUS_KM for p in pts)
        if near >= TOWN_SHARE * len(pts):
            by_town[key] = centre
    return Gazetteer(by_psc, by_town)


def locate(psc: str | None, location: str | None) -> tuple[tuple[float, float] | None, str]:
    """(point, how): how = 'psc' | 'town' | 'unknown'."""
    g = gazetteer()
    code = norm_psc(psc)
    if code and code in g.by_psc:
        return g.by_psc[code], "psc"
    if location:
        for key in town_keys(location.strip()):
            if key in g.by_town:
                return g.by_town[key], "town"
    return None, "unknown"


def home_point(psc: str | None, lat: float | None, lon: float | None) -> tuple[float, float]:
    """HUNTER_HOME_PSC if it is a known code, else HUNTER_HOME_LAT/LON, else Prague centre."""
    code = norm_psc(psc)
    if code and code in gazetteer().by_psc:
        return gazetteer().by_psc[code]
    if lat is not None and lon is not None:
        return (lat, lon)
    return PRAGUE_CENTRE


def distance_km(home: tuple[float, float], psc: str | None, location: str | None) -> tuple[float | None, str]:
    """(km from home or None, how it was located)."""
    point, how = locate(psc, location)
    if point is None:
        return None, how
    return round(haversine_km(home, point), 1), how


# --- shipping -----------------------------------------------------------------------------------------

# The ad offers to send the item. "pošlu" alone is not enough ("pošlu fotky", "pošlu SMART výpis").
PHOTO_AFTER = r"(?! (?:\w+ )?(?:fot|vide|screen|smart|info|vypis|cisl|odkaz|link|sms|mail|detail))"
SHIPS = re.compile(
    r"zasilkovn|balikovn|\bppl\b|\bdpd\b|\bgls\b|dobirk|postou|\bposta\b|\bzaslani\b|\bzasilani\b"
    r"|\bpostovn|\bdoprav[au]\b|\bship"
    rf"|\b(poslu|posilam|poslat|zaslu|zaslat|zasilam|odeslu|odeslani|odeslat){PHOTO_AFTER}\b"
)
NO_SHIPPING = re.compile(
    r"\bne(zasilam|posilam|zaslu|poslu|odesilam|zasilame|posilame)\b|\b(pouze|jen|jenom|only|vyhradne)"
    r" (osobni|osobne)|\bbez (zasilani|zaslani|posilani)\b|zasilani (neni mozne|nenabizim)"
    r"|\bnelze (zaslat|poslat)\b"
)


def shipping_offer(text: str) -> bool | None:
    """True: the ad offers shipping; False: it rules shipping out; None: it doesn't say (ask the LLM)."""
    t = plain(text)
    if NO_SHIPPING.search(t):
        return False
    if SHIPS.search(t):
        return True
    return None


# --- the pickup rule ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DistanceRules:
    home: tuple[float, float] = PRAGUE_CENTRE
    pickup_km: float = 40.0
    pickup_km_by_category: tuple[tuple[str, float], ...] = ()  # (('hdd', 300.0), ...)
    shipping_czk: int = 150
    travel_czk_per_km: float = 0.0  # display only

    def radius(self, category: str | None) -> float:
        return dict(self.pickup_km_by_category).get(category or "", self.pickup_km)


def place_text(
    location: str | None,
    psc: str | None,
    km: float | None,
    ships: bool | None = None,
    travel_czk_per_km: float = 0.0,
) -> str:
    """'📍 Ostrava · 280 km (ships)'. Empty when nothing is known."""
    where = location or psc or ""
    bits = [where] if where else []
    if km is not None:
        dist = f"{km:.0f} km"
        if ships:
            dist += " (ships)"
        if travel_czk_per_km and km:
            cost = f"{round(2 * km * travel_czk_per_km):,}".replace(",", " ")
            dist += f", ~{cost} Kč there and back"
        bits.append(dist)
    elif where:
        bits.append("distance unknown" + (" (ships)" if ships else ""))
    return ("📍 " + " · ".join(bits)) if bits else ""


def home_key(home: tuple[float, float]) -> str:
    return f"{home[0]:.4f},{home[1]:.4f}"


def update_distances(conn, home: tuple[float, float]) -> int:
    """Compute distance_km for listings that don't have it for this home yet. Returns the count."""
    from deal_hunter.db import repository as repo

    key = home_key(home)
    rows = [
        (lid, *distance_km(home, psc, location))
        for lid, psc, location in repo.listings_needing_distance(conn, key)
    ]
    if rows:
        repo.save_distances(conn, rows, key)
    return len(rows)
