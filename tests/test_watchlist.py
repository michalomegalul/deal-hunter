"""Watchlists: keyword prefilter, command parsing, and the LLM check -- all
without a database (see test_watchlist_db.py for the DB-backed parts: insert/list/stop, scan +
push, scrape cadence)."""

import json

import pytest

from deal_hunter import watchlist
from deal_hunter.watchlist import SEEDS, Command, check, parse_command, prefilter
from tests.test_triage import FakeLLM

DDR4 = next(s for s in SEEDS if "DDR4" in s["query"])
HBA = next(s for s in SEEDS if "LSI" in s["query"])

# --- prefilter --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Kingston DDR4 2x16GB 3200MHz",
        "RAM DDR4 32GB (2x16GB) 3200 CL16",
        "Paměti DDR4 2x32GB 3200MHz Corsair Vengeance",
    ],
)
def test_ddr4_watch_matches_the_right_kits(title):
    assert prefilter(DDR4["keywords"], title) is True


@pytest.mark.parametrize(
    "title",
    [
        "DDR4 ECC 2x16GB registered server RAM",  # excluded: ECC
        "SODIMM DDR4 2x16GB notebook RAM",  # excluded: SODIMM
        "Kingston FURY Impact 32 GB (2× 16 GB) DDR4-3200 SO-DIMM",  # excluded: SO-DIMM, hyphenated
        "Pamět SO DIMM DDR4 2x32GB (64GB) 3200 MHz",  # ... or with a space
        "DDR3 2x16GB 1600MHz",  # wrong generation (also excluded outright)
        "DDR4 2x8GB 3200MHz",  # right generation, wrong capacity
        "RTX 3060 12GB grafická karta",  # unrelated
    ],
)
def test_ddr4_watch_does_not_match(title):
    assert prefilter(DDR4["keywords"], title) is False


def test_ddr4_watch_keeps_a_non_ecc_kit():
    # 'ecc' is excluded, but 'non-ECC' is exactly what the watch wants
    assert prefilter(DDR4["keywords"], "Kingston DDR4 2x16GB 3200MHz non-ECC") is True
    assert prefilter(DDR4["keywords"], "Kingston DDR4 2x16GB 3200MHz, non ECC unbuffered") is True


def test_exclude_terms_match_from_the_start_of_a_word():
    assert prefilter(DDR4["keywords"], "DDR3L 2x16GB DDR4 compatible?") is False  # 'ddr3' catches 'ddr3l'


def test_ddr4_watch_reads_the_description_too():
    assert prefilter(DDR4["keywords"], "Prodám paměti", "DDR4 sada 2x16GB 3200MHz, nepoužité") is True


@pytest.mark.parametrize(
    "title",
    [
        "LSI SAS9207-8i HBA controller, IT mode",
        "Dell PERC H200 rebranded LSI 9211-8i, flashed IT mode",
    ],
)
def test_hba_watch_matches(title):
    assert prefilter(HBA["keywords"], title) is True


def test_hba_watch_excludes_raid_cards():
    assert prefilter(HBA["keywords"], "Dell PERC RAID card with 9207-8i chipset") is False


def test_hba_watch_rejects_unrelated_card():
    assert prefilter(HBA["keywords"], "LSI 9240-8i RAID controller") is False


def test_no_include_groups_matches_anything_not_excluded():
    assert prefilter({"exclude": ["broken"]}, "Anything at all") is True
    assert prefilter({"exclude": ["broken"]}, "Broken anything") is False


def test_normalize_text_collapses_spacing():
    assert watchlist.normalize_text("2 x 16 GB") == watchlist.normalize_text("2x16gb")
    assert watchlist.normalize_text("Paměť") == "pamet"


def test_keywords_from_query_is_an_and_of_every_word():
    kw = watchlist.keywords_from_query("ddr4 16gb")
    assert prefilter(kw, "Super DDR4 16GB kit") is True
    assert prefilter(kw, "Super DDR4 32GB kit") is False  # missing '16gb'


# --- command parsing ---------------------------------------------------------------------------------


def test_parse_watch_with_price_radius_and_tag():
    c = parse_command("watch ddr4 16gb max 1500 km 50 #homelab")
    assert c == Command("add", query="ddr4 16gb", max_price=1500, radius_km=50.0, tag="homelab")


def test_parse_is_case_insensitive_and_tolerant_of_order():
    c = parse_command("WATCH rtx 3060 km 40 max 8000")
    assert c.kind == "add" and c.query == "rtx 3060" and c.max_price == 8000 and c.radius_km == 40.0


def test_parse_do_radius_is_distance_not_price():
    # "do 50 km" is Czech for "within 50 km" -- a radius, even though "do" alone (as in "do 1500")
    # means "up to" for a price. Must not be misread as max_price=50 with "km" left dangling in the
    # query (the old bug).
    c = parse_command("watch ddr4 16gb do 50 km")
    assert c == Command("add", query="ddr4 16gb", max_price=None, radius_km=50.0, tag=None)


def test_parse_max_price_with_kc_strips_the_currency_from_the_query():
    # "max 1500 Kč" must set max_price=1500 *and* not leave a stray "Kč" in the search query (Kč's
    # diacritic wasn't matched by the old currency group, so it leaked into keywords_from_query).
    c = parse_command("watch ddr4 16gb max 1500 Kč")
    assert c == Command("add", query="ddr4 16gb", max_price=1500, radius_km=None, tag=None)


def test_parse_do_radius_and_max_price_together():
    c = parse_command("watch ddr4 16gb do 50 km max 1500 Kč")
    assert c == Command("add", query="ddr4 16gb", max_price=1500, radius_km=50.0, tag=None)


@pytest.mark.parametrize("verb", ["sleduj", "add", "pridej", "WATCH"])
def test_add_synonyms(verb):
    assert parse_command(f"{verb} ddr4 16gb").kind == "add"


@pytest.mark.parametrize("verb", ["list", "seznam", "ls", "LIST"])
def test_list_synonyms(verb):
    assert parse_command(verb).kind == "list"


@pytest.mark.parametrize("verb", ["stop", "zastav", "smazat", "delete"])
def test_stop_synonyms(verb):
    assert parse_command(f"{verb} 3") == Command("stop", watch_id=3)


@pytest.mark.parametrize("verb", ["help", "napoveda", "?"])
def test_help_synonyms(verb):
    assert parse_command(verb).kind == "help"


def test_stop_without_id_is_unknown():
    c = parse_command("stop")
    assert c.kind == "unknown" and "id" in c.error


def test_watch_without_words_is_unknown():
    c = parse_command("watch max 1000")
    assert c.kind == "unknown"


def test_gibberish_is_unknown():
    c = parse_command("blah blah blah")
    assert c.kind == "unknown"


def test_empty_message_is_unknown():
    assert parse_command("").kind == "unknown"
    assert parse_command("   ").kind == "unknown"


def test_czech_query_text_is_preserved():
    c = parse_command("sleduj rozvaděč 12u")
    assert c.kind == "add" and c.query == "rozvaděč 12u"


# --- exclude terms in `add` ---------------------------------------------------------------------------


def test_add_with_exclude_terms():
    c = parse_command("watch rtx 3060 -notebook -vadna")
    assert c.kind == "add" and c.query == "rtx 3060" and c.excludes == ("notebook", "vadna")


def test_exclude_terms_combined_with_price_and_radius():
    c = parse_command("watch ddr4 16gb -ecc do 50 km max 1500 Kč -sodimm")
    assert c.kind == "add"
    assert c.query == "ddr4 16gb"
    assert c.excludes == ("ecc", "sodimm")
    assert c.max_price == 1500 and c.radius_km == 50.0


def test_exclude_term_does_not_eat_a_hyphenated_word():
    # "SO-DIMM" has no space before its "-DIMM", so it is not mistaken for an exclude token.
    c = parse_command("watch Kingston SO-DIMM DDR4")
    assert c.kind == "add" and c.excludes == () and "SO-DIMM" in c.query


def test_resulting_keywords_apply_the_exclude_terms():
    c = parse_command("watch rtx 3060 -notebook")
    kw = watchlist.keywords_from_query(c.query)
    kw["exclude"] = list(kw.get("exclude") or []) + list(c.excludes)
    assert prefilter(kw, "RTX 3060 notebook GPU") is False
    assert prefilter(kw, "RTX 3060 desktop GPU") is True


# --- start / mute / vote ------------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["start", "obnov", "spustit", "START"])
def test_start_synonyms(verb):
    assert parse_command(f"{verb} 3") == Command("start", watch_id=3)


def test_start_without_id_is_unknown():
    c = parse_command("start")
    assert c.kind == "unknown" and "id" in c.error


@pytest.mark.parametrize("verb", ["mute", "ztlum", "ztlumit", "MUTE"])
def test_mute_by_watch_id(verb):
    c = parse_command(f"{verb} 3 7d")
    assert c == Command("mute", watch_id=3, mute_days=7.0)


def test_mute_by_free_text_query():
    c = parse_command("mute ddr4 16gb 2w")
    assert c.kind == "mute" and c.query == "ddr4 16gb" and c.watch_id is None
    assert c.mute_days == 14.0


def test_mute_hours():
    c = parse_command("mute 3 12h")
    assert c.watch_id == 3 and c.mute_days == 0.5


def test_mute_without_duration_is_unknown():
    c = parse_command("mute ddr4 16gb")
    assert c.kind == "unknown" and "duration" in c.error


def test_mute_without_target_is_unknown():
    c = parse_command("mute 7d")
    assert c.kind == "unknown"


def test_vote_parses_hit_id_and_direction():
    assert parse_command("vote 3-501 up") == Command("vote", watch_id=3, listing_id=501, vote="up")
    assert parse_command("vote 3-501 down") == Command("vote", watch_id=3, listing_id=501, vote="down")


def test_vote_malformed_is_unknown():
    assert parse_command("vote banana up").kind == "unknown"
    assert parse_command("vote 3-501 sideways").kind == "unknown"
    assert parse_command("vote 3-501").kind == "unknown"


# --- watch-hit push action buttons --------------------------------------------------------------------


class _FakeNtfyAttrs:
    public_url = "https://ntfy.example"
    watch_topic = "watch"
    watch_write_token = ""


def test_watch_hit_actions_empty_without_a_write_token():
    assert watchlist.watch_hit_actions(_FakeNtfyAttrs(), 3, 501) == []


def test_watch_hit_actions_posts_commands_to_the_watch_topic():
    ntfy = _FakeNtfyAttrs()
    ntfy.watch_write_token = "tk_watch_write"
    actions = watchlist.watch_hit_actions(ntfy, 3, 501)
    labels = {a["label"]: a["body"] for a in actions}
    assert labels["Stop"] == "stop 3"
    assert labels["👍"] == "vote 3-501 up"
    assert labels["👎"] == "vote 3-501 down"
    assert all(a["url"] == "https://ntfy.example/watch" for a in actions)
    assert all(a["headers"]["Authorization"] == "Bearer tk_watch_write" for a in actions)


# --- the LLM check -----------------------------------------------------------------------------------

WATCH = {"query": "DDR4 UDIMM 2x16GB non-ECC 3200"}
LISTING = {
    "id": 1,
    "title": "Kingston DDR4 2x16GB 3200MHz",
    "description": "Prodám paměti, nikdy nepoužité.",
    "price_czk": 1200,
}
GOOD_ANSWER = {
    "matches": True,
    "product_type": "RAM kit",
    "brand": "Kingston",
    "model": "HX432C16FBK2/32",
    "condition": "used",
    "price_czk": 1200,
    "evidence": "Kingston DDR4 2x16GB 3200MHz",
}


def test_check_accepts_a_backed_match():
    llm = FakeLLM(json.dumps(GOOD_ANSWER))
    match = check(llm, WATCH, LISTING, None)
    assert match.matches is True
    assert match.brand == "Kingston" and match.model == "HX432C16FBK2/32"
    assert match.price_czk == 1200
    assert match.evidence == "Kingston DDR4 2x16GB 3200MHz"


def test_check_rejects_a_match_with_a_made_up_quote():
    bad = {**GOOD_ANSWER, "evidence": "something that is not in the ad at all"}
    llm = FakeLLM(json.dumps(bad))
    match = check(llm, WATCH, LISTING, None)
    assert match.matches is False
    assert "not found" in match.evidence


def test_check_rejects_an_accessory_for_the_item():
    answer = {**GOOD_ANSWER, "accessory_only": True}
    match = check(FakeLLM(json.dumps(answer)), WATCH, LISTING, None)
    assert match.matches is False and match.accessory_only is True


def test_check_handles_an_unreadable_answer():
    llm = FakeLLM("not json at all")
    match = check(llm, WATCH, LISTING, None)
    assert match.matches is False


def test_check_propagates_llm_unavailable():
    from deal_hunter.triage import LLMUnavailable

    llm = FakeLLM(LLMUnavailable("connection refused"))
    with pytest.raises(LLMUnavailable):
        check(llm, WATCH, LISTING, None)


def test_check_false_answer_with_no_evidence_is_kept_as_no_match():
    answer = {**GOOD_ANSWER, "matches": False, "evidence": ""}
    llm = FakeLLM(json.dumps(answer))
    match = check(llm, WATCH, LISTING, None)
    assert match.matches is False


# --- the watch's own limits ---------------------------------------------------------------------------

LIMITED = {"max_price": 1600, "radius_km": 80, "categories": []}


def test_within_limits_passes_a_listing_inside_them():
    assert watchlist.within_limits(LIMITED, {"price_czk": 1500, "distance_km": 30}) is None


def test_within_limits_rejects_over_price_and_over_radius():
    assert watchlist.within_limits(LIMITED, {"price_czk": 4500, "distance_km": 30}) == (
        "price 4 500 Kč > max 1 600 Kč"
    )
    reason = watchlist.within_limits(LIMITED, {"price_czk": 1500, "distance_km": 231.4, "location": "Brno"})
    assert reason == "Brno 231 km > 80 km"


def test_within_limits_lets_unknown_price_and_distance_through():
    assert watchlist.within_limits(LIMITED, {"price_czk": None, "distance_km": None}) is None


def test_within_limits_without_limits_passes_anything():
    watch = {"max_price": None, "radius_km": None, "categories": []}
    assert watchlist.within_limits(watch, {"price_czk": 99999, "distance_km": 500}) is None


def test_within_limits_applies_categories_when_set():
    watch = {**LIMITED, "categories": ["pamet"]}
    assert watchlist.within_limits(watch, {"price_czk": 1000, "section": "pamet"}) is None
    assert watchlist.within_limits(watch, {"price_czk": 1000, "section": "notebook"}) == (
        "section notebook not in pamet"
    )


# --- push formatting ---------------------------------------------------------------------------------


def test_format_watch_push_includes_price_and_evidence():
    hit = {
        "query": "DDR4 UDIMM 2x16GB non-ECC 3200",
        "title": "Kingston DDR4 2x16GB 3200MHz",
        "price_czk": 1200,
        "location": "Brno",
        "psc": None,
        "distance_km": 182.4,
        "result": {"evidence": "Kingston DDR4 2x16GB 3200MHz"},
        "tag": "homelab",
        "source": "bazos",
    }
    title, message = watchlist.format_watch_push(hit)
    assert "DDR4 UDIMM" in title
    assert "1 200 Kč" in message and "Brno" in message and "182 km" in message
    assert "#homelab" in message and "Kingston DDR4 2x16GB 3200MHz" in message
