"""Rule-based normalizer for Apple computers: MacBook Air/Pro, iMac, Mac mini, Mac Studio.

Apple hardware was previously lumped into office.py's `out_of_scope` bucket (it names no business
line), even though it is a large, well-defined slice of the pc/notebook sections (~1.6k ads).

The canonical model is the pricing key:

- Apple Silicon: 'macbook air m2 13', 'macbook pro m1 pro 14', 'imac m1 24', 'mac mini m2',
  'mac studio m2 max'. The chip (m1-m4, optionally pro/max/ultra) comes straight from the title;
  the screen size defaults when the title doesn't give one (`_default_screen`), since Air/Pro
  only ever shipped in a couple of sizes per chip generation -- the same "coarser key when a detail
  is missing" trade-off office.py makes for a ThinkPad T14 without a generation.
- Intel-era Macs (pre Apple Silicon): keyed by year when the title states one ('macbook pro intel
  2017 13', 'imac 27 2017'), else by screen size alone ('macbook pro intel 13') -- coarser still,
  mixing several years' worth of specs under one key, same trade-off again.

A bare 'MacBook' (no Air/Pro) or a Mac mini/Studio/iMac with neither a chip nor a year nor a usable
size is left unmatched (`hunt review`): which model, exactly, is unclear from the title alone.

iPads, iPhones, Watches, AirPods and chargers/cases/batteries sold on their own are not Mac
computers at all, and are excluded (`accessory` if they're literally an accessory of a Mac,
`out_of_scope` otherwise -- reusing office.py's reasons rather than inventing new ones). A machine
"na díly" (for parts, not sold working) is excluded `accessory` too: not a working-computer sale.

RAM, SSD and (for Intel Macs) CPU are read from the title with office.features(), widened with
Apple Silicon's unusual RAM sizes (36/96 GB).
"""

import re
from dataclasses import dataclass, field

from deal_hunter.office import features as _office_features
from deal_hunter.text import fold

# Bump whenever the rules change, so `hunt normalize` re-processes old listings.
VERSION = 1

APPLE_RAM_SIZES = frozenset({36, 96})  # Apple Silicon configs office.RAM_SIZES doesn't have


@dataclass(frozen=True)
class AppleResult:
    model: str | None = None
    category: str | None = None  # apple (laptop) | apple_desktop
    features: dict = field(default_factory=dict, compare=False)
    excluded_reason: str | None = None

    @property
    def status(self) -> str:
        if self.model:
            return "matched"
        return "excluded" if self.excluded_reason else "unmatched"


def prepare(title: str) -> str:
    """Lower case, no accents; an inch mark after a number becomes ' in' so screen size survives
    punctuation stripping. 'MacBook Air 13” M2' -> 'macbook air 13 in m2'."""
    t = fold(title.replace("×", " x "))
    t = re.sub(r"(\d{1,2}(?:[.,]\d)?)\s*(?:[\"”“″]|-?inch\w*|palc\w*)", r"\1 in", t)
    t = re.sub(r"(?<=\d)[.,](?=\d)", "#", t)
    t = re.sub(r"[^a-z0-9#+]+", " ", t).replace("#", ".")
    return " ".join(t.split())


def features(title: str) -> dict:
    return _office_features(title, extra_ram_sizes=APPLE_RAM_SIZES)


# --- exclusions -----------------------------------------------------------------------------------

WANTED = re.compile(
    r"\b(koupim|kupim|shanim|hledam|vykup|vykupuji|vykupujeme|vykoupim|poptavam|sezenu|koupime)\b"
)
ACCESSORY = re.compile(
    r"\b(nabijec\w*|adapter\w*|zdroj\w*|napajec\w*|baterie|baterk\w*|akumulator\w*|dok|dokovac\w*|docking"
    r"|dock|klavesnic\w*|displej\w*|display|lcd|matrice|panel|kryt\w*|palmrest|pant\w*|ventilator\w*"
    r"|chladic\w*|kabel\w*|flex|brasn\w*|task\w*|obal\w*|batoh\w*|stojan\w*|drzak\w*|vesa|touchpad"
    r"|reproduktor\w*|webkamer\w*|nahradni dil\w*|dily|kostr\w*|sasi|nabijecka|myska|magsafe)\b"
)
PARTS = re.compile(r"\bna dily\b|\bnefunk\w*\b|\brozbit\w*\b|\bnejede\b|\bbez (zivotnosti|baterie)\b")
BUNDLE = re.compile(r"\b(monitor\w*|tiskarn\w*|ipad\w*|iphone\w*)\b")
MULTIPLE = re.compile(r"^\d{1,2} ?(x|ks|kusy|kusu)\b|\b([2-9]|\d{2}) ?(ks|kusy|kusu)\b")

# --- lines ------------------------------------------------------------------------------------------

MACBOOK = re.compile(r"\bmac ?book")  # no trailing \b: 'MacbookPro'/'Macbookair' fuse the line on
AIR = re.compile(r"\bair\b")
PRO_LINE = re.compile(r"\bpro\b")
IMAC = re.compile(r"\bimac\b")
MAC_MINI = re.compile(r"\bmac ?mini\b")
MAC_STUDIO = re.compile(r"\bmac ?studio\b")

# 'm1', 'm4 pro', 'm4pro' (sellers fuse the chip variant on; no space required before pro/max/ultra).
CHIP = re.compile(r"\bm([1-4])\s?(pro|max|ultra)?\b")
SCREEN_MARK = re.compile(r"\b(13|14|15|16)(?:#\d)? in\b")
SCREEN_LEADING = re.compile(r"^(13|14|15|16)\b")
IMAC_SCREEN = re.compile(r"\b(21\.5|21|24|27)(?:\s?in)?\b")
# Intel Macs: realistic model years only (2006-2020), so a warranty year ("zaruka do 2028") never matches.
YEAR = re.compile(r"\b(200[6-9]|201[0-9]|2020)\b")

# A bare 13/14/15/16 elsewhere in the title is the screen size *unless* it's a RAM/storage number
# that happens to use a space before its unit ('16 GB RAM' rather than the usual fused '16gb').
UNIT_WORDS = {"gb", "tb", "g", "ram", "ssd", "nvme", "emmc", "ddr", "ddr3", "ddr4", "ddr5"}


def _screen(t: str) -> int | None:
    if m := SCREEN_MARK.search(t):
        return int(m.group(1))
    if m := SCREEN_LEADING.match(t):
        return int(m.group(1))
    tokens = t.split()
    for i, tok in enumerate(tokens):
        if tok in ("13", "14", "15", "16"):
            before = tokens[i - 1] if i > 0 else ""
            after = tokens[i + 1] if i + 1 < len(tokens) else ""
            if before in UNIT_WORDS or after in UNIT_WORDS:
                continue
            return int(tok)
    return None


def _default_screen(line: str, chip: str) -> int:
    if line == "air":
        return 13
    return 13 if chip in ("m1", "m2") else 14  # M1/M2 base Pro is 13"; everything else is 14 or 16


# A model found in a title: (key, category, position in the prepared title)
Found = tuple[str, str, int]


def _chip_str(m: re.Match) -> str:
    return f"m{m.group(1)}" + (f" {m.group(2)}" if m.group(2) else "")


def _macbook(t: str) -> list[Found] | None:
    m = MACBOOK.search(t)
    if m is None:
        return None
    rest = t[m.end() :]
    line = (
        "air" if AIR.search(rest[:20]) or AIR.search(t) else ("pro" if PRO_LINE.search(rest[:10]) else None)
    )
    if line is None:
        return []  # bare 'MacBook' (no Air/Pro): a pre-2012 model, unclear which -> `hunt review`
    chip_m = CHIP.search(t, m.end())
    if chip_m:
        chip = _chip_str(chip_m)
        screen = _screen(t) or _default_screen(line, chip)
        return [(f"macbook {line} {chip} {screen}", "apple", m.start())]
    year_m = YEAR.search(t)
    screen = _screen(t)
    if year_m and screen:
        return [(f"macbook {line} intel {year_m.group(0)} {screen}", "apple", m.start())]
    if screen:
        return [(f"macbook {line} intel {screen}", "apple", m.start())]
    return []  # named, but no chip/year/screen to pin a key -> unmatched


def _imac(t: str) -> list[Found] | None:
    m = IMAC.search(t)
    if m is None:
        return None
    chip_m = CHIP.search(t, m.end())
    if chip_m:
        screen = _screen(t) or 24  # the only size Apple Silicon iMacs ship in
        return [(f"imac m{chip_m.group(1)} {screen}", "apple_desktop", m.start())]
    year_m = YEAR.search(t)
    screen_m = IMAC_SCREEN.search(t)
    screen = screen_m.group(1) if screen_m else None
    if year_m and screen:
        return [(f"imac {screen} {year_m.group(0)}", "apple_desktop", m.start())]
    if screen:
        return [(f"imac {screen}", "apple_desktop", m.start())]
    return []


def _mac_mini(t: str) -> list[Found] | None:
    m = MAC_MINI.search(t)
    if m is None:
        return None
    chip_m = CHIP.search(t, m.end())
    if chip_m:
        return [(f"mac mini {_chip_str(chip_m)}", "apple_desktop", m.start())]
    if year_m := YEAR.search(t):
        return [(f"mac mini intel {year_m.group(0)}", "apple_desktop", m.start())]
    return []


def _mac_studio(t: str) -> list[Found] | None:
    m = MAC_STUDIO.search(t)
    if m is None:
        return None
    chip_m = CHIP.search(t, m.end())
    if chip_m:
        return [(f"mac studio {_chip_str(chip_m)}", "apple_desktop", m.start())]
    return []  # no Intel Mac Studio exists; without a chip, which one is it?


def find_models(t: str) -> tuple[list[Found], bool]:
    """(models found, whether an Apple computer line was named at all)."""
    found: list[Found] = []
    named = False
    for finder in (_macbook, _imac, _mac_mini, _mac_studio):
        hits = finder(t)
        if hits is None:
            continue
        named = True
        for hit in hits:
            if hit[0] not in [f[0] for f in found]:
                found.append(hit)
    return found, named


def normalize(title: str) -> AppleResult:
    t = prepare(title)
    if WANTED.search(t):
        return AppleResult(excluded_reason="wanted")
    found, named = find_models(t)
    if not named:
        # Not a Mac at all (iPad/iPhone/Watch/AirPods) or no Apple line named: same outcome either
        # way -- out_of_scope, like office.py's "no model at all: not even an accessory of one".
        return AppleResult(excluded_reason="out_of_scope")
    first = min((f[2] for f in found), default=len(t))
    part = ACCESSORY.search(t)
    if part is not None and part.start() < first:
        return AppleResult(excluded_reason="accessory")
    if PARTS.search(t):
        return AppleResult(excluded_reason="accessory")
    if BUNDLE.search(t):
        return AppleResult(excluded_reason="bundle")
    if MULTIPLE.search(t):
        return AppleResult(excluded_reason="multiple")
    if len(found) > 1:
        return AppleResult(excluded_reason="multiple")
    if not found:
        return AppleResult()  # an Apple line, but no model pinned down -> `hunt review`
    key, category, _ = found[0]
    return AppleResult(key, category, features(title))
