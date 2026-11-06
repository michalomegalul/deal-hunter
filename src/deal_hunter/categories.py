"""The category tree (`categories.yaml`): every category id, Czech name, triage label, follow-up
noun, pickup default, pricing/spec-adjust flag and source section this app knows, read once instead
of being re-typed as separate tuples/dicts in alerts.py, web.py, followup.py, triage.py, config.py
and cli/prices.py.

A shop-style tree, physically modelled on Datart's own menu::

    pocitace > notebooky > {office_laptop, herni, apple, ostatni}
    pocitace > stolni > {office_pc, herni_pc, apple_desktop}
    komponenty > {gpu, procesory, pameti, zakladni_desky, skrine}
    uloziste > hdd
    sit

Only `office_laptop`, `office_pc`, `gpu` and `hdd` are priced today (`Node.priced`) -- the exact ids
`normalize.py`/`office.py`/`storage.py` already emit, unchanged, so no database data moves and no
migration is needed. Every other leaf is a placeholder (`priced=False`) for the catalogs Phase 3
will fill in; they stay out of alerting because `alerts.py` only ever sees a `category` that came
from a priced normalizer result in the first place.

Two category values the normalizers can emit are not part of the physical tree above, because they
never carry a model and are never priced or alerted. They are `groups` instead (name + member leaf
ids, validated against the tree but with no parent/children of their own):

- `"office"`: an ex-office PC/laptop ad `office.py` recognised the business line of but could not
  pin to a model (`hunt review` territory: `Normalized("office", model=None, ...)`). Represented as
  the group `{office_pc, office_laptop}` -- the old `?category=office` web/CLI filter (a
  `row_category.startswith("office")` hack) now resolves through `subtree("office")` instead.
- `"watch"`: an ad from a watch-only Bazoš section (pamet, sit, wifi, case, motherboard) that this
  pipeline never prices or alerts, only matches against `watchlist.py`. Represented as an empty
  group: a recognised sentinel, not a product category.

`"out_of_scope"` is deliberately not here: it is only ever an *excluded_reason* (set on
`category="gpu"`/`"office"`/`"hdd"` when a normalizer recognised the ad but ruled it out), never a
`category` value by itself, so it needs no node or group.
"""

from dataclasses import dataclass
from functools import cache
from importlib import resources

import yaml


@dataclass(frozen=True)
class Node:
    id: str
    parent: str | None
    name: str
    noun: str | None = None
    icon: str | None = None
    triage_label: str | None = None
    default_pickup_km: float | None = None
    priced: bool = False
    spec_adjust: bool = False
    sections: tuple[str, ...] = ()


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tree:
    nodes: dict[str, Node]
    groups: dict[str, Group]
    children: dict[str, tuple[str, ...]]  # physical tree only
    roots: tuple[str, ...]


def _validate(nodes: dict[str, Node], groups: dict[str, Group]) -> None:
    for gid in groups:
        if gid in nodes:
            raise ValueError(f"categories.yaml: group id {gid!r} collides with a node id")
    for node in nodes.values():
        if node.parent is not None and node.parent not in nodes:
            raise ValueError(f"categories.yaml: {node.id!r} has an unknown parent {node.parent!r}")
    for node in nodes.values():  # every node must reach a root without looping
        seen: set[str] = set()
        cur = node.id
        while cur is not None:
            if cur in seen:
                raise ValueError(f"categories.yaml: cycle in the tree at {cur!r}")
            seen.add(cur)
            cur = nodes[cur].parent
    for group in groups.values():
        for member in group.members:
            if member not in nodes:
                raise ValueError(f"categories.yaml: group {group.id!r} has an unknown member {member!r}")


def _load(text: str) -> Tree:
    raw = yaml.safe_load(text)
    nodes: dict[str, Node] = {}
    for entry in raw["nodes"]:
        node = Node(
            id=entry["id"],
            parent=entry.get("parent"),
            name=entry["name"],
            noun=entry.get("noun"),
            icon=entry.get("icon"),
            triage_label=entry.get("triage_label"),
            default_pickup_km=entry.get("default_pickup_km"),
            priced=bool(entry.get("priced", False)),
            spec_adjust=bool(entry.get("spec_adjust", False)),
            sections=tuple(entry.get("sections") or ()),
        )
        if node.id in nodes:
            raise ValueError(f"categories.yaml: duplicate id {node.id!r}")
        nodes[node.id] = node

    groups = {
        gid: Group(id=gid, name=g["name"], members=tuple(g.get("members") or ()))
        for gid, g in (raw.get("groups") or {}).items()
    }
    _validate(nodes, groups)

    children: dict[str, list[str]] = {nid: [] for nid in nodes}
    roots = []
    for node in nodes.values():
        if node.parent is None:
            roots.append(node.id)
        else:
            children[node.parent].append(node.id)
    return Tree(nodes, groups, {k: tuple(v) for k, v in children.items()}, tuple(roots))


@cache
def tree() -> Tree:
    text = resources.files("deal_hunter").joinpath("categories.yaml").read_text(encoding="utf-8")
    return _load(text)


# --- lookups --------------------------------------------------------------------------------------


def node(id: str) -> Node | None:
    return tree().nodes.get(id)


def group(id: str) -> Group | None:
    return tree().groups.get(id)


def is_known(id: str) -> bool:
    """Whether `id` is a category value this module recognises: a node or a group."""
    return id in tree().nodes or id in tree().groups


def children(id: str) -> tuple[str, ...]:
    """Immediate children of a physical tree node (empty for a leaf, a group, or an unknown id)."""
    return tree().children.get(id, ())


@cache
def ancestors(id: str) -> tuple[Node, ...]:
    """Root-first breadcrumb: `ancestors("office_laptop")` -> (pocitace, notebooky, office_laptop).
    Empty for a group or an unknown id (groups have no place in the physical tree)."""
    out = []
    cur = tree().nodes.get(id)
    while cur is not None:
        out.append(cur)
        cur = tree().nodes.get(cur.parent) if cur.parent else None
    return tuple(reversed(out))


def roots() -> tuple[Node, ...]:
    return tuple(tree().nodes[r] for r in tree().roots)


@cache
def subtree(id: str) -> tuple[str, ...]:
    """Leaf ids under `id`: itself if it is already a leaf, a group's members, or every leaf in its
    physical subtree. Unknown ids give an empty tuple, so callers can treat that as "nothing
    matches" instead of raising (config.py's HUNTER_PICKUP_KM_<anything> relies on this)."""
    t = tree()
    id = (id or "").lower()
    if id in t.groups:
        return t.groups[id].members
    if id not in t.nodes:
        return ()
    out: list[str] = []
    stack = [id]
    while stack:
        cur = stack.pop()
        kids = t.children.get(cur, ())
        if kids:
            stack.extend(kids)
        else:
            out.append(cur)
    return tuple(out)


@cache
def leaves(priced: bool | None = None) -> tuple[Node, ...]:
    """Every physical leaf (a node with no children), optionally filtered by `priced`. Groups are
    never included: they are not part of the physical tree."""
    t = tree()
    return tuple(
        n for n in t.nodes.values() if not t.children.get(n.id) and (priced is None or n.priced == priced)
    )


@cache
def for_section(section: str) -> str | None:
    """The priced leaf a Bazoš section normalizes into by default: its "home" section is listed
    first in `sections`, e.g. `for_section("notebook") == "office_laptop"`. None for a section no
    priced leaf claims as home (placeholders and watch-only sections aren't in scope yet -- the same
    "default to the GPU prompt" fallback callers used before this module existed)."""
    for n in leaves(priced=True):
        if n.sections and n.sections[0] == section:
            return n.id
    return None


@cache
def nouns(priced: bool | None = None) -> dict[str, str]:
    """{category id: accusative noun}, for the follow-up message ("zajímám se o {noun} ...")."""
    return {n.id: n.noun for n in leaves(priced) if n.noun}


@cache
def triage_labels(priced: bool | None = None) -> dict[str, str]:
    """{category id: English label}, for the LLM prompt ("Item: ... (ex-office PC)")."""
    return {n.id: n.triage_label for n in leaves(priced) if n.triage_label}


@cache
def icons(priced: bool | None = None) -> dict[str, str]:
    """{category id: icon}. For the four priced leaves these are the exact ntfy tag names the push
    notifications always used (`video_game`, `desktop_computer`, `computer`, `floppy_disk`)."""
    return {n.id: n.icon for n in leaves(priced) if n.icon}


@cache
def pickup_defaults(priced: bool | None = None) -> dict[str, float]:
    """{category id: default pickup radius km}, for categories with one even with no env var set
    (today only `hdd`, 300 km: disks ship easily)."""
    return {n.id: n.default_pickup_km for n in leaves(priced) if n.default_pickup_km is not None}
