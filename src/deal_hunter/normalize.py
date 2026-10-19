"""Rule-based normalizers: ad title -> canonical model, or a reason to exclude the ad.

GPUs (the graficka section) are matched here against gpu_catalog.yaml; ex-office PCs and laptops
(the pc and notebook sections) in office.py; hard disks and SSDs (the hdd section) in storage.py. `classify` picks the right one for a listing's section
and returns the shared shape the rest of the pipeline uses: category, model (the pricing key) and
features.

Deliberately conservative: a wrong model poisons the fair-price estimates, a missing one only
costs a data point. So anything ambiguous comes out unmatched (model None, no exclusion) and is
listed by `hunt review`, where it can be turned into a new catalog rule.
"""

import re
from dataclasses import dataclass, field
from functools import cache
from importlib import resources

import yaml

from deal_hunter import office, storage
from deal_hunter.bazos import CATEGORY, SECTIONS
from deal_hunter.text import fold


@dataclass(frozen=True)
class Result:
    gpu_model: str | None = None
    vram_gb: int | None = None
    excluded_reason: str | None = None

    @property
    def status(self) -> str:
        if self.gpu_model:
            return "matched"
        return "excluded" if self.excluded_reason else "unmatched"


@dataclass(frozen=True)
class Model:
    name: str  # 'rtx 3060 ti'
    brand: str  # nvidia | amd | intel
    vram: tuple[int, ...]
    msrp: tuple[tuple[int | None, int], ...] = ()  # ((VRAM GB or None for all, launch MSRP in USD), ...)
    launch: int | None = None  # launch year


@dataclass(frozen=True)
class Catalog:
    version: int
    models: dict[str, Model]  # by key: '3060 ti', '6700 xt', 'a770'
    aliases: tuple[tuple[re.Pattern, str], ...]
    out_of_scope: tuple[re.Pattern, ...]


@cache
def load_catalog() -> Catalog:
    raw = yaml.safe_load(resources.files("deal_hunter").joinpath("gpu_catalog.yaml").read_text())
    models = {}
    for brand in ("nvidia", "amd", "intel"):
        for entry in raw[brand]:
            name = entry["model"]
            key = name.split(" ", 1)[1]  # drop the gtx/rtx/rx/arc/gt prefix
            raw_msrp = entry.get("msrp_usd")
            if isinstance(raw_msrp, dict):
                msrp = tuple((int(v), int(usd)) for v, usd in raw_msrp.items())
            else:
                msrp = ((None, int(raw_msrp)),) if raw_msrp else ()
            models[key] = Model(name, brand, tuple(entry["vram"]), msrp, entry.get("launch"))
    return Catalog(
        version=raw["version"],
        models=models,
        aliases=tuple((re.compile(k), v) for k, v in raw["aliases"].items()),
        out_of_scope=tuple(re.compile(p) for p in raw["out_of_scope"]),
    )


def launch_msrp(key: str) -> tuple[int, int] | None:
    """(launch MSRP in USD, launch year) for a GPU pricing key, or None if unknown or ambiguous.

    'rtx 3060 12gb' -> (329, 2021); 'rtx 3070' -> (499, 2020); 'rtx 3060' (VRAM not named, and the
    8 GB and 12 GB cards had different prices) -> None.
    """
    m = re.fullmatch(r"(.+?)(?: (\d{1,2})gb)?", key)
    if m is None:
        return None
    name, vram = m.group(1), int(m.group(2)) if m.group(2) else None
    model = next((x for x in load_catalog().models.values() if x.name == name), None)
    if model is None or not model.msrp:
        return None
    if vram is None and len(model.vram) == 1:
        vram = model.vram[0]
    for size, usd in model.msrp:
        if size is None or size == vram:
            return usd, model.launch
    return None


def version() -> int:
    """One number for all rule sets: GPU catalog * 100 000 + office rules * 100 + storage catalog."""
    return load_catalog().version * 100_000 + office.VERSION * 100 + storage.version()


# --- text preparation ---------------------------------------------------------------------------

ARC_NUMBERS = r"(310|380|580|750|770|570)"


def prepare(title: str) -> str:
    """Lower case, no accents, letters and digits split apart, aliases applied.

    'MSI RTX3080Ti 12GB' -> 'msi rtx 3080 ti 12 gb'; 'Sapphire 6700XT' -> 'sapphire 6700 xt'.
    """
    t = fold(title.replace("×", " x "))
    t = re.sub(r"\b(720|1080|1440|2160) ?p\b", " ", t)  # screen resolutions, not GTX 1080s
    t = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", t)
    t = re.sub(r"[^a-z0-9+&]+", " ", t)
    t = re.sub(rf"\b([ab]) {ARC_NUMBERS}\b", r"\1\2", t)  # 'a 770' -> 'a770' (Intel Arc)
    t = " ".join(t.split())
    for pattern, repl in load_catalog().aliases:
        t = pattern.sub(repl, t)
    return t


# --- exclusions -----------------------------------------------------------------------------------

WANTED = re.compile(
    r"\b(koupim|kupim|shanim|hledam|vykup|vykupuji|vykupujeme|vykoupim|poptavam|sezenu|koupime)\b"
)
LAPTOP = re.compile(r"\b(notebook\w*|laptop\w*|ntb|mobile)\b")
CPU = re.compile(
    r"\b(i [3579] \d{3,5}\w*|i [3579]|core i ?[3579]|ryzen( [3579])?( \d{4}\w*)?|xeon|athlon|threadripper"
    r"|procesor\w*|cpu)\b"
)
BUNDLE = re.compile(
    r"\b(sestav\w*|herni pc|gaming pc|pc set|monitor\w*|zakladni desk\w*|motherboard|gaming box|egpu)\b"
)
PC_WORD = re.compile(r"\b(pc|pocitac\w*)\b")
PC_HARMLESS_BEFORE = {"do", "pro", "z", "ze", "na", "v", "ve", "od", "k"}
BOX = re.compile(r"^(prazdna |jen |pouze )?(krabice|box|obal)\b|\b(krabice|box|obal) (od|z|na|pro)\b")
# Parts sold on their own. They only count when they come before any GPU name in the title:
# 'chladic na rtx 3080' is a cooler, 'rtx 3080 + vodni blok' is a card that comes with one.
ACCESSORY = re.compile(
    r"\b(chladic\w*|ventilator\w*|vetrak\w*|fan|fans|backplate|riser\w*|drzak\w*|anti sag|podper\w*"
    r"|kabel\w*|redukce|bracket|waterblock|vodni blok|blok|mount|vodni okruh|cerpadl\w*|radiator\w*)\b"
)
GPU_MENTION = re.compile(
    r"\b(gtx|rtx|gt|rx|radeon|geforce|quadro|arc|nvidia|nvs|graficka|grafika|\d{4}|[ab]\d{3})\b"
)
MINING = re.compile(
    r"^rig\b|\b(mining|tezebni|tezarsk\w*) (rig|stroj|sestav\w*)\b|\brig (s|se|\d)\b|\blot\b"
    r"|\bp 10[246]\b|\bcmp \d{2,3}\b"  # mining-only cards without display outputs (P106-090, CMP 30HX)
)
BRANDS = (
    r"(rtx|gtx|rx|radeon|geforce|nvidia|amd|intel|msi|asus|gigabyte|gainward|palit|zotac|sapphire|xfx|evga"
    r"|inno 3 d|asrock|powercolor|pny|kfa 2|galax|grafi\w*)"
)
# '2x RTX 3070', '3 ks', '2x Gainward GTX 1650'; but not 'VENTUS 2X' / 'MECH 2X' (fan counts)
MULTIPLE = re.compile(rf"\b([2-9]|1\d) ?(ks|kusy|kusu)\b|^[2-9] x\b|\b[2-9] x {BRANDS}\b")
EXCHANGE = re.compile(r"\b(vymen\w*|vymena|swap|trade)\b")
FOR = re.compile(r"\b(za|for)\b")

MODEL_RE = re.compile(
    r"\b(?:(gtx|rtx|gt|rx|arc|radeon|geforce|nvidia|amd|intel) )?"
    r"(\d{4}|[ab]" + ARC_NUMBERS + r")"
    r"(?: (ti super|ti|super|xtx|xt|gre))?\b"
)
VRAM_RE = re.compile(r"\b(\d{1,2}) ?(gb|g|gib|giga)\b")
AMD_HINT = re.compile(r"\b(rx|radeon|amd|sapphire|powercolor|xfx)\b")


def _bundle(t: str) -> bool:
    if BUNDLE.search(t) or CPU.search(t):
        return True
    for m in PC_WORD.finditer(t):
        before = t[: m.start()].split()
        # 'herni pc ...', 'pc i5 ...' -> a whole computer; 'grafika do pc', 'z pc' -> just the card
        if not before or before[-1] not in PC_HARMLESS_BEFORE:
            return True
    return False


def _accessory(t: str) -> bool:
    part = ACCESSORY.search(t)
    if part is None:
        return False
    gpu = GPU_MENTION.search(t)
    return gpu is None or part.start() < gpu.start()


# 'radeon pro w 5700', 'quadro p 2000', 'nvidia t 1000': workstation cards that share a number
# with a gaming card. ('a' is not in the list: in Czech it means "and", as in 'rtx 3070 a 3080'.)
WORKSTATION_LETTERS = {"w", "wx", "p", "t", "k", "m", "mx", "quadro"}


def find_models(t: str) -> list[Model]:
    """Catalog models mentioned in prepared text, in order, without duplicates."""
    catalog = load_catalog()
    found: list[Model] = []
    amd_hint = bool(AMD_HINT.search(t))
    for m in MODEL_RE.finditer(t):
        prefix, number, _, suffix = m.groups()
        key = f"{number} {suffix}" if suffix else number
        model = catalog.models.get(key)
        if model is None and suffix in ("super", "ti", "ti super"):
            continue  # e.g. 'rtx 3050 ti' (a laptop chip): don't silently drop the suffix
        if model is None:
            continue
        before = t[: m.start()].split()
        if prefix is None and before and before[-1] in WORKSTATION_LETTERS:
            continue
        if model.brand == "amd" and not (
            amd_hint or suffix in ("xt", "xtx", "gre") or prefix in ("rx", "radeon")
        ):
            continue  # a bare 5700 / 6600 could be a Ryzen CPU or anything else
        if model.brand == "nvidia" and prefix in ("rx", "radeon", "amd", "arc", "intel"):
            continue
        if model not in found:
            found.append(model)
    return found


def pick_vram(t: str, model: Model) -> int | None:
    if len(model.vram) == 1:
        return model.vram[0]
    sizes = {int(m.group(1)) for m in VRAM_RE.finditer(t)} & set(model.vram)
    return sizes.pop() if len(sizes) == 1 else None


def offered_part(t: str) -> str:
    """The part of a title describing what's for sale, when the seller also wants a swap.

    'rtx 3070 vymenim za rtx 3080' -> 'rtx 3070 '; 'prodam vymenim rog strix rtx 4090' -> 'rog strix rtx 4090'.
    """
    m = EXCHANGE.search(t)
    if m is None:
        return t
    before, after = t[: m.start()], t[m.end() :]
    if find_models(before):
        return before
    return FOR.split(after, maxsplit=1)[0]


def normalize(title: str) -> Result:
    t = prepare(title)
    if WANTED.search(t):
        return Result(excluded_reason="wanted")
    if LAPTOP.search(t):
        return Result(excluded_reason="laptop")
    if BOX.search(t) or _accessory(t):
        return Result(excluded_reason="accessory")
    if MINING.search(t):
        return Result(excluded_reason="mining")
    if _bundle(t):
        return Result(excluded_reason="bundle")
    if MULTIPLE.search(t):
        return Result(excluded_reason="multiple_gpus")

    offered = offered_part(t)
    models = find_models(offered)
    if len(models) > 1:
        return Result(excluded_reason="multiple_gpus")
    if len(models) == 1:
        return Result(models[0].name, pick_vram(offered, models[0]))
    if any(p.search(t) for p in load_catalog().out_of_scope):
        return Result(excluded_reason="out_of_scope")
    return Result()


# --- one interface for every section -------------------------------------------------------------


@dataclass(frozen=True)
class Normalized:
    category: str  # gpu | office (excluded/unmatched office ads) | office_pc | office_laptop | hdd
    model: str | None = None  # the pricing key: 'rtx 3060 12gb', 'rtx 3070', 'thinkpad t480'
    features: dict = field(default_factory=dict, compare=False)
    excluded_reason: str | None = None


def gpu_key(model: Model, vram: int | None) -> str:
    """'rtx 3070' (one VRAM size exists), 'rtx 3060 12gb' / 'rtx 3060' (size known / not given)."""
    if len(model.vram) > 1 and vram is not None:
        return f"{model.name} {vram}gb"
    return model.name


def classify(title: str, section: str = CATEGORY, description: str | None = None) -> Normalized:
    """The pricing key and features of one ad. `description` is only read for disks (storage.py)."""
    kind = SECTIONS.get(section, SECTIONS[CATEGORY]).kind
    if kind == "watch":
        # RAM/network/case/motherboard sections: never priced or alerted by this
        # pipeline, only matched against watches.py. Excluded early so they don't show up as
        # unmatched GPUs in `hunt stats`/`hunt review`.
        return Normalized("watch", excluded_reason="watch_only")
    if kind == "storage":
        r = storage.normalize(title, description)
        return Normalized("hdd", r.model, r.features, r.excluded_reason)
    if kind == "office":
        r = office.normalize(title)
        return Normalized(r.category or "office", r.model, r.features, r.excluded_reason)
    r = normalize(title)
    if r.gpu_model is None:
        return Normalized("gpu", excluded_reason=r.excluded_reason)
    model = load_catalog().models[r.gpu_model.split(" ", 1)[1]]
    return Normalized("gpu", gpu_key(model, r.vram_gb), {"vram_gb": r.vram_gb} if r.vram_gb else {})


def run(conn, everything: bool = False) -> int:
    """Normalize listings that are new or were done by older rules. Returns the count."""
    from deal_hunter.db import repository as repo

    todo = repo.to_normalize(conn, version(), everything)
    rows = []
    for listing_id, title, section, description in todo:
        r = classify(title, section, description)
        rows.append((listing_id, r.category, r.model, r.features, r.excluded_reason))
    repo.save_normalized(conn, rows, version())
    return len(rows)
