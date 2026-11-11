"""Rule-based normalizer for consumer and gaming laptops (IdeaPad/Legion/Yoga, Pavilion/Omen/Victus,
Aspire/Nitro/Predator, Vivobook/ROG/TUF, MSI, ...): everything office.py and apple.py don't claim.

These don't come in uniform leasing batches with a stable model number the way ThinkPads/Latitudes
do, and there are far too many consumer model numbers (Lenovo alone has hundreds of IdeaPad/Legion
SKUs) to catalog one by one. Instead the pricing key is a **spec bucket**: what actually drives a
gaming/consumer laptop's resale value is its CPU tier+generation and its GPU, not the exact SKU --

    'laptop i7 g12 rtx 4060'   (Core i7, 12th gen, RTX 4060)
    'laptop r5 5000 igpu'      (Ryzen 5, 5000 series, no discrete GPU)
    'laptop rtx 5070'          (GPU only -- no CPU stated; happens a lot with "wow spec" gaming ads)

CPU bucket: 'i{3,5,7,9} g{gen}' (classic Core, generation from the model number -- '1135G7' -> gen
11, '12700H' -> gen 12, '8265U' -> gen 8), 'ultra{5,7,9} s{100,200}' (Core Ultra, by hundred-series:
Meteor Lake/Lunar Lake 100s vs Arrow Lake 200s), 'r{3,5,7,9} {series}' (Ryzen, by thousand-series --
'5600H' -> 5000), 'rai{7,9} {series}' (Ryzen AI, by hundred-series). A bare tier with no model
number ('Ryzen 5', no suffix) still gives the tier alone ('r5'): coarser, same trade-off office.py
takes for a ThinkPad T14 without a generation. No CPU at all in the title drops the CPU component
from the key entirely (not a placeholder): the key is then GPU-only.

GPU bucket: the exact model name from gpu_catalog.yaml (normalize.py's desktop catalog, reused as-is
-- no VRAM suffix, since a laptop GPU's VRAM often differs from its desktop namesake and the catalog
only knows the desktop one) plus a handful of laptop-exclusive chips the desktop catalog has no entry
for at all (RTX 2050/3050 Ti/4050, GTX 1650 Ti). No discrete GPU found -> 'igpu'. A legacy
'<number>M' mobile suffix (970M, 1060M: a differently-specced chip, not the same-numbered desktop
card) is masked out before catalog lookup rather than matched as the desktop chip, so it reads
'igpu' too; a modern 'Laptop GPU'/'Mobile'/'Max-Q' suffix needs no such handling, since that chip
really is the desktop one, power-limited, and matches its number as-is.

Category: `herni` if a discrete GPU was found (gpu_catalog.yaml only starts at GTX 10-series, so any
hit is already gaming-tier) or a gaming-line brand name is in the title (Legion, ROG, TUF, Nitro,
Predator, Omen, Victus, MSI, Aorus, Alienware, Loq, ...); `ostatni` otherwise.

A title with neither a CPU, a discrete GPU nor a gaming-line name ('Notebook Asus Vivobook 15
x507m', 'Lenovo IdeaPad 3') is left unmatched: spec bucketing has nothing to bucket on, same as a
bare 'Dell OptiPlex' with no number in office.py.

This module only runs on ads already confirmed to be a laptop: the notebook section, or (for the pc
section, since office.py historically treats pc/notebook as interchangeable) a title that says
'notebook'/'laptop'/'ntb'. Without that gate a gaming *desktop* ad ("Herní PC RTX 4060, Ryzen 5
5600") would otherwise bucket the same way a gaming laptop does -- desktops are a later phase.

RAM and SSD are read with office.features().
"""

import re
from dataclasses import dataclass, field

from deal_hunter.normalize import find_models as _gpu_find_models
from deal_hunter.normalize import prepare as _gpu_prepare
from deal_hunter.office import features
from deal_hunter.text import fold
from deal_hunter.title_filters import WANTED

# Bump whenever the rules change, so `hunt normalize` re-processes old listings.
VERSION = (
    3  # v3: screen resolutions ('1920x1080') no longer read as a GPU; v2: mobile '<number>M' suffixes, 'víko'
)


@dataclass(frozen=True)
class LaptopResult:
    model: str | None = None
    category: str | None = None  # herni | ostatni
    features: dict = field(default_factory=dict, compare=False)
    excluded_reason: str | None = None

    @property
    def status(self) -> str:
        if self.model:
            return "matched"
        return "excluded" if self.excluded_reason else "unmatched"


def prepare(title: str) -> str:
    t = fold(title.replace("×", " x "))
    t = re.sub(r"(?<=\d)[.,](?=\d)", "#", t)
    t = re.sub(r"[^a-z0-9#+]+", " ", t).replace("#", ".")
    return " ".join(t.split())


# --- exclusions -----------------------------------------------------------------------------------

ACCESSORY = re.compile(
    r"\b(nabijec\w*|adapter\w*|zdroj\w*|napajec\w*|baterie|baterk\w*|akumulator\w*|dok|dokovac\w*|docking"
    r"|dock|replikator\w*|klavesnic\w*|displej\w*|display|lcd|matrice|panel|kryt\w*|palmrest|pant\w*"
    r"|ventilator\w*|chladic\w*|kabel\w*|flex|brasn\w*|task\w*|obal\w*|batoh\w*|stojan\w*|drzak\w*|vesa"
    r"|touchpad|reproduktor\w*|webkamer\w*|nahradni dil\w*|dily|kostr\w*|sasi|connector|konektor\w*|viko\w*)\b"
)
BUNDLE = re.compile(r"\b(monitor\w*|tiskarn\w*)\b")
MULTIPLE = re.compile(r"^\d{1,2} ?(x|ks|kusy|kusu)\b|\b([2-9]|\d{2}) ?(ks|kusy|kusu)\b")

NOTEBOOK_WORD = re.compile(r"\b(notebook\w*|laptop\w*|ntb)\b")


def _is_laptop(t: str, section: str) -> bool:
    """Only the notebook section (or an explicit 'notebook'/'laptop'/'ntb' word, for a laptop
    mis-posted in pc) counts: the pc section is desktops, a later phase."""
    return section == "notebook" or bool(NOTEBOOK_WORD.search(t))


# --- CPU --------------------------------------------------------------------------------------------

CPU_RYZEN_AI = re.compile(r"\bryzen ai ?([3579])(?: hx)? ?(\d{3})[a-z]{0,2}\b")
CPU_ULTRA = re.compile(r"\b(?:core )?ultra ?([579]) ?(\d{3})[a-z]{0,2}\b")
CPU_RYZEN = re.compile(r"\bryzen ?([3579])(?: pro)? ?(\d{4})[a-z]{0,3}\b")
CPU_INTEL = re.compile(
    r"\bi([3579]) ?(\d{4,5})[a-z]{0,3}\d?\b"
)  # '1135G7': a letter suffix can end in a digit
CPU_BARE_INTEL = re.compile(r"\bi([3579])\b")
CPU_BARE_RYZEN_AI = re.compile(r"\bryzen ai ?([3579])\b")
CPU_BARE_RYZEN = re.compile(r"\bryzen ?([3579])\b")
CPU_BARE_ULTRA = re.compile(r"\b(?:core )?ultra ?([579])\b")


def _intel_gen(number: str) -> int | None:
    """'1135' (4 digits) -> 11 if it starts with 10/11 (10th/11th gen), else 1 (6th-9th gen, the
    first digit only: '8265' -> 8). A 5-digit number ('13900') -> its first two digits."""
    if len(number) == 5 or number[:2] in ("10", "11"):
        gen = int(number[:2])
    else:
        gen = int(number[0])
    return gen if 4 <= gen <= 15 else None


def cpu_bucket(t: str) -> str | None:
    """The CPU component of the pricing key, or None if no CPU is named at all."""
    if m := CPU_RYZEN_AI.search(t):
        return f"rai{m.group(1)} {m.group(2)[0]}00"
    if m := CPU_ULTRA.search(t):
        return f"ultra{m.group(1)} s{m.group(2)[0]}00"
    if m := CPU_RYZEN.search(t):
        return f"r{m.group(1)} {m.group(2)[0]}000"
    if m := CPU_INTEL.search(t):
        gen = _intel_gen(m.group(2))
        return f"i{m.group(1)} g{gen}" if gen else f"i{m.group(1)}"
    if m := CPU_BARE_INTEL.search(t):
        return f"i{m.group(1)}"
    if m := CPU_BARE_RYZEN_AI.search(t):
        return f"rai{m.group(1)}"
    if m := CPU_BARE_RYZEN.search(t):
        return f"r{m.group(1)}"
    if m := CPU_BARE_ULTRA.search(t):
        return f"ultra{m.group(1)}"
    return None


# --- GPU --------------------------------------------------------------------------------------------

# Laptop-exclusive chips gpu_catalog.yaml (desktop cards) has no entry for at all. The 'ti' models
# are distinctive enough that sellers naming just the number ('3050Ti', no 'RTX') still count.
_LAPTOP_ONLY_GPU = (
    (re.compile(r"\brtx 2050\b"), "rtx 2050"),
    (re.compile(r"\b(?:rtx )?3050 ?ti\b"), "rtx 3050 ti"),
    (re.compile(r"\brtx 4050\b"), "rtx 4050"),
    (re.compile(r"\b(?:gtx )?1650 ?ti\b"), "gtx 1650 ti"),
)

GAMING_LINE = re.compile(
    r"\b(legion|rog|tuf|nitro|predator|omen|victus|zephyrus|katana|vector|titan|stealth|raider|sword"
    r"|crosshair|aorus|alienware|loq|msi|gaming|herni)\b"
)

# Legacy laptop-GPU naming fused a trailing 'M' onto the chip number ('970M', '1060M', '980M'): a
# differently-specced mobile chip, never the same-numbered desktop card gpu_catalog.yaml knows
# about, so a 4-digit one (a 3-digit one, like 970, can't match the catalog's \d{4} models anyway)
# must be masked out before catalog lookup, or '1060M' would wrongly resolve to desktop 'gtx 1060'.
# Modern 'Laptop GPU'/'Mobile'/'Max-Q' suffixes are separate words that don't interfere with
# matching the chip number as-is ('RTX 3060 Laptop GPU' already finds 'rtx 3060' with no help).
MOBILE_SUFFIX_GPU = re.compile(r"\b(\d{4}) m\b")

# A screen resolution ('1920x1080', '2560 x 1440', after prepare() splits it) holds a 4-digit number
# that looks like a GPU ('gtx 1080'): mask it out before catalog lookup.
RESOLUTION = re.compile(r"\b\d{3,4} x \d{3,4}\b")


def gpu_bucket(title: str) -> tuple[str, bool]:
    """(GPU component of the key, whether a gaming-line brand name is in the title)."""
    gt = _gpu_prepare(title)
    gaming_line = bool(GAMING_LINE.search(gt))
    for pattern, name in _LAPTOP_ONLY_GPU:
        if pattern.search(gt):
            return name, gaming_line
    gt = MOBILE_SUFFIX_GPU.sub("", gt)
    gt = RESOLUTION.sub(" ", gt)
    models = _gpu_find_models(gt)
    if models:
        return models[0].name, gaming_line
    return "igpu", gaming_line


# --- the normalizer -----------------------------------------------------------------------------------


# Where the laptop description starts (first CPU-like or GPU-like or gaming-line token): an
# accessory word before that point is a part sold on its own ('Klávesnice pro Legion 5'); after it,
# it's just something that comes with the machine ('Legion 5 + nová klávesnice'), same distinction
# office.py makes for '... + dokovací stanice'.
_ANCHOR = re.compile(r"\b(?:rtx|gtx|rx|geforce|radeon|arc|i[3579]|ryzen|ultra)\w*\b|" + GAMING_LINE.pattern)


def normalize(title: str, section: str = "notebook") -> LaptopResult:
    t = prepare(title)
    if not _is_laptop(t, section):
        return LaptopResult()
    if WANTED.search(t):
        return LaptopResult(excluded_reason="wanted")
    if MULTIPLE.search(t):
        return LaptopResult(excluded_reason="multiple")
    if BUNDLE.search(t):
        return LaptopResult(excluded_reason="bundle")
    anchor_m = _ANCHOR.search(t)
    anchor = anchor_m.start() if anchor_m else len(t)
    part = ACCESSORY.search(t)
    if part is not None and part.start() < anchor:
        return LaptopResult(excluded_reason="accessory")

    cpu = cpu_bucket(t)
    gpu, gaming_line = gpu_bucket(title)
    if cpu is None and gpu == "igpu":
        return LaptopResult()  # a brand/model name only, no spec at all -> `hunt review`

    category = "herni" if (gpu != "igpu" or gaming_line) else "ostatni"
    key = "laptop " + " ".join(p for p in (cpu, gpu) if p)
    return LaptopResult(key, category, features(title))
