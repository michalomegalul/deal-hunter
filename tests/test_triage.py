"""LLM triage without an LLM: prompt, tolerant parsing, cache, fallback and the deal score."""

import json

import httpx
import pytest

from deal_hunter import triage
from deal_hunter.triage import Triage, clean, parse_answer, penalty, score

DEAL = {
    "listing_id": 7,
    "title": "RTX 3080 Gigabyte Vision",
    "price": 7500,
    "fair": 11000,
    "location": "Praha 4",
    "psc": "140 00",
    "description": "Prodám kartu, nikdy netěžila, účtenka.",
    "model": "rtx 3080 10gb",
    "category": "gpu",
}
GOOD = {
    "condition": "used",
    "for_parts": False,
    "defects": [],
    "mining_suspected": False,
    "warranty_or_receipt": True,
    "urgent_sale": False,
    "scam_signals": [],
    "is_bundle": False,
    "is_wanted_ad": False,
    "specs": {"ram_gb": None, "ssd_gb": None, "cpu": None},
    "one_line_verdict": "Clean card with a receipt, worth a look.",
}


class FakeLLM:
    model = "fake:1b"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def chat(self, system, user, schema):
        self.calls.append((system, user))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, {"prompt_tokens": 10, "output_tokens": 5, "seconds": 0.1}


def test_prompt_has_the_facts():
    text = triage.build_prompt(DEAL)
    assert "rtx 3080 10gb (graphics card)" in text
    assert "7 500 Kč" in text and "11 000 Kč" in text
    assert "Praha 4 140 00" in text and "účtenka" in text
    long = triage.build_prompt({**DEAL, "description": "x" * 10_000})
    assert len(long) < 3500 and long.endswith("[...]")


@pytest.mark.parametrize(
    "raw, notes",
    [
        (json.dumps(GOOD), 0),
        ("```json\n" + json.dumps(GOOD) + "\n```", 1),
        ("Here you go: " + json.dumps(GOOD) + " hope it helps", 1),
    ],
)
def test_parse_answer_is_tolerant(raw, notes):
    data, fixes = parse_answer(raw)
    assert data["condition"] == "used" and len(fixes) == notes


def test_parse_answer_rejects_garbage():
    with pytest.raises(json.JSONDecodeError):
        parse_answer("no json here")
    with pytest.raises(TypeError):
        parse_answer("[1, 2]")


def test_clean_coerces_sloppy_answers():
    t, notes = clean(
        {
            "condition": "Like New",
            "for_parts": "false",
            "defects": "noisy fan",
            "mining_suspected": "yes",
            "scam_signals": ["Shipping only", "weird"],
            "specs": {"ram_gb": "16 GB", "ssd_gb": 0, "cpu": "i5-8350U"},
            "one_line_verdict": "  Fine   deal. ",
        }
    )
    assert t.condition == "like_new" and t.for_parts is False and t.mining_suspected is True
    assert t.defects == ["noisy fan"]
    assert t.scam_signals == ["other", "shipping_only"]
    assert t.specs == {"ram_gb": 16, "ssd_gb": None, "cpu": "i5-8350U"}
    assert t.one_line_verdict == "Fine deal."
    assert any("weird" in n for n in notes)
    t, notes = clean({"condition": "???"})
    assert t.condition == "used" and notes


SOURCE = "RTX 3080. Pouze osobní odběr v Praze. Pošlete zálohu 500 Kč na účet. Pište na WhatsApp +380 123."


@pytest.mark.parametrize(
    "signal, quote, kept",
    [
        ("deposit_or_prepay", "Pošlete zálohu 500 Kč na účet", ["deposit_or_prepay"]),
        ("deposit_or_prepay", "made up words", []),  # not in the ad
        ("deposit_or_prepay", "Pište na WhatsApp +380 123", ["other"]),  # says nothing about paying
        ("shipping_only", "Pouze osobní odběr v Praze", []),  # pickup only is the opposite
        ("shipping_only", "Pište na WhatsApp +380 123", ["other"]),
        ("other", "Pište na WhatsApp +380 123", ["other"]),
        ("too_good", "", []),  # no quote, no signal
    ],
)
def test_scam_signals_need_evidence(signal, quote, kept):
    t, notes = clean({"condition": "used", "scam_signals": [{"signal": signal, "quote": quote}]}, SOURCE)
    assert t.scam_signals == kept
    assert bool(notes) == (kept != [signal])


def test_shipping_only_with_real_evidence():
    t, _ = clean(
        {"scam_signals": [{"signal": "shipping_only", "quote": "pouze zaslání"}]}, "Karta, pouze zaslání."
    )
    assert t.scam_signals == ["shipping_only"] and t.scam_quotes == {"shipping_only": "pouze zaslání"}
    t, _ = clean(
        {"scam_signals": [{"signal": "shipping_only", "quote": "osobní předání není možné"}]},
        "Osobní předání není možné, jen poštou.",
    )
    assert t.scam_signals == ["shipping_only"]


def test_placeholder_defects_are_dropped():
    assert clean({"defects": ["none"]})[0].defects == []
    assert clean({"defects": ["null", "No defects."]})[0].defects == []


def test_triage_caches_answers(tmp_path):
    llm = FakeLLM(json.dumps(GOOD))
    first = triage.triage(llm, DEAL, tmp_path)
    second = triage.triage(llm, DEAL, tmp_path)
    assert first.status == second.status == "done"
    assert first.result == second.result and first.result.warranty_or_receipt
    assert len(llm.calls) == 1 and second.stats["cached"] and not first.stats["cached"]
    assert "Bazos.cz" in llm.calls[0][0] and "RTX 3080 Gigabyte Vision" in llm.calls[0][1]


def test_triage_retries_then_falls_back():
    llm = FakeLLM("not json", json.dumps(GOOD))
    assert triage.triage(llm, DEAL, None).status == "done" and len(llm.calls) == 2
    llm = FakeLLM("not json", "still not json")
    out = triage.triage(llm, DEAL, None)
    assert out.status == "failed" and out.result == triage.FALLBACK and out.error


def test_unreachable_ollama_raises_for_the_queue():
    llm = FakeLLM(triage.LLMUnavailable("connection refused"))
    with pytest.raises(triage.LLMUnavailable):
        triage.triage(llm, DEAL, None)


def test_ollama_client_payload(monkeypatch):
    seen = {}

    def fake_post(url, json, timeout):
        seen.update(url=url, body=json)
        return httpx.Response(
            200,
            json={"message": {"content": "{}"}, "prompt_eval_count": 3, "eval_count": 2},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(triage.httpx, "post", fake_post)
    raw, stats = triage.OllamaClient("gemma3:12b", "http://ollama:11434/").chat(
        "sys", "user", {"type": "object"}
    )
    assert seen["url"] == "http://ollama:11434/api/chat"
    body = seen["body"]
    assert body["format"] == {"type": "object"} and body["stream"] is False and body["think"] is False
    assert body["options"]["temperature"] == 0
    assert raw == "{}" and stats["prompt_tokens"] == 3

    def down(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(triage.httpx, "post", down)
    with pytest.raises(triage.LLMUnavailable):
        triage.OllamaClient("gemma3:12b", "http://ollama:11434").chat("s", "u", {})


@pytest.mark.parametrize(
    "fields, factor, dropped",
    [
        ({}, 1.0, False),
        ({"warranty_or_receipt": True}, 1.1, False),
        ({"is_wanted_ad": True}, 0.0, True),
        ({"for_parts": True}, 0.0, True),
        ({"condition": "broken"}, 0.0, True),
        ({"scam_signals": ["deposit_or_prepay"]}, 0.0, True),
        ({"scam_signals": ["shipping_only", "too_good"]}, 0.0, True),
        ({"scam_signals": ["too_good"]}, 0.6, False),
        ({"scam_signals": ["copied_text"]}, 0.6, False),  # resellers copy shop text all the time
        ({"is_bundle": True}, 0.5, False),
        ({"condition": "worn", "defects": ["scratches"]}, 0.56, False),
        ({"mining_suspected": True}, 0.8, False),
    ],
)
def test_penalties(fields, factor, dropped):
    f, reason = penalty(Triage(**fields))
    assert f == pytest.approx(factor) and (reason is not None) == dropped


def test_score_is_discount_times_confidence_times_penalty():
    assert score(0.3, 0.6, Triage())[0] == pytest.approx(0.18)
    assert score(0.3, 0.6, Triage(mining_suspected=True))[0] == pytest.approx(0.144)
    assert score(0.5, 0.9, Triage(is_wanted_ad=True)) == (0.0, 0.0, "wanted ad")


def test_ships_needs_a_word_about_sending_in_the_ad():
    t, notes = clean({**GOOD, "ships": True}, "Prodám kartu, osobní odběr nebo pošlu Zásilkovnou.")
    assert t.ships is True and not notes
    t, notes = clean({**GOOD, "ships": True}, "Prodám kartu, funkční.")
    assert t.ships is False and "ignored 'ships'" in notes[0]
    assert clean(GOOD, "Pošlu PPL.")[0].ships is False  # the field defaults to false
    assert "ships" in triage.RESPONSE_SCHEMA["required"] and "- ships:" in triage.SYSTEM_PROMPT


# --- hard disks -------------------------------------------------------------------------------------

DISK_AD = (
    "3× Toshiba MG09 18TB enterprise HDD\n"
    "Podle SMART jsou všechny tři v pořádku: 0 vadných sektorů, naběháno 12 400 hodin. Fotky disků i výpis "
    "SMART jsou v inzerátu. Záruka výrobce do 4. 2. 2027. Běžely v NAS."
)
DISK = {
    "power_on_hours": 12400,
    "reallocated_or_pending": 0,
    "health_pct": None,
    "smart_screenshot_mentioned": True,
    "tested": False,
    "warranty_until": "4. 2. 2027",
    "from_nas_or_server": True,
}
SATA_CMR = {"kind": "hdd_35", "capacity_tb": 18.0, "interface": "sata", "smr": False}


def test_disk_questions_only_for_disks():
    assert "disk" in triage.schema_for("hdd")["properties"] and "disk" in triage.schema_for("hdd")["required"]
    assert "disk" not in triage.schema_for("gpu")["properties"]
    assert "power_on_hours" in triage.system_prompt("hdd") and "power_on_hours" not in triage.system_prompt(
        "gpu"
    )
    llm = FakeLLM(json.dumps({**GOOD, "ships": False, "disk": DISK}))
    outcome = triage.triage(
        llm, {**DEAL, "category": "hdd", "title": "3× Toshiba MG09 18TB", "description": DISK_AD}, None
    )
    assert outcome.status == "done" and outcome.result.disk == DISK
    assert "hard disk" in llm.calls[0][1] and "power_on_hours" in llm.calls[0][0]


def test_disk_claims_need_evidence():
    made_up = {
        "power_on_hours": 31337,
        "reallocated_or_pending": 5,
        "health_pct": 97,
        "smart_screenshot_mentioned": True,
        "tested": True,
        "warranty_until": "2028",
        "from_nas_or_server": True,
    }
    t, notes = clean({**GOOD, "disk": made_up}, "Prodám disk 8TB, funkční.")
    assert t.disk == {
        "power_on_hours": None,
        "reallocated_or_pending": None,
        "health_pct": None,
        "smart_screenshot_mentioned": False,
        "tested": False,
        "warranty_until": None,
        "from_nas_or_server": False,
    }
    assert len(notes) == 7
    # sectors are talked about, but the count is not in the ad: 'some'
    t, _ = clean(
        {**GOOD, "disk": {**DISK, "reallocated_or_pending": 7}}, "Disk má přemapované sektory, viz SMART."
    )
    assert t.disk["reallocated_or_pending"] is True
    # Czech thousands separators
    assert clean({**GOOD, "disk": DISK}, DISK_AD)[0].disk["power_on_hours"] == 12400


@pytest.mark.parametrize(
    "disk, features, factor, dropped",
    [
        (DISK, SATA_CMR, 1.0, None),
        ({**DISK, "reallocated_or_pending": 2}, SATA_CMR, 0.0, "reallocated/pending sectors"),
        ({**DISK, "reallocated_or_pending": True}, SATA_CMR, 0.0, "reallocated/pending sectors"),
        ({**DISK, "power_on_hours": 61000}, SATA_CMR, 0.6, None),
        ({}, SATA_CMR, 0.85, None),  # SMART unknown
        (
            {**DISK, "power_on_hours": None, "smart_screenshot_mentioned": False},
            SATA_CMR,
            1.0,
            None,
        ),  # 0 sectors said
        (DISK, {**SATA_CMR, "smr": True}, 0.7, None),
        ({}, {**SATA_CMR, "smr": True}, 0.595, None),
        (DISK, {**SATA_CMR, "interface": "sas"}, 0.0, "SAS drive: not usable on this server"),
    ],
)
def test_disk_penalties(disk, features, factor, dropped):
    got, reason = penalty(Triage(disk=disk), features)
    assert got == factor
    assert (reason or "").startswith(dropped or "")
    assert (reason is None) is (dropped is None)


def test_disk_penalties_stack_with_the_common_ones():
    value, factor, reason = score(
        0.4, 0.5, Triage(warranty_or_receipt=True, disk={}), {**SATA_CMR, "smr": True}
    )
    assert reason is None and factor == round(1.1 * 0.85 * 0.7, 3) and value == round(0.4 * 0.5 * factor, 4)
    assert penalty(Triage(disk={}), {"ram_gb": 8})[0] == 1.0  # office features: no disk rules
