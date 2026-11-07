"""The category tree (categories.yaml/categories.py): loading, validation, lookups, and that every
category value the normalizers can emit is something this module recognises."""

import pytest

from deal_hunter import categories as c
from deal_hunter.config import pickup_by_category

# Every `Normalized.category`/`OfficeResult.category` value normalize.py, office.py and storage.py
# can emit (grepped from normalize.py's `Normalized(...)` calls and office.py's `found` tuples):
# gpu, office_pc, office_laptop, hdd (always priced leaves), "office" (an office.py ad recognised as
# a business line but not pinned to a model) and "watch" (a watch-only Bazoš section, matched only by
# watchlist.py). "out_of_scope" is never one of them: normalize.py/office.py/storage.py only ever set
# it as an *excluded_reason* on "gpu"/"office"/"hdd".
EMITTED_CATEGORIES = ("gpu", "office_pc", "office_laptop", "hdd", "office", "watch")


def test_the_tree_loads_and_validates():
    t = c.tree()
    assert t.roots == ("pocitace", "komponenty", "uloziste", "sit")
    assert set(t.nodes) >= {"gpu", "office_pc", "office_laptop", "hdd"}
    # unique ids: loading twice (no cache) must not raise, and a group can't shadow a node id
    assert "office" not in t.nodes
    assert "office" in t.groups


def test_tree_is_cached():
    assert c.tree() is c.tree()


@pytest.mark.parametrize("category", EMITTED_CATEGORIES)
def test_every_category_the_normalizers_can_emit_is_known(category):
    assert c.is_known(category)


def test_out_of_scope_is_not_a_known_category():
    # it is only ever an excluded_reason, never a category value, so it has no node or group
    assert not c.is_known("out_of_scope")


def test_the_priced_leaves_keep_their_existing_ids():
    priced = {n.id for n in c.leaves(priced=True)}
    assert priced == {
        "gpu",
        "office_pc",
        "office_laptop",
        "hdd",
        "apple",
        "apple_desktop",
        "herni",
        "ostatni",
    }


def test_placeholder_leaves_are_not_priced():
    # herni_pc (gaming desktops) and components/network are still Phase-3 placeholders.
    for leaf_id in (
        "herni_pc",
        "procesory",
        "pameti",
        "zakladni_desky",
        "skrine",
        "sit",
    ):
        node = c.node(leaf_id)
        assert node is not None, leaf_id
        assert node.priced is False


def test_no_cycles_detected_in_a_broken_tree():
    text = """
nodes:
  - {id: a, parent: b, name: A}
  - {id: b, parent: a, name: B}
"""
    with pytest.raises(ValueError, match="cycle"):
        c._load(text)


def test_unknown_parent_rejected():
    text = """
nodes:
  - {id: a, parent: ghost, name: A}
"""
    with pytest.raises(ValueError, match="unknown parent"):
        c._load(text)


def test_duplicate_id_rejected():
    text = """
nodes:
  - {id: a, parent: null, name: A}
  - {id: a, parent: null, name: A again}
"""
    with pytest.raises(ValueError, match="duplicate id"):
        c._load(text)


def test_group_colliding_with_a_node_id_rejected():
    text = """
nodes:
  - {id: a, parent: null, name: A}
groups:
  a:
    name: Also A
    members: []
"""
    with pytest.raises(ValueError, match="collides with a node id"):
        c._load(text)


def test_group_with_unknown_member_rejected():
    text = """
nodes:
  - {id: a, parent: null, name: A}
groups:
  g:
    name: G
    members: [ghost]
"""
    with pytest.raises(ValueError, match="unknown member"):
        c._load(text)


def test_node_and_children_and_roots():
    assert c.node("gpu").name == "Grafické karty"
    assert c.node("nonexistent") is None
    assert "office_laptop" in c.children("notebooky")
    assert c.children("gpu") == ()  # a leaf has no children
    assert {n.id for n in c.roots()} == {"pocitace", "komponenty", "uloziste", "sit"}


def test_ancestors_breadcrumb():
    chain = [n.id for n in c.ancestors("office_laptop")]
    assert chain == ["pocitace", "notebooky", "office_laptop"]
    assert [n.id for n in c.ancestors("gpu")] == ["komponenty", "gpu"]
    assert c.ancestors("office") == ()  # a group has no place in the physical tree
    assert c.ancestors("nonexistent") == ()


def test_subtree_of_a_leaf_a_branch_a_group_and_an_unknown_id():
    assert c.subtree("gpu") == ("gpu",)
    assert set(c.subtree("office")) == {"office_pc", "office_laptop"}
    assert set(c.subtree("pocitace")) >= {"office_pc", "office_laptop", "herni", "apple", "ostatni"}
    assert c.subtree("does-not-exist") == ()
    assert c.subtree("OFFICE") == c.subtree("office")  # case insensitive


def test_for_section_default_category():
    assert c.for_section("graficka") == "gpu"
    assert c.for_section("pc") == "office_pc"
    assert c.for_section("notebook") == "office_laptop"
    assert c.for_section("hdd") == "hdd"
    assert c.for_section("wifi") is None  # no priced leaf claims it as a home section


def test_nouns_triage_labels_icons_match_the_old_hard_coded_tables():
    assert c.nouns(priced=True) == {
        "gpu": "grafickou kartu",
        "office_pc": "počítač",
        "office_laptop": "notebook",
        "hdd": "disk",
        "apple": "Apple notebook",
        "apple_desktop": "Apple počítač",
        "herni": "herní notebook",
        "ostatni": "notebook",
    }
    assert c.triage_labels(priced=True) == {
        "gpu": "graphics card",
        "office_pc": "ex-office PC",
        "office_laptop": "business laptop",
        "hdd": "hard disk / SSD",
        "apple": "Apple laptop",
        "apple_desktop": "Apple desktop",
        "herni": "gaming laptop",
        "ostatni": "consumer laptop",
    }
    assert c.icons(priced=True) == {
        "gpu": "video_game",
        "office_pc": "desktop_computer",
        "office_laptop": "computer",
        "hdd": "floppy_disk",
        "apple": "apple",
        "apple_desktop": "apple",
        "herni": "joystick",
        "ostatni": "laptop",
    }


def test_pickup_defaults_only_hdd_today():
    assert c.pickup_defaults() == {"hdd": 300}


# --- backward compatibility with the old HUNTER_PICKUP_KM_* behaviour -------------------------------


def test_pickup_by_category_backward_compatible():
    env = {"HUNTER_PICKUP_KM_HDD": "300", "HUNTER_PICKUP_KM_OFFICE": "60", "HUNTER_PICKUP_KM_GPU": " "}
    assert pickup_by_category(env) == (("hdd", 300.0), ("office_laptop", 60.0), ("office_pc", 60.0))
    assert pickup_by_category({}) == (("hdd", 300.0),)
    assert pickup_by_category({"HUNTER_PICKUP_KM_HDD": "50"}) == (("hdd", 50.0),)


def test_pickup_by_category_more_specific_node_wins():
    env = {"HUNTER_PICKUP_KM_OFFICE": "60", "HUNTER_PICKUP_KM_OFFICE_PC": "30"}
    assert pickup_by_category(env) == (("hdd", 300.0), ("office_laptop", 60.0), ("office_pc", 30.0))


def test_pickup_by_category_unknown_node_id_ignored():
    assert pickup_by_category({"HUNTER_PICKUP_KM_NONSENSE": "10"}) == (("hdd", 300.0),)


def test_pickup_by_category_accepts_any_node_case_insensitively():
    # "KOMPONENTY" covers gpu and every placeholder leaf under it, not just "gpu"
    out = dict(pickup_by_category({"HUNTER_PICKUP_KM_KOMPONENTY": "25"}))
    assert out["gpu"] == 25.0
    assert out["procesory"] == 25.0
