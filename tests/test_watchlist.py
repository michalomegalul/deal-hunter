"""Watchlists (Phase 10 part 1): keyword prefilter, command parsing, and the LLM check -- all
without a database (see test_phase10_db.py for the DB-backed parts: insert/list/stop, scan +
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
        "DDR3 2x16GB 1600MHz",  # wrong generation (also excluded outright)
        "DDR4 2x8GB 3200MHz",  # right generation, wrong capacity
        "RTX 3060 12GB grafická karta",  # unrelated
    ],
)
def test_ddr4_watch_does_not_match(title):
    assert prefilter(DDR4["keywords"], title) is False


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
