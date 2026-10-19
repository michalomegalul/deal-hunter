"""Rule-based normalizer for hard disks and SSDs (the Bazoš /hdd/ section, "Hard disky, SSD").

The point is expanding a home server: a plain SATA controller with free ports and no SAS HBA, so the
drives worth an alert are 3.5" SATA, CMR, 8 TB and more. The normalizer reads the title (and the
description, when we have it) and records what matters for that and for pricing:

- kind: hdd_35 | hdd_25 | hdd (form unknown) | ssd_sata | ssd_nvme | ssd (type unknown)
- capacity_tb (and capacity_gb)
- interface: sata | sas | None (unknown): 'sas' only when it's certain - the word "SAS" (or "12Gb/s")
  in the title/description, the HGST/WD Ultrastar 'S' model marker (ALS6, ...), or a Seagate NM suffix
  the catalog marks `certain` (storage_catalog.yaml). Anything else, including a Seagate NM suffix
  marked `uncertain` (remembered, not checked against a data sheet) is left unknown rather than guessed;
  an unknown-interface drive can still alert, just flagged "⚠ check SATA vs SAS" (badges(), below).
- line and class: WD Red Plus/Pro, Red, Purple, Gold, Ultrastar, Seagate IronWolf/Pro, Exos, SkyHawk,
  Barracuda, Toshiba N300/MG/X300/P300, Synology HAT, ...; class nas | enterprise | surveillance |
  desktop; shuckable desktop externals (WD Elements/My Book, Seagate Expansion/Backup Plus) apart
- smr: True / False / None, from a table of models and line defaults (storage_catalog.yaml)
- quantity, and whether the price is per piece ('unit'), for all of them ('lot') or unclear ('unknown')

The pricing key is class, pooled across capacities for 3.5" HDDs with a known class: 'hdd35 nas',
'hdd35 enterprise', 'hdd35 desktop', 'hdd35 surveillance', 'external' (desktop externals, also 3.5").
pricing.py prices these in Kč/TB (docs/disks.md), not Kč, since a single capacity
rarely gets the 4 ads a fair price needs. SAS drives keep their own capacity-specific key ('hdd35
enterprise sas 4tb': a market of their own, and now rarer since the interface call got stricter), as
does a 3.5" drive with no known line ('hdd35 8tb'), 2.5" ('hdd25 1tb', 'external25 2tb') and SSDs
('ssd nvme 1tb', 'ssd sata 500gb'). The part number is kept as a feature, not in the key.

Excluded: wanted ads, broken / for parts, accessories without a drive (frames, caddies, enclosures,
cables, adapters), NAS boxes and multi-bay enclosures, mixed lots (several capacities or brands),
laptop drives under 1 TB, IDE drives, external SSDs, and things that aren't drives (memory cards,
flash drives, tapes, RAM).

Like the other normalizers it is conservative: a wrong key poisons the fair prices, a missing one only
costs a data point, so anything unclear stays unmatched and shows up in `hunt review --section hdd`.
"""

import re
from dataclasses import dataclass, field
from functools import cache
from importlib import resources

import yaml

from deal_hunter.text import fold


@dataclass(frozen=True)
class StorageResult:
    model: str | None = None  # the pricing key
    features: dict = field(default_factory=dict, compare=False)
    excluded_reason: str | None = None

    @property
    def status(self) -> str:
        if self.model:
            return "matched"
        return "excluded" if self.excluded_reason else "unmatched"


@dataclass(frozen=True)
class Line:
    line: str
    brand: str
    cls: str
    smr: bool | None
    smr_tb: tuple[float, ...]
    pattern: re.Pattern
    sata_only: bool = False  # the line has no SAS models


@dataclass(frozen=True)
class Catalog:
    version: int
    brands: tuple[tuple[str, re.Pattern], ...]
    lines: tuple[Line, ...]
    ext_desktop: re.Pattern
    ext_portable: re.Pattern
    ext_any: re.Pattern
    seagate_sas: frozenset[str]  # certain only; see storage_catalog.yaml
    seagate_sata: frozenset[str]
    smr_models: frozenset[str]
    cmr_models: frozenset[str]


@cache
def load_catalog() -> Catalog:
    raw = yaml.safe_load(resources.files("deal_hunter").joinpath("storage_catalog.yaml").read_text())
    return Catalog(
        version=raw["version"],
        brands=tuple((b, re.compile(p)) for b, p in raw["brands"].items()),
        lines=tuple(
            Line(
                e["line"],
                e["brand"],
                e["class"],
                e.get("smr"),
                tuple(float(x) for x in e.get("smr_tb") or ()),
                re.compile(e["pattern"]),
                bool(e.get("sata_only")),
            )
            for e in raw["lines"]
        ),
        ext_desktop=re.compile(raw["externals"]["desktop"]),
        ext_portable=re.compile(raw["externals"]["portable"]),
        ext_any=re.compile(raw["externals"]["any"]),
        seagate_sas=frozenset(raw["seagate_sas"]["certain"]),
        seagate_sata=frozenset(raw["seagate_sata"]),
        smr_models=frozenset(raw["smr_models"]),
        cmr_models=frozenset(raw["cmr_models"]),
    )


def version() -> int:
    return load_catalog().version


# --- text preparation ---------------------------------------------------------------------------------


def prepare(text: str) -> str:
    """Lower case, no accents, decimal commas to points, inch marks and punctuation to spaces.

    '3,5" SATA HDD 2 TB' -> '3.5 sata hdd 2 tb'; 'M.2 NVMe' -> 'm2 nvme'; '4×' -> '4 x'.
    """
    t = fold((text or "").replace("×", " x "))
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    t = re.sub(r"\bm\.2\b", "m2", t)
    t = re.sub(r"\b(\d{1,2})x(?=[a-z])", r"\1 x ", t)  # '3xSSD' -> '3 x ssd'
    t = re.sub(r"\bu\.([23])\b", r"u\1", t)
    t = re.sub(r"(?<=\d)\.(?=\d)", "#", t)  # keep decimal points through the punctuation pass
    t = re.sub(r"[^a-z0-9#+/_-]+", " ", t).replace("#", ".")
    t = re.sub(r"(?<=\s)[-/]+(?=\s)|^[-/]+|[-/]+$", " ", t)
    return " ".join(t.split())


# --- exclusions -----------------------------------------------------------------------------------------

WANTED = re.compile(
    r"\b(koupim|kupim|shanim|hledam|vykup|vykupuji|vykupujeme|vykoupim|poptavam|sezenu|koupime)\b"
)
NOT_A_DRIVE = re.compile(
    r"\b(micro ?sd|sd ?kart\w*|sdhc|sdxc|flash ?disk\w*|usb flash|flashk\w*|lto ?\d?|ultrium|kazet\w*"
    r"|ram|ddr ?\d?|pamet\w*|dvd|cd|disket\w*|vga|adr|canvas|a[12]|uhs)\b"
)
NAS_DEVICE = re.compile(
    r"\b(ds ?\d{3,4}(j|se|play|\+)?|ts ?\d{3}\w*|qnap|diskstation|beestation|bst\d{3}\w*|my ?cloud|[24] ?big"
    r"|nas server|hd 35nas|d2 quadra)\b"
)
# A bare 'NAS' is a box ("NAS Synology ...") unless it names a NAS drive line ("Seagate NAS IronWolf",
# "Red Plus NAS") or is what the drive is for ("disky do NAS", "vhodný pro NAS").
NAS_WORD = re.compile(r"^nas\b|(?<!\bdo )(?<!\bpro )(?<!\bna )(?<!\bz )(?<!\bze )\bnas\b(?! disk)")
ACCESSORY = re.compile(
    r"\b(ramec\w*|caddy|redukc\w*|adapter\w*|box|kabel\w*|dok|dokovac\w*|docking|dock|sacht\w*|supl\w*|pouzdr\w*"
    r"|obal\w*|case|enclosure|radic\w*|controller|hba|cage|kolebk\w*|sled|tray|hyper m2|chladic\w*)\b"
)
DRIVE_WORD = re.compile(r"\b(hdd|ssd|sdd|disk\w*|harddisk\w*|hard ?disk\w*|nvme|nvm|m2|msata|hadisk)\b")
# Broken or for parts. '0 vadných sektorů' / 'bez vadných sektorů' is the opposite, so those go first.
NOT_BROKEN = re.compile(
    r"\b(0|bez|zadn\w*|nula|zero)\s+(vadn\w*|realok\w*|premapovan\w*|bad)\s+(sektor\w*|blok\w*)?"
)
BROKEN = re.compile(
    r"\b(nefunkc\w*|na dily|na nd|na opravu|vadn\w*|poskozen\w*|necte|nejde|cvaka|klika|bad sector\w*"
    r"|nedetekuje|nenabiha)\b"
)
IDE = re.compile(r"\b(ide|pata|ata ?\d{2,3}|udma)\b")
MIXED_WORDS = re.compile(r"\b(mix|ruzn\w*|vetsi pocet|starsich|sada disku|lot|vice (ks|ksu|kusu|druhu))\b")
EXTERNAL_SSD = re.compile(
    r"\b(portable|t[579]|extreme portable|extreme pro portable|x[69]|externi\w*|external)\b"
)

# --- capacity, kind, interface ----------------------------------------------------------------------------

CAPACITY = re.compile(r"(?<![a-z0-9.])(\d{1,5}(?:\.\d{1,2})?)\s?(tb|t|gb|g)(?![a-z])")
# Several sizes in one title: '240-250GB', '3 a 4 TB', '100 / 250 / 320 GB' (three numbers, so that
# 'Evo 870 250 GB' and 'HAT 3310 12TB' stay one drive).
RANGE = re.compile(
    r"(?<![a-z0-9.])\d{1,4}(\.\d)?\s?(-|/|a|i|nebo)\s?\d{1,4}(\.\d)?\s?(tb|gb|t|g)\b"
    r"|(?<![a-z0-9.])\d{2,4}(\s+\d{2,4}){2,}\s?(tb|gb)\b"
)

SSD = re.compile(
    r"\b(ssd|sdd|nvme|nvm|msata|m2|u2|evo|qvo|pcie|sn\d{3}x?|sa\d{3}|a400|mx500|bx500|kc\d{3,4}|nv[123]"
    r"|pm\d{3}\w*|xg\d|bc\d{3}|p41|su\d{3}|sat\d{4}|m5\d0|mx\d{3}|bx\d{3}|870|860|850|840|750|970|980|990|9100)\b"
)
NVME = re.compile(
    r"\b(nvme|nvm|pcie|gen ?[345]|sn\d{3}x?|9[789]0|9100|nv[123]|kc3000|p3|p5|t500|t700|renegade|pm9\d\d\w*"
    r"|xg\d|bc\d{3}|p41|nm\d{3}|sn850x)\b"
)
SATA_SSD = re.compile(
    r"\b(m2 sata|sata m2|msata|sata|2\.5|8[4-7]0|750|m5\d0|mx500|bx500|a400|sa400|sa5\d0|kc600|su\d{3}|ultra 3d|3d nand)\b"
)
FORM_35 = re.compile(r"\b3\.5 ?(p|palc\w*|in|inch)?\b|\bdo pc\b|\bdesktop\b")
FORM_25 = re.compile(r"\b2\.5 ?(p|palc\w*|in|inch)?\b|\bnotebook\w*|\bdo nb\b|\bntb\b|\blaptop\w*|\bmobile\b")

SAS_TEXT = re.compile(r"\bsas ?[23]?\b|\b12 ?g(b/s|bps|b)?\b|\bsff ?-?8482\b")
SATA_TEXT = re.compile(r"\bsata ?(iii|ii|3|2|6g\w*)?\b")

# Part numbers.
SEAGATE = re.compile(r"\bst(3?)(\d{3,5})([a-z]{2})(\d{3,4}[a-z]?|\d{2,3}[a-z])\b")
SEAGATE_OLD = re.compile(r"\bst3(\d{3,4})(\d{3})as\b")  # ST32000641AS
WD = re.compile(r"\b(?:wd)?(\d{2,4})([a-z]{4})\b")
HGST = re.compile(
    r"\b(hu[ahs]|wu[hs]|hsh|hds|hdn|hts)72?(\w{4,6}?)al([a-z0-9])\w*\b|\b(hts|hds|hdn)\d{6}\w*\b"
)
TOSHIBA_MG = re.compile(r"\bmg(\d\d)([as])ca(\d{2}t|\d{3}e)\w*\b")
TOSHIBA_HDW = re.compile(r"\bhdw([dgnretq])(\w{3})\b")
TOSHIBA_OTHER = re.compile(r"\b(dt0[12]a[bc]a\d{3}|mq0\d\w+)\b")
SAMSUNG_OLD = re.compile(r"\bhd\d{3}[a-z]{2}\b")

WD_LINE = {
    "efax": "wd red",
    "efrx": "wd red",
    "efzx": "wd red plus",
    "efzz": "wd red plus",
    "efpx": "wd red plus",
    "efbx": "wd red plus",
    "ffbx": "wd red pro",
    "ffwx": "wd red pro",
    "fryz": "wd gold",
    "fbyz": "wd gold",
    "fyyz": "wd gold",
    "purz": "wd purple",
    "purx": "wd purple",
    "purp": "wd purple",
    "ejrx": "wd purple",
    "ezex": "wd blue",
    "ezrz": "wd blue",
    "ezaz": "wd blue",
    "eazz": "wd blue",
    "aakx": "wd blue",
    "eals": "wd blue",
    "fzex": "wd black",
    "fzbx": "wd black",
    "fals": "wd black",
    "ezrx": "wd green",
    "earx": "wd green",
    "ears": "wd green",
}
WD_WHITE = {"emaz", "edaz", "edbz", "edfz", "emfz", "edgz", "emzz"}  # shucked white-label (Red-like, CMR)
WD_LAPTOP = re.compile(r"^(lp|jp|sp|jf|lf|bp|jd|bf|sq)")
SEAGATE_FAMILY = {
    "dm": ("seagate barracuda", "hdd_35"),
    "dl": ("seagate barracuda", "hdd_35"),
    "vn": ("seagate ironwolf", "hdd_35"),
    "ne": ("seagate ironwolf pro", "hdd_35"),
    "nt": ("seagate ironwolf pro", "hdd_35"),
    "nm": ("seagate exos", "hdd_35"),
    "vx": ("seagate skyhawk", "hdd_35"),
    "lm": (None, "hdd_25"),
    "lx": (None, "hdd_25"),
    "nx": ("seagate exos", "hdd_25"),
    "mm": ("seagate exos", "hdd_25"),
    "mp": ("seagate exos", "hdd_25"),
    "mx": ("seagate exos", "hdd_25"),
}
TOSHIBA_HDW_LINE = {"d": "toshiba p300", "g": "toshiba n300", "n": "toshiba n300", "r": "toshiba x300"}
TOSHIBA_HDW_LINE |= {"e": "toshiba x300", "t": "toshiba s300", "q": "toshiba n300"}


@dataclass
class PartInfo:
    model: str | None = None  # 'ST8000DM004'
    brand: str | None = None
    line: str | None = None
    kind: str | None = None  # hdd_35 | hdd_25
    interface: str | None = None
    capacity_gb: int | None = None
    smr: bool | None = None
    host_managed: bool = False


def part_number(t: str) -> PartInfo:
    """What a model number in prepared text tells: brand, line, form, interface, capacity, SMR."""
    cat = load_catalog()
    if m := SEAGATE_OLD.search(t):
        model = m.group(0)
        return PartInfo(model.upper(), "seagate", None, "hdd_35", "sata", int(m.group(1)), _smr(model))
    if m := SEAGATE.search(t):
        old, digits, family, suffix = m.groups()
        model = m.group(0)
        line, kind = SEAGATE_FAMILY.get(family, (None, None))
        interface = None
        code = f"{family}{suffix}"
        if family == "nm":
            interface = "sas" if code in cat.seagate_sas else "sata" if code in cat.seagate_sata else None
        elif family in ("mm", "mp", "mx"):
            interface = "sas"
        elif kind:
            interface = "sata"
        cap = int(digits) if not old else None
        return PartInfo(model.upper(), "seagate", line, kind, interface, cap, _smr(model))
    m = next(
        (
            x
            for x in WD.finditer(t)
            if x.group(0).startswith("wd") or x.group(2) in WD_LINE or x.group(2) in WD_WHITE
        ),
        None,
    )
    if m:
        digits, suffix = m.groups()
        model = m.group(0) if m.group(0).startswith("wd") else f"wd{m.group(0)}"
        line = WD_LINE.get(suffix) or ("wd white label" if suffix in WD_WHITE else None)
        kind = "hdd_25" if WD_LAPTOP.match(suffix) else "hdd_35" if line else None
        if len(digits) == 2:
            cap = int(digits[0]) * 1000
        elif len(digits) == 3:
            cap = int(digits[:2]) * 1000
        else:
            cap = None  # WD5000 = 500 GB but WD3000 is 300 GB or 3 TB: let the title say
        return PartInfo(model.upper(), "wd", line, kind, "sata" if kind else None, cap, _smr(model))
    if m := HGST.search(t):
        model = m.group(0)
        prefix = m.group(1) or m.group(4)
        if prefix in ("hts",):
            return PartInfo(model.upper(), "hgst", None, "hdd_25", "sata")
        if prefix in ("hds", "hdn"):
            line = "hgst deskstar nas" if prefix == "hdn" else "hgst deskstar"
            return PartInfo(model.upper(), "hgst", line, "hdd_35", "sata")
        code = m.group(3)
        # HGST/WD Ultrastar's own documented markers: 'ALE6'/'ALA6'/'ALN6' SATA, 'AL5x'/'AL4x'/'ALS6'
        # SAS - certain either way, unlike the Seagate NM suffix table below (see its `uncertain` list).
        interface = "sata" if code in "ean" else "sas" if code in "s45" else None
        brand = "wd" if prefix.startswith("w") else "hgst"
        return PartInfo(
            model.upper(),
            brand,
            "ultrastar",
            "hdd_35",
            interface,
            smr=prefix == "hsh",
            host_managed=prefix == "hsh",
        )
    if m := TOSHIBA_MG.search(t):
        model = m.group(0)
        size = m.group(3)
        cap = int(size[:2]) * 1000 if size.endswith("t") else int(size[0]) * 1000
        return PartInfo(
            model.upper(), "toshiba", "toshiba mg", "hdd_35", "sata" if m.group(2) == "a" else "sas", cap
        )
    if m := TOSHIBA_HDW.search(t):
        model = m.group(0)
        return PartInfo(
            model.upper(), "toshiba", TOSHIBA_HDW_LINE.get(m.group(1)), "hdd_35", "sata", smr=_smr(model)
        )
    if m := TOSHIBA_OTHER.search(t):
        model = m.group(0)
        kind = "hdd_25" if model.startswith("mq") else "hdd_35"
        return PartInfo(model.upper(), "toshiba", None, kind, "sata", smr=_smr(model))
    if m := SAMSUNG_OLD.search(t):
        return PartInfo(m.group(0).upper(), "samsung", "samsung spinpoint", "hdd_35", "sata", smr=False)
    return PartInfo()


def _smr(model: str) -> bool | None:
    cat = load_catalog()
    m = model.lower()
    if any(m.startswith(x) for x in cat.smr_models):
        return True
    if any(m.startswith(x) for x in cat.cmr_models):
        return False
    return None


def capacities(t: str) -> list[int]:
    """Distinct capacities in GB, in order: '2TB (2000 GB)' -> [2000]; '1tb 500gb 320gb' -> [1000, 500, 320]."""
    out: list[int] = []
    for m in CAPACITY.finditer(t):
        value, unit = float(m.group(1)), m.group(2)
        gb = round(value * 1000) if unit.startswith("t") else round(value)
        if unit.startswith("g") and gb < 16:
            continue  # '6G' (VRAM), '12G' (SAS speed) and the like
        if gb > 40000:
            continue
        if not any(abs(gb - o) <= 0.05 * max(gb, o) for o in out):
            out.append(gb)
    return out


def cap_text(gb: int) -> str:
    """8000 -> '8tb', 1500 -> '1.5tb', 1920 -> '1.92tb', 500 -> '500gb'."""
    if gb >= 1000:
        tb = gb / 1000
        return f"{tb:g}tb"
    return f"{gb}gb"


def round_capacity(gb: int) -> int:
    """Marketing sizes: 960/1000/1024 GB all 1 TB for HDDs; SSDs keep 960 vs 1000 apart."""
    for size in (
        1000,
        2000,
        3000,
        4000,
        5000,
        6000,
        8000,
        10000,
        12000,
        14000,
        16000,
        18000,
        20000,
        22000,
        24000,
    ):
        if abs(gb - size) <= 0.03 * size:
            return size
    return gb


# --- quantity and price per piece -----------------------------------------------------------------------

NUMBER_WORDS = {
    "dva": 2,
    "dve": 2,
    "tri": 3,
    "ctyri": 4,
    "pet": 5,
    "sest": 6,
    "sedm": 7,
    "osm": 8,
    "oba": 2,
    "obe": 2,
}
QTY_TITLE = re.compile(
    r"^(\d{1,3}) ?(x|ks|kusy|kusu)\b|(?<![.\d])\b(\d{1,3}) ?(ks|kusy|kusu)\b"
    r"|(?<![.\d])\b(\d{1,3}) ?x (?=(hdd|ssd|disk\w*|wd|seagate|toshiba|hgst|hitachi|samsung|sata|\d+ ?(tb|gb)\b))"
)
QTY_TEXT = re.compile(
    r"(?<![.\d])\b(\d{1,3}|dva|dve|tri|ctyri|pet|sest|sedm|osm)\s+(?:(?!tb\b|gb\b|t\b|g\b)\w+\s+){0,2}?"
    r"(ks|kusy|kusu|disky|disku|hdd)\b"
)
PER_UNIT = re.compile(
    r"/ ?ks\b|/ ?kus\b|\bza (1 )?(ks|kus)\b|\bcena za (1|jeden|kus)\b|\bkus za\b|\bper piece\b"
    r"|\ba \d[\d ]* ?kc\b"  # 'á 500 Kč'
)
PER_LOT = re.compile(
    r"\b(za vse|za vsechny|za oba|za obe|vse za|celkem|komplet\w*|za cele|dohromady|za tri|za ctyri|vse)\b"
)


def quantity(t: str, d: str) -> int:
    """How many drives the ad sells (1 unless it says so)."""
    if m := QTY_TITLE.search(t):
        return int(next(g for g in (m.group(1), m.group(3), m.group(5)) if g))
    if m := QTY_TEXT.search(d):
        word = m.group(1)
        n = int(word) if word.isdigit() else NUMBER_WORDS[word]
        if 2 <= n <= 50:
            return n
    return 1


def price_basis(t: str, d: str) -> str:
    """'unit' (the price is per drive), 'lot' (for all of them) or 'unknown'."""
    text = f"{t} {d}"
    if PER_UNIT.search(text):
        return "unit"
    if PER_LOT.search(text):
        return "lot"
    return "unknown"


# --- the normalizer ---------------------------------------------------------------------------------------


def find_brand(t: str) -> str | None:
    hits = [(m.start(), b) for b, p in load_catalog().brands for m in [p.search(t)] if m]
    return min(hits)[1] if hits else None


def all_brands(t: str) -> set[str]:
    brands = {b for b, p in load_catalog().brands if p.search(t)}
    for word, brand in (
        ("kingston", "kingston"),
        ("crucial", "crucial"),
        ("patriot", "patriot"),
        ("intel", "intel"),
    ):
        if re.search(rf"\b{word}\b", t):
            brands.add(brand)
    return brands


def find_line(t: str, brand: str | None) -> Line | None:
    for line in load_catalog().lines:
        if line.pattern.search(t) and (brand is None or line.brand == brand or line.line == "ultrastar"):
            return line
    return None


def line_by_name(name: str | None) -> Line | None:
    return next((x for x in load_catalog().lines if x.line == name), None)


def normalize(title: str, description: str | None = None) -> StorageResult:
    t = prepare(title)
    d = prepare(description or "")
    if WANTED.search(t):
        return StorageResult(excluded_reason="wanted")
    if NAS_DEVICE.search(t) or (NAS_WORD.search(t) and find_line(t, None) is None):
        return StorageResult(excluded_reason="nas_device")
    drive = DRIVE_WORD.search(t)
    part = ACCESSORY.search(t)
    if part and (drive is None or part.start() < drive.start() or not capacities(t)):
        return StorageResult(excluded_reason="accessory")
    if NOT_A_DRIVE.search(t):
        return StorageResult(excluded_reason="out_of_scope")
    if BROKEN.search(NOT_BROKEN.sub(" ", t)):
        return StorageResult(excluded_reason="broken")
    if IDE.search(t):
        return StorageResult(excluded_reason="out_of_scope")  # IDE/PATA: no use for a server

    info = part_number(t)
    if info.model is None and d:
        found = {part_number(x).model for x in re.findall(r"\b(?:st|wd|hu|wu|mg|hdw)\w{6,14}\b", d)} - {None}
        if len(found) == 1:  # the description names exactly one drive model
            info = part_number(prepare(found.pop()))
    caps = capacities(t)
    if RANGE.search(t) or len(caps) > 1 or MIXED_WORDS.search(t):
        return StorageResult(excluded_reason="mixed_lot")
    if len(all_brands(t)) > 1:
        return StorageResult(excluded_reason="mixed_lot")
    brand = find_brand(t) or info.brand
    line = find_line(t, brand) or line_by_name(info.line)
    gb = caps[0] if caps else info.capacity_gb

    is_ssd = bool(SSD.search(t)) and not (line and line.cls != "desktop" and not re.search(r"\bssd\b", t))
    features: dict = {}
    if is_ssd:
        if EXTERNAL_SSD.search(t):
            return StorageResult(excluded_reason="out_of_scope")  # external/portable SSDs
        kind = "ssd_sata" if SATA_SSD.search(t) and not re.search(r"\bnvme\b", t) else "ssd"
        if kind == "ssd" and NVME.search(t):
            kind = "ssd_nvme"
        features["kind"] = kind
    else:
        cat = load_catalog()
        named = cat.ext_any.search(t) or cat.ext_desktop.search(t) or cat.ext_portable.search(t)
        external = bool(named) and info.model is None  # a bare drive with a part number is internal
        if external:
            if cat.ext_desktop.search(t):
                kind = "hdd_35"
            elif cat.ext_portable.search(t) or FORM_25.search(t):
                kind = "hdd_25"
            elif gb and gb >= 6000:
                kind = "hdd_35"
            else:
                kind = "hdd_25"
            features["external"] = True
        elif FORM_35.search(t) and not FORM_25.search(t):
            kind = "hdd_35"
        elif FORM_25.search(t) and not FORM_35.search(t):
            kind = "hdd_25"
        elif info.kind:
            kind = info.kind
        elif line is not None or FORM_35.search(d) and not FORM_25.search(d):
            kind = "hdd_35"
        elif FORM_25.search(d) and not FORM_35.search(d):
            kind = "hdd_25"
        else:
            kind = "hdd"
        features["kind"] = kind

    if gb is None:
        if is_ssd or drive or brand or line:
            return StorageResult()  # a drive, but how big? -> `hunt review`
        return StorageResult(excluded_reason="out_of_scope")
    if features["kind"].startswith("hdd"):
        gb = round_capacity(gb)
    if features["kind"] == "hdd_25" and gb < 1000 and not features.get("external"):
        return StorageResult(excluded_reason="small_laptop_drive")

    features["capacity_gb"] = gb
    features["capacity_tb"] = round(gb / 1000, 3)
    if brand:
        features["brand"] = brand
    if info.model:
        features["part"] = info.model
    if line is not None and not is_ssd:
        features["line"] = line.line
        features["class"] = line.cls
    elif info.line == "wd white label":
        features["line"], features["class"] = "wd white label", "nas"

    if not is_ssd:
        interface = info.interface
        text_sas, text_sata = SAS_TEXT.search(t), SATA_TEXT.search(t)
        if interface is None:
            if text_sas and not text_sata:
                interface = "sas"
            elif text_sata and not text_sas:
                interface = "sata"
            elif d and SAS_TEXT.search(d) and not SATA_TEXT.search(d):
                interface = "sas"
            elif (d and SATA_TEXT.search(d) and not SAS_TEXT.search(d)) or (
                line is not None and line.sata_only
            ):
                interface = "sata"
        if interface:
            features["interface"] = interface
        smr = info.smr
        if smr is None and line is not None:
            smr = True if gb / 1000 in line.smr_tb else line.smr
        if smr is not None:
            features["smr"] = smr
        if info.host_managed:
            features["host_managed"] = True

    if features["kind"] == "hdd" and features.get("interface") == "sas" and gb >= 2000:
        features["kind"] = (
            "hdd_35"  # SAS drives of 2 TB and more are 3.5" (the 2.5" ones are 10k/15k, smaller)
        )
    qty = quantity(t, d)
    if qty > 1:
        features["quantity"] = qty
        features["price_per"] = price_basis(t, d)
    if re.search(r"\b(recert\w*|refurb\w*|repas\w*|renew\w*)\b", f"{t} {d}"):
        features["recertified"] = True
    return StorageResult(pricing_key(features), features)


HDD35_CLASSES = ("nas", "enterprise", "surveillance", "desktop")  # pooled, Kč/TB (pricing.py)
CAPACITY_SUFFIX = re.compile(r"^(?P<base>.+) (?P<num>\d+(?:\.\d+)?)(?P<unit>tb|gb)$")


def pooled_key(model: str) -> bool:
    """True for a Kč/TB-pooled 3.5" HDD key: a known class, or a desktop external (also 3.5")."""
    return model == "external" or model in {f"hdd35 {c}" for c in HDD35_CLASSES}


def split_capacity_key(key: str) -> tuple[str, float] | None:
    """'hdd35 nas 8tb' -> ('hdd35 nas', 8.0); None if `key` has no trailing capacity (already pooled,
    e.g. 'hdd35 nas', 'external'). Used to read the capacity-specific keys in new_price_pages.yaml."""
    m = CAPACITY_SUFFIX.match(key)
    if not m:
        return None
    tb = float(m["num"]) * (1.0 if m["unit"] == "tb" else 0.001)
    return m["base"], tb


def pricing_key(f: dict) -> str:
    cap = cap_text(f["capacity_gb"])
    kind = f["kind"]
    if kind.startswith("ssd"):
        sub = {"ssd_sata": "sata ", "ssd_nvme": "nvme "}.get(kind, "")
        return f"ssd {sub}{cap}"
    if f.get("external"):
        return "external" if kind == "hdd_35" else f"external25 {cap}"
    if kind == "hdd_25":
        return f"hdd25 {cap}"
    if kind == "hdd":
        return f"hdd {cap}"
    cls = f.get("class")
    if f.get("interface") == "sas":
        return f"hdd35 {cls or 'enterprise'} sas {cap}"
    return f"hdd35 {cls}" if cls else f"hdd35 {cap}"


def unit_price(price: int | None, features: dict | None) -> int | None:
    """The price of one drive: the asking price, or the lot price split, or None when unclear."""
    if not price:
        return price
    f = features or {}
    qty = int(f.get("quantity") or 1)
    if qty <= 1 or f.get("price_per") == "unit":
        return price
    if f.get("price_per") == "lot":
        return round(price / qty)
    return None


def wants_detail(r) -> bool:
    """Detail pages only for drives that could fit the server (3.5", external desktop, form unknown)."""
    if r.excluded_reason is not None:
        return False
    kind = (r.features or {}).get("kind")
    return kind not in ("ssd", "ssd_sata", "ssd_nvme", "hdd_25")


# --- server fit ---------------------------------------------------------------------------------------


def server_fit(features: dict, min_tb: float) -> str | None:
    """Why a drive can't go into the home server (None = it fits): 3.5" SATA, >= min_tb, not SAS."""
    f = features or {}
    if f.get("interface") == "sas":
        return "SAS drive: not usable on this server (SATA controller, no SAS HBA)"
    if f.get("host_managed"):
        return "host-managed SMR: needs special software, not usable on this server"
    if f.get("kind") != "hdd_35":
        what = {"hdd_25": '2.5" drive', "hdd": "form factor unknown"}.get(f.get("kind"), "SSD")
        return f'server fit: {what}, not a 3.5" disk'
    if (f.get("capacity_tb") or 0) < min_tb:
        return f"server fit: {f.get('capacity_tb', 0):g} TB < {min_tb:g} TB"
    return None


# --- for messages and pushes ------------------------------------------------------------------------------

BRAND_NAMES = {"wd": "WD", "seagate": "Seagate", "toshiba": "Toshiba", "hgst": "HGST", "samsung": "Samsung"}
BRAND_NAMES |= {"synology": "Synology", "lacie": "LaCie"}
LINE_WORDS = {
    "wd red plus": ("Red", "Plus"),
    "wd red pro": ("Red", "Pro"),
    "wd red": ("Red",),
    "wd purple": ("Purple",),
    "wd gold": ("Gold",),
    "ultrastar": ("Ultrastar",),
    "wd black": ("Black",),
    "wd blue": ("Blue",),
    "wd green": ("Green",),
    "seagate ironwolf pro": ("IronWolf", "Pro"),
    "seagate ironwolf": ("IronWolf",),
    "seagate exos": ("Exos",),
    "seagate constellation": ("Constellation",),
    "seagate skyhawk": ("SkyHawk",),
    "seagate barracuda pro": ("Barracuda", "Pro"),
    "seagate barracuda": ("Barracuda",),
    "toshiba n300": ("N300",),
    "toshiba mg": ("MG",),
    "toshiba x300": ("X300",),
    "toshiba p300": ("P300",),
    "toshiba s300": ("S300",),
    "hgst deskstar nas": ("Deskstar", "NAS"),
    "hgst deskstar": ("Deskstar",),
    "samsung spinpoint": ("SpinPoint",),
}
BRAND_WRITTEN = re.compile(
    r"(?<![^\W_])(wd_black|wdc|wd|western digital|seagate|toshiba|hgst|hitachi|samsung|synology|lacie)(?![^\W_])",
    re.IGNORECASE,
)
SERIES = re.compile(r"(?<![^\W_])(mg ?\d\d|hat ?\d{4}|x\d{2}|hc ?\d{3})(?![^\W_])", re.IGNORECASE)


def _as_written(title: str, word: str) -> str:
    m = re.search(rf"(?<![^\W_]){re.escape(word)}(?![^\W_])", title, re.IGNORECASE)
    return m.group(0) if m else word


def display_name(title: str, f: dict) -> str:
    """'Prodám 3× Toshiba MG09 18TB enterprise' -> 'Toshiba MG09 18TB' (the seller's spelling)."""
    words = []
    brand = f.get("brand")
    if brand:
        written = BRAND_WRITTEN.search(title)
        words.append(written.group(0) if written else BRAND_NAMES.get(brand, brand))
    line = f.get("line")
    if line in LINE_WORDS:
        series = SERIES.search(title)
        if line in ("toshiba mg",) and series:
            words.append(series.group(0))
        else:
            words += [_as_written(title, w) for w in LINE_WORDS[line]]
            if line == "seagate exos" and series and series.group(0).lower().startswith("x"):
                words.append(series.group(0))
    elif line and line.startswith("synology") and (series := SERIES.search(title)):
        words.append(series.group(0))
    elif f.get("external"):
        ext = re.search(
            r"(?<![^\W_])(elements|my ?book|my ?passport|expansion|backup plus|one touch)(?![^\W_])",
            title,
            re.IGNORECASE,
        )
        if ext:
            words.append(ext.group(0))
    gb = f.get("capacity_gb")
    if gb:
        words.append(cap_text(gb).upper())
    return " ".join(words)


def noun_and_name(title: str, f: dict) -> tuple[str, str]:
    """(accusative noun, item) for the message to the seller: ('disky', 'Toshiba MG09 18TB')."""
    many = int(f.get("quantity") or 1) > 1
    kind = f.get("kind") or ""
    if kind.startswith("ssd"):
        noun = "SSD"
    elif f.get("external"):
        noun = "externí disky" if many else "externí disk"
    else:
        noun = "disky" if many else "disk"
    return noun, display_name(title, f)


def badges(f: dict) -> str:
    """'SATA ✓ · CMR ✓' / 'SAS ✗' / '⚠ check SATA vs SAS · SMR ⚠'."""
    out = []
    interface = f.get("interface")
    out.append("SATA ✓" if interface == "sata" else "SAS ✗" if interface == "sas" else "⚠ check SATA vs SAS")
    smr = f.get("smr")
    out.append("CMR ✓" if smr is False else "SMR ⚠" if smr else "CMR?")
    return " · ".join(out)


def smart_text(disk: dict) -> str:
    """'SMART: 12 000 h, 0 reallocated, health 100 %, tested' or 'SMART unknown'."""
    bits = []
    hours = disk.get("power_on_hours")
    if hours is not None:
        bits.append(f"{hours:,} h".replace(",", " "))
    realloc = disk.get("reallocated_or_pending")
    if realloc is not None:
        bits.append(
            f"{int(realloc)} reallocated/pending"
            if not isinstance(realloc, bool)
            else ("reallocated/pending sectors!" if realloc else "0 reallocated")
        )
    if disk.get("health_pct") is not None:
        bits.append(f"health {disk['health_pct']} %")
    if disk.get("smart_screenshot_mentioned"):
        bits.append("SMART screenshot")
    if disk.get("tested"):
        bits.append("tested")
    if disk.get("warranty_until"):
        bits.append(f"warranty {disk['warranty_until']}")
    if disk.get("from_nas_or_server"):
        bits.append("from a NAS/server")
    return ("SMART: " + ", ".join(bits)) if bits else "SMART unknown"


def summary_line(f: dict, disk: dict, price: int | None = None, new_price: int | None = None) -> str:
    """'💾 18 TB · 750 Kč/TB (new ~1 050 Kč/TB) · SATA ✓ · CMR ✓ · SMART: 12 000 h'."""
    tb = f.get("capacity_tb")
    if not tb:
        return ""
    bits = [f"💾 {tb:g} TB"]
    if int(f.get("quantity") or 1) > 1:
        bits[0] = f"💾 {f['quantity']}× {tb:g} TB"
    if price:
        per = f"{round(price / tb):,} Kč/TB".replace(",", " ")
        if new_price:
            per += f" (new ~{round(new_price / tb):,} Kč/TB)".replace(",", " ")
        bits.append(per)
    if (f.get("kind") or "").startswith("hdd"):
        bits.append(badges(f))
    if disk is not None:
        bits.append(smart_text(disk))
    return " · ".join(bits)
