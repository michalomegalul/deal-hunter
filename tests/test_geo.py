"""Distance from home (bundled GeoNames PSČ table), shipping detection and the pickup rule."""

import pytest

from deal_hunter import alerts, geo
from deal_hunter.alerts import Rules
from deal_hunter.config import pickup_by_category
from deal_hunter.geo import DistanceRules

PRAGUE = geo.PRAGUE_CENTRE


def test_the_bundled_table_covers_the_country():
    g = geo.gazetteer()
    assert len(g.by_psc) > 2600
    assert {"10000", "11000", "60200", "70200", "40007"} <= set(g.by_psc)


@pytest.mark.parametrize(
    "psc, location, km, how",
    [
        ("702 00", "Ostrava", 275, "psc"),
        ("602 00", "Brno", 182, "psc"),
        ("684 01", "Vyškov", 206, "psc"),
        ("400 07", "Ústí nad Labem", 72, "psc"),
        ("140 00", "Praha 4", 1, "psc"),
        ("156 00", "Praha 5", 1, "psc"),  # GeoNames puts Zbraslav in South Bohemia: Prague's median instead
        (None, "Ostrava", 275, "town"),
        (None, "Praha 4", 5, "town"),  # 'Praha 4-Krč' etc. answer to 'praha 4'
        (None, "Praha", 1, "town"),
        (None, "Frýdek-Místek", 283, "town"),
        ("99999", "Hradec Králové", 100, "town"),  # unknown code: the town name still works
    ],
)
def test_distance_from_prague(psc, location, km, how):
    got, src = geo.distance_km(PRAGUE, psc, location)
    assert src == how and abs(got - km) <= 2


@pytest.mark.parametrize("location", ["Lhota", "Nowhere", "", None])
def test_ambiguous_or_unknown_places_have_no_distance(location):
    assert geo.distance_km(PRAGUE, None, location) == (None, "unknown")


def test_home_from_psc_or_coordinates():
    assert geo.home_point("602 00", None, None) == geo.gazetteer().by_psc["60200"]
    assert geo.home_point(None, 49.2, 16.6) == (49.2, 16.6)
    assert geo.home_point("nonsense", None, None) == PRAGUE


def test_build_table_from_geonames_rows():
    geonames = [
        "CZ\t100 00\tPraha 10-Malešice\tHlavní město Praha\t52\t\t\t\t\t50.0847\t14.5094\t4",
        "CZ\t100 00\tVršovice\tHlavní město Praha\t52\t\t\t\t\t50.0673\t14.4603\t4",
        "CZ\t110 00\tPraha 1-Staré Město\tHlavní město Praha\t52\t\t\t\t\t50.0870\t14.4202\t4",
        "CZ\t120 00\tPraha 2-Nové Město\tHlavní město Praha\t52\t\t\t\t\t50.0783\t14.4209\t4",
        "CZ\t156 00\tZbraslav\tHlavní město Praha\t52\t\t\t\t\t48.6333\t14.4333\t4",
        "SK\t811 01\tBratislava\t\t\t\t\t\t\t48.14\t17.10\t4",
    ]
    text = geo.build_table("\n".join(geonames))
    assert "CC BY 4.0" in text
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines[0] == "psc,lat,lon,places"
    assert lines[1] == "10000,50.0760,14.4848,Praha 10-Malešice|Vršovice"
    assert lines[4] == "15600,50.0772,14.4271,Zbraslav"  # misplaced Prague code -> Prague's median
    assert len(lines) == 5


@pytest.mark.parametrize(
    "text, ships",
    [
        ("Pošlu Zásilkovnou nebo osobní odběr Praha", True),
        ("Disky lze zaslat Balíkovnou/zásilkovnou dle platného ceníku.", True),
        ("Možno poslat na dobírku.", True),
        ("Posílám PPL, platba předem.", True),
        ("Zaslání Českou poštou možné.", True),
        ("Odeslání do druhého dne.", True),
        ("Osobní předání v Praze, kontrola při převzetí možná, neposílám.", False),
        ("Pouze osobní odběr Brno.", False),
        ("Jen osobně, nezasílám.", False),
        ("Pošlu fotky na mail.", None),
        ("Na požádání pošlu SMART výpis.", None),
        ("Prodám disk, plně funkční.", None),
    ],
)
def test_shipping_offer(text, ships):
    assert geo.shipping_offer(text) is ships


def test_place_text():
    assert geo.place_text("Ostrava", "702 00", 280.4, True) == "📍 Ostrava · 280 km (ships)"
    assert geo.place_text("Brno", "602 00", 182.0, False) == "📍 Brno · 182 km"
    assert geo.place_text("Lhota", None, None, None) == "📍 Lhota · distance unknown"
    assert geo.place_text(None, None, None) == ""
    assert geo.place_text("Kolín", None, 52, False, 4.0) == "📍 Kolín · 52 km, ~416 Kč there and back"


def test_pickup_radius_per_category_from_the_environment():
    env = {"HUNTER_PICKUP_KM_HDD": "300", "HUNTER_PICKUP_KM_OFFICE": "60", "HUNTER_PICKUP_KM_GPU": " "}
    assert pickup_by_category(env) == (("hdd", 300.0), ("office_laptop", 60.0), ("office_pc", 60.0))
    rules = DistanceRules(pickup_km_by_category=pickup_by_category(env))
    assert rules.radius("hdd") == 300 and rules.radius("gpu") == 40 and rules.radius(None) == 40


def test_hdd_pickup_radius_defaults_to_300_without_any_env():
    assert pickup_by_category({}) == (("hdd", 300.0),)  # disks ship easily
    assert pickup_by_category({"HUNTER_PICKUP_KM_OFFICE": "60"}) == (
        ("hdd", 300.0),
        ("office_laptop", 60.0),
        ("office_pc", 60.0),
    )
    assert pickup_by_category({"HUNTER_PICKUP_KM_HDD": "50"}) == (("hdd", 50.0),)  # still overridable


def rules(**kw):
    return Rules(
        enabled=True,
        min_score=0.12,
        max_per_day=5,
        min_samples=15,
        min_days=7,
        quiet_start=23,
        quiet_end=7,
        quiet_bypass=0.3,
        candidate_discount=0.2,
        distance=DistanceRules(**kw),
    )


def deal(**kw):
    base = {
        "category": "gpu",
        "price": 6500,
        "fair": 10000,
        "confidence": 0.6,
        "penalty": 1.0,
        "location": "Brno",
        "psc": "602 00",
        "distance_km": 182.0,
        "ships": False,
        "features": {},
    }
    return {**base, **kw}


def test_pickup_rule():
    r = rules()
    assert alerts.pickup_check(deal(distance_km=12.0, location="Praha 4"), r) is None
    assert alerts.pickup_check(deal(), r) == "out of range: Brno 182 km > 40 km, no shipping"
    assert alerts.pickup_check(deal(ships=True), r) is None  # 6 650 vs 10 000 still scores 0.2
    assert alerts.pickup_check(deal(distance_km=None, location="Lhota"), r) == (
        "out of range: Lhota: distance unknown, no shipping"
    )
    assert alerts.pickup_check(deal(distance_km=None, ships=True), r) is None
    # ships, but 150 Kč of postage eats a thin deal: 1 - 950/1000 = 5 % off
    thin = deal(price=800, fair=1000, ships=True)
    assert alerts.pickup_check(thin, r) == (
        "out of range: Brno 182 km > 40 km, ships, but score 0.030 after 150 Kč shipping"
    )
    # disks ship easily: a wider radius for the category
    assert alerts.pickup_check(deal(category="hdd"), rules(pickup_km_by_category=(("hdd", 300.0),))) is None
    # four disks in one parcel: the postage is split (850 vs 1000 doesn't score, 737.5 does)
    assert alerts.pickup_check(deal(price=700, fair=1000, ships=True), r) is not None
    four = deal(price=700, fair=1000, ships=True, features={"quantity": 4})
    assert alerts.pickup_check(four, r) is None
    # no distance rule at all
    assert alerts.pickup_check(deal(), rules(pickup_km=0)) is None


def test_ships_prefers_what_the_ad_says():
    from deal_hunter.triage import Triage

    assert alerts.ships({"title": "x", "description": "Pošlu Zásilkovnou."}, Triage(ships=False)) is True
    assert alerts.ships({"title": "x", "description": "Pouze osobní odběr."}, Triage(ships=True)) is False
    assert alerts.ships({"title": "x", "description": "Funkční."}, Triage(ships=True)) is True
