"""Rule-based normalizer for ex-office hardware: business mini/SFF PCs and business laptops.

Leasing returns (ThinkPad, Latitude, EliteBook, OptiPlex, ThinkCentre, EliteDesk, ...) come back in
large, uniform batches, so they price well and flip well. Everything else in the PC and notebook
sections (gaming PCs, MacBooks, consumer laptops, workstations) is excluded as `out_of_scope`, so it
doesn't clutter `hunt review`; only titles that name one of these business lines but can't be pinned
down to a model stay unmatched.

Fujitsu Esprimo desktops ('esprimo d756', 'esprimo q920') and HP's business all-in-ones
('eliteone 800 g3', 'proone 600 g4') are covered because they turn up in state auction lots.

The canonical model is the pricing key, e.g. 'thinkpad t480', 'thinkpad t14 gen 3', 'latitude 7490',
'elitebook 840 g5', 'optiplex 7060 micro', 'thinkcentre m720q', 'elitedesk 800 g4 mini'. A missing
generation or form factor gives a coarser key ('thinkpad t14', 'optiplex 7050'), which mixes
variants: its price is less certain, and the pricing confidence shows that by itself.

RAM, SSD size and CPU are read from the title as features (best effort, often missing).
"""

import re
from dataclasses import dataclass, field

from deal_hunter.text import fold

# Bump whenever the rules change, so `hunt normalize` re-processes old listings.
VERSION = 3  # v3: Fujitsu Esprimo, HP EliteOne/ProOne all-in-ones


@dataclass(frozen=True)
class OfficeResult:
    model: str | None = None
    category: str | None = None  # office_pc | office_laptop
    features: dict = field(default_factory=dict, compare=False)
    excluded_reason: str | None = None

    @property
    def status(self) -> str:
        if self.model:
            return "matched"
        return "excluded" if self.excluded_reason else "unmatched"


def prepare(title: str) -> str:
    """Lower case, no accents, punctuation to spaces; letters and digits stay together.

    'Lenovo ThinkPad T14s G.2' -> 'lenovo thinkpad t14s g2'; 'HP 400 G2.5' -> 'hp 400 g2.5'.
    """
    t = fold(title.replace("×", " x "))
    t = re.sub(r"\bg\.\s?(?=\d)", "g", t)  # 'G.2' -> 'g2'
    t = re.sub(r"(?<=\d)[.,](?=\d)", "#", t)  # keep decimal points ('g2.5', '2,5tb')
    t = re.sub(r"[^a-z0-9#+]+", " ", t).replace("#", ".")
    return " ".join(t.split())


# --- exclusions -----------------------------------------------------------------------------------

WANTED = re.compile(
    r"\b(koupim|kupim|shanim|hledam|vykup|vykupuji|vykupujeme|vykoupim|poptavam|sezenu|koupime)\b"
)
# Parts and accessories, when they come before the machine in the title: 'dokovaci stanice thinkpad'
# is a dock, 'thinkpad t480 + dokovaci stanice' is a laptop that comes with one.
ACCESSORY = re.compile(
    r"\b(nabijec\w*|adapter\w*|zdroj\w*|napajec\w*|baterie|baterk\w*|akumulator\w*|dok|dokovac\w*|docking"
    r"|dock|replikator\w*|klavesnic\w*|displej\w*|display|lcd|matrice|panel|zakladni desk\w*|motherboard"
    r"|deska|kryt\w*|palmrest|pant\w*|ventilator\w*|chladic\w*|kabel\w*|flex|brasn\w*|task\w*|obal\w*"
    r"|batoh\w*|stojan\w*|drzak\w*|vesa|touchpad|reproduktor\w*|webkamer\w*|nahradni dil\w*|dily|kostr\w*"
    r"|sasi|connector|konektor\w*)\b"
)
# Storage and memory sold on their own ('SSD disk do ThinkPad'); only when they come first, since
# 'ThinkPad T480, 16GB RAM, SSD' is the normal way to describe a laptop.
PART = re.compile(r"\b(disk\w*|ssd|hdd|ram|pamet\w*)\b")
BUNDLE = re.compile(r"\b(monitor\w*|tiskarn\w*|lcd \d\d)\b")
MULTIPLE = re.compile(
    r"^\d{1,2} ?(x|ks|kusy|kusu)\b|\b([2-9]|\d{2}) ?(ks|kusy|kusu)\b"
    r"|\b([2-9]|\d{2}) ?x (sestav\w*|pc|pocitac\w*|notebook\w*|ntb|lenovo|dell|hp|thinkpad|thinkcentre"
    r"|optiplex|latitude|elitebook|probook|elitedesk|prodesk)\b"
)

# --- business lines -------------------------------------------------------------------------------

THINKPAD = re.compile(r"\bthink ?pad\b")
THINKCENTRE = re.compile(r"\bthink ?cent(re|er|rum)\b")
LENOVO = re.compile(r"\blenovo\b")
# Lenovo lines that aren't ThinkPad/ThinkCentre: a bare 'Lenovo E14' next to these words is no ThinkPad.
LENOVO_OTHER = re.compile(
    r"\b(ideapad|ideacentre|thinkbook|thinkstation|legion|yoga|loq|v\d{2,3}|b\d{2}|flex|chromebook)\b"
)

# 'x1 carbon gen 9', 'x1 yoga', 'x1 extreme', 'x1 nano'
X1 = re.compile(r"\bx1 (carbon|yoga|extreme|nano|tablet|titanium|fold)\b")
# t480, t480s, t14, t14s, p14s, p15v, x13, x390, l590, e14, e330, p1, x13 yoga
THINKPAD_MODEL = re.compile(r"\b([txlpe])(\d{1,3})(s|v|p|i|e)?(?: (yoga))?\b")
THINKPAD_OTHER = re.compile(
    r"\bthink ?pad (\d{2,3}e?|helix|w\d{3}|z\d{2}|a\d{3}|r\d{2,3}|sl\d{3}|yoga \d{3}|edge)\b"
)

THINKCENTRE_OTHER = re.compile(r"\b(a\d{2}z?|e\d{2}z?|edge|neo)\b")
THINKCENTRE_MODEL = re.compile(r"\bm\s?(\d{2,4})([a-z])?\b")

LATITUDE = re.compile(r"\blatitude\b")
LATITUDE_MODEL = re.compile(r"\blatitude (?:\w+ )?([a-z]?)(\d{3,4})\b")
DELL_E = re.compile(r"\bdell (?:latitude )?(e[57]\d{3})\b")  # 'Dell E5470': the E-prefix is Latitude-only
DELL_BARE = re.compile(r"\bdell ([57][2-5]\d[01])\b")
DELL_OTHER = re.compile(r"\b(inspiron|vostro|xps|precision|alienware|g\d{1,2})\b")
OPTIPLEX = re.compile(
    r"\boptiplex(?: (?:micro|mff|usff|sff|mt|dt|tower|plus|aio|all in one|small form factor))* (\d{3,4})(m)?\b"
)

HP_BOOK = re.compile(r"\b(elitebook|probook)(?: (?:folio|revolve))?(?: (x360))? (\d{3,4})([a-z])?\b")
HP_DESK = re.compile(r"\b(elitedesk|prodesk)(?: (?:mini|sff|dm))? (\d{3})\b")
HP_NEW_MINI = re.compile(r"\b(elite|pro) mini (\d{3})\b")  # 2022+ naming: 'HP Elite Mini 800 G9'
HP_BARE = re.compile(r"\bhp (\d{3,4}) g\d")  # 'HP 840 G2', 'HP 405 G4' without the line name
HP_GEN = re.compile(r"\bg ?(\d{1,2}(?:\.\d)?)\b")
ELITEBOOK_NUMBERS = re.compile(r"^(8[2-5]0|7[2-5][05]|10[34]0)$")
PROBOOK_NUMBERS = re.compile(r"^(4[3-7][05]|6[3-5][05])$")
ELITEDESK_NUMBERS = re.compile(r"^(705|800|805|880)$")
PRODESK_NUMBERS = re.compile(r"^(400|405|600|680)$")

HP_AIO = re.compile(r"\b(elite|pro) ?one(?: aio)? (\d{3,4})\b")  # 'HP EliteOne 800 G3', 'ProOne 600 G4'

FUJITSU = re.compile(r"\b(fujitsu|esprimo)\b")
# Esprimo D/P/Q (desktop, tower, mini) 5xx-9xx ('D756', 'Q920', 'P958') and the 2020+ names
# ('D7010', 'G9010'). 'D9510' (2008), 'J550', 'E5730' and the like are recognised but not priced.
ESPRIMO_MODEL = re.compile(r"\b([dpqg])([5-9]\d\d|[5-9]01[0-3])\b")
ESPRIMO_OLD = re.compile(r"\b([a-z])(\d{3,4})\b")
FUJITSU_OTHER = re.compile(r"\b(celsius|lifebook|stylistic|primergy|scenic|futro)\b")

GEN = re.compile(r"\b(?:gen|g)\s?(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)\s?gen\b")

FORM_TINY = re.compile(r"\b(tiny|mini|micro|mff|usff|dm|ultra small|desktop mini)\b")
FORM_SFF = re.compile(r"\b(sff|small form factor)\b")
FORM_TOWER = re.compile(r"\b(tower|mt|minitower|twr|microtower)\b")


def form_factor(t: str) -> str | None:
    if FORM_TINY.search(t):
        return "tiny"
    if FORM_SFF.search(t):
        return "sff"
    if FORM_TOWER.search(t):
        return "tower"
    return None


def generation(t: str, start: int = 0) -> int | None:
    m = GEN.search(t, start)
    if m is None:
        return None
    gen = int(m.group(1) or m.group(2))
    return gen if 1 <= gen <= 13 else None


# A model found in a title: (key, category, position in the prepared title)
Found = tuple[str, str, int]


def _thinkpad(t: str) -> list[Found] | None:
    """ThinkPad T/X/L/P/E models. None = not a ThinkPad title at all."""
    named = bool(THINKPAD.search(t))
    if not named and not (LENOVO.search(t) and not LENOVO_OTHER.search(t)):
        return None
    found = []
    x1 = X1.search(t)
    if x1:
        gen = generation(t, x1.end())
        found.append(
            (f"thinkpad x1 {x1.group(1)}" + (f" gen {gen}" if gen else ""), "office_laptop", x1.start())
        )
    for m in THINKPAD_MODEL.finditer(t):
        letter, number, suffix, yoga = m.groups()
        if x1 and m.start() == x1.start():
            continue
        if letter == "x" and number == "1":
            continue  # bare 'x1': which one?
        if letter == "e" and len(number) == 1:
            continue
        if letter == "p" and (len(number) == 3 or (len(number) == 1 and number != "1")):
            continue  # ThinkPad P is p1/p14s/p52; a 'Lenovo P320' is a ThinkStation tower
        name = f"{letter}{number}{suffix or ''}" + (" yoga" if yoga else "")
        if len(number) <= 2:  # the newer naming needs a generation: t14 gen 3, p1 gen 4
            gen = generation(t, m.end())
            name += f" gen {gen}" if gen else ""
        found.append((f"thinkpad {name}", "office_laptop", m.start()))
    if not found and (other := THINKPAD_OTHER.search(t)):
        found.append((f"thinkpad {other.group(1)}", "office_laptop", other.start()))  # out of scope
    if not found and not named:
        return None
    return found


def _thinkcentre(t: str) -> list[Found] | None:
    named = bool(THINKCENTRE.search(t))
    if not named and not (LENOVO.search(t) and not LENOVO_OTHER.search(t)):
        return None
    found = []
    for m in THINKCENTRE_MODEL.finditer(t):
        number, suffix = m.groups()
        if suffix and suffix not in "qsteapz":
            continue
        name = f"m{number}{suffix or ''}"
        if len(number) == 2 and suffix in ("q", "s", "t", "e"):
            gen = generation(t, m.end())
            name += f" gen {gen}" if gen else ""
        if suffix not in ("q", "s", "t"):
            ff = form_factor(t)
            name += f" {ff}" if ff else ""
        found.append((f"thinkcentre {name}", "office_pc", m.start()))
    if not found and named and (other := THINKCENTRE_OTHER.search(t)):
        found.append((f"thinkcentre {other.group(1)}", "office_pc", other.start()))  # out of scope
    if not found and not named:
        return None
    return found


def _dell(t: str) -> list[Found] | None:
    found: list[Found] = []
    named = False
    if LATITUDE.search(t):
        named = True
        if m := LATITUDE_MODEL.search(t):
            prefix, number = m.groups()
            found.append((f"latitude {prefix}{number}", "office_laptop", m.start()))
    else:
        m = DELL_E.search(t)
        if m:
            found.append((f"latitude {m.group(1)}", "office_laptop", m.start()))
    for m in OPTIPLEX.finditer(t):
        named = True
        ff = "tiny" if m.group(2) else form_factor(t)  # 'OptiPlex 3020M' = Micro
        plus = " plus" if re.search(r"\bplus\b", t) else ""
        found.append(
            (f"optiplex {m.group(1)}{plus}" + (f" {_dell_ff(ff)}" if ff else ""), "office_pc", m.start())
        )
    if re.search(r"\boptiplex\b", t):
        named = True
    if not found and DELL_BARE.search(t) and not DELL_OTHER.search(t):
        named = True  # 'Dell 5420': probably a Latitude, but Inspirons share numbers -> `hunt review`
    if not found and not named:
        return None
    return found


def _dell_ff(ff: str) -> str:
    return {"tiny": "micro", "sff": "sff", "tower": "tower"}[ff]


def _hp_ff(ff: str | None) -> str:
    return {"tiny": " mini", "sff": " sff", "tower": " tower", None: ""}[ff]


def _hp(t: str) -> list[Found] | None:
    found: list[Found] = []
    named = False
    for m in HP_BOOK.finditer(t):
        named = True
        line, x360, number, letter = m.groups()
        gen = HP_GEN.match(t, m.end() + 1) if m.end() < len(t) else None
        name = f"{line} " + ("x360 " if x360 else "") + number + (letter or "")
        name += f" g{gen.group(1)}" if gen else ""
        found.append((name, "office_laptop", m.start()))
    for m in HP_DESK.finditer(t):
        named = True
        line, number = m.groups()
        gen = HP_GEN.match(t, m.end() + 1) if m.end() < len(t) else None
        name = f"{line} {number}" + (f" g{gen.group(1)}" if gen else "") + _hp_ff(form_factor(t))
        found.append((name, "office_pc", m.start()))
    for m in HP_NEW_MINI.finditer(t):
        named = True
        line = "elitedesk" if m.group(1) == "elite" else "prodesk"
        gen = HP_GEN.match(t, m.end() + 1) if m.end() < len(t) else None
        found.append(
            (f"{line} {m.group(2)}" + (f" g{gen.group(1)}" if gen else "") + " mini", "office_pc", m.start())
        )
    if re.search(r"\b(elitebook|probook|elitedesk|prodesk)\b", t):
        named = True
    if not named:
        for m in HP_BARE.finditer(t):
            number = m.group(1)
            gen = HP_GEN.search(t, m.end() - 2)
            g = f" g{gen.group(1)}" if gen else ""
            if ELITEBOOK_NUMBERS.match(number):
                found.append((f"elitebook {number}{g}", "office_laptop", m.start()))
            elif PROBOOK_NUMBERS.match(number):
                found.append((f"probook {number}{g}", "office_laptop", m.start()))
            elif ELITEDESK_NUMBERS.match(number):
                found.append((f"elitedesk {number}{g}{_hp_ff(form_factor(t))}", "office_pc", m.start()))
            elif PRODESK_NUMBERS.match(number):
                found.append((f"prodesk {number}{g}{_hp_ff(form_factor(t))}", "office_pc", m.start()))
    if not found and not named:
        return None
    return found


def _hp_aio(t: str) -> list[Found] | None:
    found: list[Found] = []
    for m in HP_AIO.finditer(t):
        line, number = m.groups()
        gen = HP_GEN.match(t, m.end() + 1) if m.end() < len(t) else None
        found.append((f"{line}one {number}" + (f" g{gen.group(1)}" if gen else ""), "office_pc", m.start()))
    if not found and not re.search(r"\b(elite|pro) ?one\b", t):
        return None
    return found


def _fujitsu(t: str) -> list[Found] | None:
    if not FUJITSU.search(t):
        return None
    if other := FUJITSU_OTHER.search(t):
        return [(f"fujitsu {other.group(1)}", "office_pc", other.start())]  # out of scope
    found: list[Found] = []
    for m in ESPRIMO_MODEL.finditer(t):
        found.append((f"esprimo {m.group(1)}{m.group(2)}", "office_pc", m.start()))
    if not found and re.search(r"\besprimo\b", t) and (old := ESPRIMO_OLD.search(t, t.index("esprimo"))):
        found.append((f"esprimo {old.group(1)}{old.group(2)}", "office_pc", old.start()))  # out of scope
    return found


def find_models(t: str) -> tuple[list[Found], bool]:
    """(models found, whether any business line was named at all)."""
    found: list[Found] = []
    named = False
    for finder in (_thinkpad, _thinkcentre, _dell, _hp, _hp_aio, _fujitsu):
        hits = finder(t)
        if hits is None:
            continue
        named = True
        for hit in hits:
            if hit[0] not in [f[0] for f in found]:
                found.append(hit)
    return found, named


# --- scope ------------------------------------------------------------------------------------------

# The models we price. A business line outside these (Latitude 3xxx/E6xxx, ThinkPad 11e/W/A/R,
# OptiPlex 7xx, pre-2013 'ProBook 6460b'-style names) is recognised but excluded as out_of_scope.
IN_SCOPE = re.compile(
    r"^(thinkpad ([txlpe]\d|x1 )"
    r"|thinkcentre m"
    r"|latitude e?[57]\d{3}$"
    r"|optiplex [3579]\d{3}\b"
    r"|elitebook (x360 )?(8[1-6]0|7[2-5][05]|6[3-5]0|10[34]0)( g[\d.]+)?$"
    r"|probook (4[3-7][05]|6[3-5][05])( g[\d.]+)?$"
    r"|elitedesk|prodesk"
    r"|eliteone (800|705|840|870|1000)( g\d)?$"
    r"|proone (400|440|600|640)( g\d)?$"
    r"|esprimo ([dpq][5-9]\d\d|[dpqg][5-9]01[0-3])$)"
)


def _scope_ok(key: str) -> bool:
    return bool(IN_SCOPE.search(key))


# --- features ---------------------------------------------------------------------------------------

SIZE = re.compile(
    r"(?<![\w.])(?<!\b\d/)(?<!\b\d\d/)(\d{1,4}(?:\.\d)?) ?(tb|gb|g)(?=\b|ram|ssd|nvme|hdd|ddr|emmc)( ?(?:ram|ddr\d?|ssd|nvme|hdd|emmc|m 2))?"
)
RAM_SIZES = {2, 4, 6, 8, 12, 16, 20, 24, 32, 40, 48, 64}
CPU_INTEL = re.compile(r"\b(i[3579])(?: |-)?(\d{4,5}[a-z]{0,2}\d?)?\b")
CPU_ULTRA = re.compile(r"\b(?:core )?ultra ?([579]) ?(\d{3}[a-z]{0,2})?\b")
CPU_RYZEN = re.compile(r"\bryzen ?([3579])(?: pro)? ?(\d{4}[a-z]{0,2})?\b")


def features(title: str) -> dict:
    """{'ram_gb', 'ssd_gb', 'cpu'} from the title, each only when it is reasonably clear."""
    t = fold(title)
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    t = re.sub(r"[^a-z0-9./]+", " ", t)
    t = re.sub(r"\b(ssd|nvme|hdd|ram)(?=\d)", r"\1 ", t)  # 'ssd256gb' -> 'ssd 256gb'
    out: dict = {}
    ram_labeled, ram_plain, ssd_labeled, storage_plain = [], [], [], []
    for m in SIZE.finditer(t):
        value, unit, label = float(m.group(1)), m.group(2), (m.group(3) or "").strip()
        before = t[max(0, m.start() - 6) : m.start()].split()
        before = before[-1] if before else ""
        gb = int(value * 1024) if unit == "tb" else int(value)
        if label and not label.startswith(("ram", "ddr")) and gb < 64:
            label = ""  # '16GB, SSD 512GB': the SSD label belongs to the next number
        kind = label or (before if before in ("ram", "ssd", "nvme", "hdd", "emmc") else "")
        if kind.startswith(("ram", "ddr")):
            ram_labeled.append(gb)
        elif kind in ("ssd", "nvme", "emmc", "m 2"):
            ssd_labeled.append(gb)
        elif kind == "hdd":
            continue
        elif unit == "tb" or gb >= 120:
            storage_plain.append(gb)
        elif gb in RAM_SIZES:
            ram_plain.append(gb)
    ram = (ram_labeled or ram_plain or [None])[0]
    ssd = (ssd_labeled or storage_plain or [None])[0]
    if ram:
        out["ram_gb"] = ram
    if ssd and 16 <= ssd <= 8192:
        out["ssd_gb"] = ssd
    if m := CPU_ULTRA.search(t):
        out["cpu"] = f"ultra {m.group(1)}" + (f" {m.group(2)}" if m.group(2) else "")
    elif m := CPU_INTEL.search(t):
        out["cpu"] = m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")
    elif m := CPU_RYZEN.search(t):
        out["cpu"] = f"ryzen {m.group(1)}" + (f" {m.group(2)}" if m.group(2) else "")
    return out


# --- the normalizer -----------------------------------------------------------------------------------


def normalize(title: str) -> OfficeResult:
    t = prepare(title)
    if WANTED.search(t):
        return OfficeResult(excluded_reason="wanted")
    found, named = find_models(t)
    if not named:
        return OfficeResult(excluded_reason="out_of_scope")
    first = min((f[2] for f in found), default=None)
    line = re.search(
        r"\b(think ?pad|think ?cent\w*|lenovo|latitude|optiplex|dell|elitebook|probook|elitedesk|prodesk|hp"
        r"|eliteone|proone|fujitsu|esprimo)\b",
        t,
    )
    anchor = first if first is not None else (line.start() if line else len(t))
    if line is not None:
        anchor = min(anchor, line.start())
    part = ACCESSORY.search(t)
    spec = PART.search(t)
    if (part is not None and (part.start() < anchor or not found)) or (
        spec is not None and spec.start() < anchor
    ):
        return OfficeResult(excluded_reason="accessory")
    if BUNDLE.search(t):
        return OfficeResult(excluded_reason="bundle")
    if MULTIPLE.search(t):
        return OfficeResult(excluded_reason="multiple")
    if len(found) > 1:
        return OfficeResult(excluded_reason="multiple")
    if not found:
        return OfficeResult()  # a business line, but which model? -> `hunt review`
    key, category, _ = found[0]
    if not _scope_ok(key):
        return OfficeResult(excluded_reason="out_of_scope")
    return OfficeResult(key, category, features(title))
