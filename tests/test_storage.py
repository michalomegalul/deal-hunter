"""Storage normalizer tests: ~300 real, hand-checked titles from the /hdd/ section plus edge cases."""

from collections import Counter
from pathlib import Path

import pytest
import yaml

from deal_hunter import bazos, storage
from deal_hunter.normalize import classify
from deal_hunter.storage import normalize, unit_price
from tests.conftest import fixture_text

TITLES = yaml.safe_load((Path(__file__).parent / "fixtures" / "disk_titles.yaml").read_text(encoding="utf-8"))
FEATURES = ("kind", "capacity_tb", "interface", "smr", "line", "quantity", "price_per", "external")


@pytest.mark.parametrize("entry", TITLES, ids=[e["title"][:50] for e in TITLES])
def test_real_titles(entry):
    r = normalize(entry["title"])
    assert (r.model, r.excluded_reason) == (entry.get("model"), entry.get("excluded"))
    if r.model:
        assert {k: r.features[k] for k in FEATURES if k in r.features} == {
            k: entry[k] for k in FEATURES if k in entry
        }


def test_real_title_set_is_big_and_varied():
    assert len(TITLES) >= 150
    statuses = Counter(storage.StorageResult(e.get("model"), {}, e.get("excluded")).status for e in TITLES)
    assert set(statuses) == {"matched", "excluded", "unmatched"}
    kinds = Counter(e.get("kind") for e in TITLES if "model" in e)
    assert kinds["hdd_35"] >= 30 and kinds["ssd_nvme"] >= 30
    big = [e for e in TITLES if e.get("kind") == "hdd_35" and e.get("capacity_tb", 0) >= 8]
    assert len(big) >= 8


@pytest.mark.parametrize(
    "raw, prepared",
    [
        ('3,5" SATA HDD 2 TB', "3.5 sata hdd 2 tb"),
        ("SSD M.2 NVMe 1TB", "ssd m2 nvme 1tb"),
        ("3xSSD 128GB", "3 x ssd 128gb"),
        ("4× Toshiba MG07 14TB", "4 x toshiba mg07 14tb"),
        ("Synology SAT5221 1,92TB", "synology sat5221 1.92tb"),
    ],
)
def test_prepare(raw, prepared):
    assert storage.prepare(raw) == prepared


@pytest.mark.parametrize(
    "title, interface",
    [
        # Seagate Exos: the same drive in SATA and SAS, told apart by the suffix
        ("Seagate Exos X16 16TB ST16000NM001G", "sata"),
        ("Seagate Exos X16 16TB ST16000NM002G", "sas"),
        ("Seagate Exos X18 18TB ST18000NM000J", "sata"),
        ("Seagate Exos X18 18TB ST18000NM004J", "sas"),
        ("Seagate Exos X20 20TB ST20000NM007D", "sata"),
        ("Seagate Exos 7E8 8TB ST8000NM0055", "sata"),
        ("Seagate Exos 7E8 8TB ST8000NM0075", "sas"),
        ("Seagate Enterprise 4TB ST4000NM0023", "sas"),
        ("Seagate Exos 12TB ST12000NM9999", None),  # unknown suffix: don't guess
        # HGST / WD Ultrastar: 'ALE6'/'ALA6'/'ALN6' SATA, 'AL5x'/'AL4x'/'ALS6' SAS
        ("HGST Ultrastar He12 12TB HUH721212ALE604", "sata"),
        ("HGST Ultrastar He12 12TB HUH721212AL5204", "sas"),
        ("WD Ultrastar DC HC530 14TB WUH721414ALE6L4", "sata"),
        ("WD Ultrastar DC HC550 16TB WUH721816AL5204", "sas"),
        ("HGST 4TB HUS724040ALA640", "sata"),
        ("HGST 4TB HUS724040ALS640", "sas"),
        # Toshiba MG: ACA = SATA, SCA = SAS
        ("Toshiba MG08ACA16TE 16TB", "sata"),
        ("Toshiba MG08SCA16TE 16TB", "sas"),
        # words
        ("HDD 4TB SAS 12Gb/s 3.5", "sas"),
        ("Disk 8TB 3.5 SATA III", "sata"),
        ("Disk 8TB 3.5", None),
        # lines that only exist as SATA
        ("WD Red Plus 8TB", "sata"),
        ("Seagate IronWolf 12TB", "sata"),
    ],
)
def test_interface(title, interface):
    assert normalize(title).features.get("interface") == interface


def test_seagate_10k_and_15k_drives_are_sas():
    assert storage.part_number("seagate st900mm0006").interface == "sas"
    assert storage.part_number("st1200mm0108").kind == "hdd_25"


def test_sas_drives_get_their_own_key_and_never_fit_the_server():
    r = normalize("Seagate Exos X16 16TB ST16000NM002G")
    assert r.model == "hdd35 enterprise sas 16tb"
    assert storage.server_fit(r.features, 8).startswith("SAS drive: not usable on this server")
    sata = normalize("Seagate Exos X16 16TB ST16000NM001G")
    assert sata.model == "hdd35 enterprise 16tb" and storage.server_fit(sata.features, 8) is None


@pytest.mark.parametrize(
    "title, smr",
    [
        ("WD Red 4TB WD40EFAX", True),  # the 2019 SMR Reds
        ("WD Red 4TB WD40EFRX", False),  # the older CMR ones
        ("WD Red 4TB", True),  # no model number: 2-6 TB Reds are SMR by default
        ("WD Red 8TB", False),
        ("WD Red Plus 4TB WD40EFZX", False),
        ("WD Red Pro 4TB", False),
        ("Seagate Barracuda 8TB ST8000DM004", True),
        ("Seagate Barracuda 2TB ST2000DM001", False),
        ("Seagate Barracuda 4TB", True),
        ("Seagate Barracuda Pro 8TB", False),
        ("Seagate IronWolf 8TB ST8000VN004", False),
        ("Toshiba P300 4TB HDWD240", True),
        ("Toshiba P300 2TB HDWD120", False),
        ("Toshiba N300 8TB", False),
        ("WD Blue 4TB", None),  # depends on the model: unknown
        ("HDD 8TB 3.5", None),
    ],
)
def test_smr(title, smr):
    assert normalize(title).features.get("smr") is smr


def test_host_managed_smr_ultrastar_does_not_fit():
    r = normalize("WD Ultrastar DC HC620 14TB HSH721414ALE6M4")
    assert r.features["smr"] is True and r.features["host_managed"] is True
    assert "host-managed SMR" in storage.server_fit(r.features, 8)


@pytest.mark.parametrize(
    "title, key",
    [
        ("WD Red Plus 8TB", "hdd35 nas 8tb"),
        ("Seagate IronWolf Pro 12TB", "hdd35 nas 12tb"),
        ("Toshiba N300 8TB", "hdd35 nas 8tb"),
        ("Seagate Exos X18 18TB ST18000NM000J", "hdd35 enterprise 18tb"),
        ("WD Gold 10TB", "hdd35 enterprise 10tb"),
        ("HGST Ultrastar 10TB", "hdd35 enterprise 10tb"),
        ("Toshiba MG09 18TB", "hdd35 enterprise 18tb"),
        ("WD Purple 8TB", "hdd35 surveillance 8tb"),
        ("Seagate SkyHawk 6TB", "hdd35 surveillance 6tb"),
        ("Seagate Barracuda 4TB", "hdd35 desktop 4tb"),
        ("Toshiba X300 8TB", "hdd35 desktop 8tb"),
        ("WD Elements 12TB", "external 12tb"),  # 6 TB and up: a desktop (shuckable) external
        ("WD Elements Desktop 8TB", "external 8tb"),
        ("Seagate Expansion Desktop 16TB", "external 16tb"),
        ("WD My Book 14TB", "external 14tb"),
        ("Seagate Expansion Portable 5TB", "external25 5tb"),
        ("WD Elements 4TB", "external25 4tb"),
        ("Pevný disk 8TB 3.5", "hdd35 8tb"),
        ("Toshiba 2,5 1TB do notebooku", "hdd25 1tb"),
        ("Seagate 2TB", "hdd 2tb"),  # form unknown
        ("Samsung 870 EVO 1TB", "ssd sata 1tb"),
        ("Samsung 990 PRO 2TB", "ssd nvme 2tb"),
        ("WD 8TB WD80EFZX", "hdd35 nas 8tb"),  # the model number says Red Plus
        ("Disk ST16000NM001G", "hdd35 enterprise 16tb"),  # and the capacity
        ("HDD 1,5TB", "hdd 1.5tb"),
    ],
)
def test_pricing_keys(title, key):
    assert normalize(title).model == key


@pytest.mark.parametrize(
    "title, reason",
    [
        ("Koupím disk 8TB", "wanted"),
        ("Sháním WD Red 4TB", "wanted"),
        ("HDD 2TB nefunkční, na díly", "broken"),
        ("Seagate 4TB vadné sektory", "broken"),
        ("Rámeček na 2.5 disk do 3.5 pozice", "accessory"),
        ("Externí box na HDD 3.5", "accessory"),
        ("Synology DS918+ bez disků", "nas_device"),
        ("NAS QNAP TS-453 + 4x 4TB", "nas_device"),
        ("Disky 2TB, 4TB a 8TB", "mixed_lot"),
        ("HDD WD a Seagate 2TB", "mixed_lot"),
        ("HDD 500GB 2.5 do notebooku", "small_laptop_drive"),
        ("Samsung T7 1TB portable SSD", "out_of_scope"),
        ("MicroSD 128GB", "out_of_scope"),
        ("IDE disk 40GB", "out_of_scope"),
    ],
)
def test_exclusions(title, reason):
    assert normalize(title).excluded_reason == reason


def test_no_bad_sectors_is_not_broken():
    assert normalize("HDD 2TB 3.5 bez vadných sektorů").model == "hdd35 2tb"
    assert normalize("WD Red 4TB, 0 vadných sektorů").excluded_reason is None


@pytest.mark.parametrize(
    "title, description, quantity, basis, price, unit",
    [
        ("4× Toshiba MG07 14TB", "Cena 7 000 Kč/ks, při odběru všech čtyř dohodou.", 4, "unit", 7000, 7000),
        ("4x 500gb sata. Vše : 700kč", "", 4, "lot", 700, 175),
        ("2x SSD Intel 100Gb - cena za oba", "", 2, "lot", 400, 200),
        ("WD Ultrastar 16 TB", "Prodám dva disky WD Ultrastar 16 TB SATA.", 2, "unknown", 7000, None),
        ("Seagate IronWolf 8TB", "Disk běží 2 roky v NAS.", None, None, 3000, 3000),
        ("Samsung 9100 PRO 2TB PCIe 5.0 4x NVMe", "", None, None, 5000, 5000),  # '4x' is the PCIe width
        ('2,5" HDD 1TB', "zachovalý 2.5 hdd", None, None, 500, 500),  # '5 hdd' is part of '2.5 hdd'
    ],
)
def test_quantity_and_price_per_drive(title, description, quantity, basis, price, unit):
    r = normalize(title, description)
    assert r.features.get("quantity") == quantity and r.features.get("price_per") == basis
    assert unit_price(price, r.features) == unit


def test_capacity_parsing():
    assert storage.capacities("3.5 sata hdd 2 tb 2000 gb") == [2000]
    assert storage.capacities("hdd 1tb 500gb 320gb") == [1000, 500, 320]
    assert storage.capacities("synology hat5300-12t") == [12000]
    assert storage.capacities("mg09aca18te 32 mb cache") == []  # part number, cache size
    assert storage.cap_text(1920) == "1.92tb" and storage.cap_text(500) == "500gb"


def test_description_fills_in_what_the_title_leaves_out():
    detail = bazos.parse_detail(
        fixture_text("detail_hdd_224787138.html"), "https://pc.bazos.cz/inzerat/224787138/x.php"
    )
    r = normalize(detail.title, detail.description)
    assert r.model == "hdd35 enterprise 18tb"
    f = r.features
    assert (f["part"], f["interface"], f["quantity"], f["price_per"]) == ("MG09ACA18TE", "sata", 3, "unit")
    assert storage.server_fit(f, 8) is None

    sas = bazos.parse_detail(
        fixture_text("detail_hdd_224774141.html"), "https://pc.bazos.cz/inzerat/224774141/x.php"
    )
    r = normalize(sas.title, sas.description)
    assert r.model == "hdd35 enterprise sas 20tb" and r.features["part"] == "ST20000NM004D"


def test_classify_and_detail_pages_only_for_server_candidates():
    assert classify("WD Red Plus 8TB", "hdd").category == "hdd"
    assert classify("WD Red Plus 8TB", "hdd").model == "hdd35 nas 8tb"
    assert storage.wants_detail(normalize("WD Red Plus 8TB"))
    assert storage.wants_detail(normalize("HDD 8TB"))  # form unknown: the page may say
    assert not storage.wants_detail(normalize("Samsung 990 PRO 2TB"))
    assert not storage.wants_detail(normalize("Seagate 2,5 1TB do notebooku"))
    assert not storage.wants_detail(normalize("Koupím disk 8TB"))


def test_hdd_pages_parse():
    items = bazos.parse_category(fixture_text("hdd_0.html"))
    assert len(items) >= 20 and all(i.price_kind for i in items)
    assert {i.psc for i in items if i.psc} and any("TB" in i.title for i in items)
    deeper = bazos.parse_category(fixture_text("hdd_280.html"))
    assert len(deeper) >= 20 and not {i.id for i in items} & {i.id for i in deeper}
    rss = bazos.parse_rss(fixture_text("rss_hdd.xml"))
    assert len(rss) >= 30 and rss[0].title == "SSD ADATA 240 GB" and rss[0].price_czk == 450
    assert bazos.rss_url("hdd") == "https://www.bazos.cz/rss.php?rub=pc&cat=5"
    assert bazos.category_url(2, "hdd") == "https://pc.bazos.cz/hdd/40/"
    detail = bazos.parse_detail(
        fixture_text("detail_hdd_224810214.html"), "https://pc.bazos.cz/inzerat/224810214/x.php"
    )
    assert detail.title == 'HDD SEAGATE 3,5" - 8TB' and detail.price_czk == 1800 and detail.psc == "684 01"
    assert "přemapované" in detail.description


def test_server_fit():
    fit = normalize("WD Red Plus 8TB").features
    assert storage.server_fit(fit, 8) is None
    assert storage.server_fit(normalize("WD Red Plus 6TB").features, 8) == "server fit: 6 TB < 8 TB"
    assert storage.server_fit(normalize("WD Elements 12TB").features, 8) is None  # shuckable
    assert "2.5" in storage.server_fit(normalize("WD Elements Portable 5TB").features, 4)
    assert "SSD" in storage.server_fit(normalize("Samsung 870 QVO 8TB").features, 8)
    assert "form factor unknown" in storage.server_fit(normalize("Seagate 8TB").features, 8)


@pytest.mark.parametrize(
    "title, noun, name",
    [
        ("3× Toshiba MG09 18TB enterprise HDD, záruka do 4. 2. 2027", "disky", "Toshiba MG09 18TB"),
        ("WD red 8TB", "disk", "WD red 8TB"),
        ("Prodám WD Red Plus 8TB 🔥 2800 Kč", "disk", "WD Red Plus 8TB"),
        ("Seagate Exos X18 18TB ST18000NM000J", "disk", "Seagate Exos X18 18TB"),
        ('HDD SEAGATE 3,5" - 8TB', "disk", "SEAGATE 8TB"),
        ("Predám 24TB WD Gold", "disk", "WD Gold 24TB"),
        ("Synology HAT5300-12T, 3.5” - 12TB", "disk", "Synology HAT5300 12TB"),
        ("Externí disk WD 8tb", "externí disk", "WD 8TB"),
        ("WD Elements Desktop 12TB", "externí disk", "WD Elements 12TB"),
        ("NOVÝ Pevný disk Seagate IronWolf Pro 4TB", "disk", "Seagate IronWolf Pro 4TB"),
        ("Samsung 870 EVO 1TB", "SSD", "Samsung 1TB"),
        ("Disk 8TB 3.5", "disk", "8TB"),
    ],
)
def test_noun_and_name_for_the_seller(title, noun, name):
    assert storage.noun_and_name(title, normalize(title).features) == (noun, name)


def test_badges_and_summary():
    f = normalize("Toshiba MG09ACA18TE 18TB").features
    assert storage.badges(f) == "SATA ✓ · CMR ✓"
    assert storage.badges(normalize("Seagate Barracuda 8TB ST8000DM004").features) == "SATA ✓ · SMR ⚠"
    assert storage.badges(normalize("HDD 8TB 3.5").features) == "SATA? · CMR?"
    assert storage.badges(normalize("HGST 4TB HUS724040ALS640").features) == "SAS ✗ · CMR ✓"
    disk = {"power_on_hours": 12000, "reallocated_or_pending": 0, "health_pct": 100, "tested": True}
    line = storage.summary_line(f, disk, price=13500, new_price=8990)
    assert line == (
        "💾 18 TB · 750 Kč/TB (new ~499 Kč/TB) · SATA ✓ · CMR ✓ · "
        "SMART: 12 000 h, 0 reallocated/pending, health 100 %, tested"
    )
    assert storage.smart_text({}) == "SMART unknown"
    many = normalize("3× Toshiba MG09 18TB").features
    assert storage.summary_line(many, {}, price=13500).startswith("💾 3× 18 TB · 750 Kč/TB")
