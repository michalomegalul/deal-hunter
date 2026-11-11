"""Alert rules without a database: the gate, quiet hours, priorities, message text, votes, reposts."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from deal_hunter import alerts
from deal_hunter.alerts import Rules, gate, in_quiet_hours, parse_vote
from deal_hunter.pricing import FairPrice

RULES = Rules(
    enabled=True,
    min_score=0.12,
    max_per_day=5,
    min_samples=15,
    min_days=7,
    quiet_start=23,
    quiet_end=7,
    quiet_bypass=0.3,
    candidate_discount=0.2,
)
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def fp(n=20, category="gpu"):
    return FairPrice("rtx 3080 10gb", category, n, n, 10000, 9500, 10500, 0.7, "high")


def test_gate_needs_samples_and_history():
    since = {
        "graficka": NOW - timedelta(days=10),
        "pc": NOW - timedelta(days=2),
        "notebook": NOW - timedelta(days=3),
    }
    assert gate(fp(), since, RULES, NOW) == (True, "")
    assert gate(fp(n=8), since, RULES, NOW) == (False, "n=8<15")
    is_open, why = gate(fp(category="office_laptop"), since, RULES, NOW)
    assert not is_open and why == "2.0<7 days of data"  # the newer of pc/notebook counts
    assert gate(None, since, RULES, NOW) == (False, "no fair price")
    assert gate(fp(), {}, RULES, NOW)[0] is False


@pytest.mark.parametrize(
    "utc_hour, quiet",
    [(20.5, False), (21, True), (2, True), (4.9, True), (5, False), (12, False)],  # Prague = UTC+2 in October
)
def test_quiet_hours_in_prague_time(utc_hour, quiet):
    now = datetime(2026, 10, 20, int(utc_hour), int(utc_hour % 1 * 60), tzinfo=UTC)
    assert in_quiet_hours(now, RULES) is quiet


def test_quiet_hours_within_one_day():
    rules = Rules(**{**RULES.__dict__, "quiet_start": 1, "quiet_end": 6})
    assert in_quiet_hours(datetime(2026, 10, 20, 0, tzinfo=UTC), rules)  # 02:00 Prague
    assert not in_quiet_hours(datetime(2026, 10, 20, 10, tzinfo=UTC), rules)


def test_priority_from_discount():
    assert [alerts.priority(d) for d in (0.2, 0.33, 0.45, 0.8)] == [3, 4, 5, 5]


def test_alert_message():
    deal = {
        "title": "Gigabyte RTX 3080 Vision OC",
        "price": 6500,
        "fair": 10000,
        "discount": 0.35,
        "model": "rtx 3080 10gb",
        "category": "gpu",
        "verdict": "Clean card with receipt.",
        "location": "Brno",
        "psc": "602 00",
    }
    title, message, tags = alerts.format_alert(deal, fp())
    assert title == "Gigabyte RTX 3080 Vision OC"
    assert message.splitlines()[0] == "6 500 Kč vs ~10 000 Kč fair (−35%), n=20, high confidence"
    assert "🤖 Clean card with receipt." in message and "📍 Brno · 182 km" in message
    assert tags == ["video_game", "moneybag"]
    assert alerts.format_alert({**deal, "category": "office_laptop", "discount": 0.25}, None)[2] == [
        "computer"
    ]


def test_vote_buttons_post_to_the_public_feedback_topic():
    ntfy = alerts.Ntfy("http://lan", "tk_pub", "deals", "https://ntfy.example/", "deals-feedback", "tk_fb")
    up, down = ntfy.vote_actions(123)
    assert up["action"] == "http" and up["method"] == "POST"
    assert up["url"] == "https://ntfy.example/deals-feedback"
    assert up["headers"] == {"Authorization": "Bearer tk_fb"}
    assert up["body"] == '{"ad": 123, "vote": "up"}' and '"down"' in down["body"]
    assert alerts.Ntfy("http://lan", "t", "deals", "https://x", "fb", "").vote_actions(1) == []


def parse_votes(events):
    return [v for v in map(parse_vote, events) if v is not None]


def test_parse_votes_skips_junk():
    events = [
        {"id": "a", "time": 1791579144, "event": "message", "message": '{"ad": 5, "vote": "up"}'},
        {"id": "b", "time": 1791579145, "event": "message", "message": '{"ad": 5, "vote": "maybe"}'},
        {"id": "c", "time": 1791579146, "event": "message", "message": "hello"},
        {"id": "d", "time": 1791579147, "event": "open"},
        {"id": "e", "time": 1791579148, "event": "message", "message": '{"ad": "6", "vote": "down"}'},
        {"id": "f", "time": 1791579149, "event": "keepalive"},
        {"id": "g", "time": 1791579150, "event": "message", "message": '{"ad": "x", "vote": "up"}'},
        {"id": "h", "time": 1791579151, "event": "message", "message": "[1, 2]"},
    ]
    votes = parse_votes(events)
    assert [(v.kind, v.item, v.vote, v.source_id) for v in votes] == [
        ("listing", "5", "up", "a"),
        ("listing", "6", "down", "e"),
    ]
    assert votes[0].voted_at == datetime.fromtimestamp(1791579144, UTC) and not votes[0].test


def test_parse_votes_lots_bought_and_tests():
    def ev(i, body):
        return {"id": f"m{i}", "time": 1791579144 + i, "event": "message", "message": body}

    votes = parse_votes(
        [
            ev(1, '{"lot": "62388", "vote": "up"}'),
            ev(2, '{"lot": 62388, "vote": "down"}'),
            ev(3, '{"ad": 224876851, "vote": "bought"}'),
            ev(4, '{"lot": "62388", "vote": "bought"}'),  # lots can't be "bought" from a push
            ev(5, '{"lot": "../etc", "vote": "up"}'),
            ev(6, '{"ad": 7, "vote": "up", "test": true}'),
            ev(7, '{"ad": 7, "vote": "up", "test": "yes"}'),  # only a real true counts
        ]
    )
    assert [(v.kind, v.item, v.vote, v.test) for v in votes] == [
        ("lot", "62388", "up", False),
        ("lot", "62388", "down", False),
        ("listing", "224876851", "bought", False),
        ("listing", "7", "up", True),
        ("listing", "7", "up", False),
    ]


def test_lot_votes_and_test_votes_say_so_in_the_payload():
    ntfy = alerts.Ntfy("http://lan", "tk_pub", "deals", "https://ntfy.example/", "deals-feedback", "tk_fb")
    up, down = ntfy.lot_vote_actions("62388")
    assert up["body"] == '{"lot": "62388", "vote": "up"}' and '"down"' in down["body"]
    assert ntfy.vote_actions(5, test=True)[0]["body"] == '{"ad": 5, "vote": "up", "test": true}'
    bought = ntfy.feedback_action("Bought ✅", {"ad": 5, "vote": "bought"})
    assert bought["method"] == "POST" and bought["url"] == "https://ntfy.example/deals-feedback"


def test_alert_shows_the_new_price_and_attaches_the_photo():
    deal = {
        "title": "RTX 3060 12GB",
        "price": 4000,
        "fair": 6000,
        "discount": 1 / 3,
        "model": "rtx 3060 12gb",
        "category": "gpu",
        "images": ["https://www.bazos.cz/img/1/851/224876851.jpg"],
    }
    f = fp()
    f.new_price, f.new_source, f.new_detail = 7490, "zbozi", "3 shops"
    assert "rtx 3060 12gb · new ~7 490 Kč (Zboží, 3 shops)" in alerts.format_alert(deal, f)[1]
    f.new_price, f.new_source, f.new_detail = 9302, "msrp", "$329 in 2021"
    assert "launch $329 (2021) ≈ 9 302 Kč" in alerts.format_alert(deal, f)[1]
    assert alerts.first_image(deal) == "https://www.bazos.cz/img/1/851/224876851.jpg"
    assert alerts.first_image({"images": []}) is None and alerts.first_image({}) is None


def test_gate_counts_the_prior_as_samples():
    since = {"graficka": NOW - timedelta(days=10)}
    f = fp(n=10)
    assert gate(f, since, RULES, NOW) == (False, "n=10<15")
    f.prior_weight = 5.0
    assert gate(f, since, RULES, NOW) == (True, "")
    f.n, f.prior_weight = 8, 5.0
    assert gate(f, since, RULES, NOW) == (False, "n=8+5<15")
    assert gate(f, {"graficka": NOW - timedelta(days=3)}, RULES, NOW)[0] is False  # 7-day rule stays


def test_repost_detection():
    sent = [
        {
            "listing_id": 1,
            "model": "rtx 3080 10gb",
            "price": 7000,
            "title": "RTX 3080 Vision",
            "seller": "Pepa",
        }
    ]
    deal = {
        "listing_id": 2,
        "model": "rtx 3080 10gb",
        "price": 7100,
        "title": "RTX 3080 Vision!!",
        "seller": "Jan",
    }
    assert alerts.is_repost(deal, sent)  # same title, ~same price
    assert alerts.is_repost({**deal, "title": "other", "seller": "pepa"}, sent)  # same seller
    assert not alerts.is_repost({**deal, "price": 6000}, sent)  # a real price drop is news
    assert not alerts.is_repost({**deal, "model": "rtx 3070"}, sent)


def test_quiet_hours_config():
    assert alerts.parse_quiet_hours("23-7") == (23, 7)
    with pytest.raises(SystemExit):
        alerts.parse_quiet_hours("late")


def test_publish_treats_a_2xx_with_an_unparseable_body_as_delivered():
    # ntfy accepted the push (2xx) but the body wasn't valid JSON: that must count as delivered
    # (ntfy_id=None), not as a failure to retry next cycle -- a retry would duplicate the push.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="not json"))
    ntfy = alerts.Ntfy(
        "http://lan",
        "tk_pub",
        "deals",
        "https://ntfy.example/",
        "deals-feedback",
        "tk_fb",
        transport=transport,
    )
    assert ntfy.publish("title", "message") is None


def test_publish_still_raises_on_a_real_http_failure():
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="nope"))
    ntfy = alerts.Ntfy(
        "http://lan",
        "tk_pub",
        "deals",
        "https://ntfy.example/",
        "deals-feedback",
        "tk_fb",
        transport=transport,
    )
    with pytest.raises(httpx.HTTPStatusError):
        ntfy.publish("title", "message")


def test_publish_returns_the_id_on_a_normal_2xx_json_reply():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "abc123"}))
    ntfy = alerts.Ntfy(
        "http://lan",
        "tk_pub",
        "deals",
        "https://ntfy.example/",
        "deals-feedback",
        "tk_fb",
        transport=transport,
    )
    assert ntfy.publish("title", "message") == "abc123"


def test_work_once_continues_past_a_failing_core_step(monkeypatch):
    """distances/pricing/candidates/disks/alerts/digest are now guarded like the auction/new-price/
    watch steps already were: one failing must not stop the rest of the cycle or crash it."""
    from deal_hunter import health
    from deal_hunter.db import repository

    failed_steps = []

    monkeypatch.setattr(alerts.geo, "update_distances", lambda *a, **k: None)
    monkeypatch.setattr(alerts.pricing, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(alerts, "find_candidates", lambda *a, **k: 0)
    monkeypatch.setattr(alerts, "screen_disks", lambda *a, **k: None)
    monkeypatch.setattr(repository, "data_since", lambda conn: {})
    monkeypatch.setattr(
        alerts, "send_alerts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("push boom"))
    )
    monkeypatch.setattr(alerts, "maybe_send_digest", lambda *a, **k: False)
    monkeypatch.setattr(health, "record_heartbeat", lambda conn, now, failed: failed_steps.extend(failed))

    stats = alerts.work_once(object(), RULES, llm=None, cache_dir=None, ntfy=None, triage_limit=5, now=NOW)

    assert stats.models == 0  # pricing failed -> prices defaulted to {}, not an abandoned cycle
    assert "pricing" in failed_steps and "alerts" in failed_steps


# --- carry_over_capped: a daily cap is not the end of the line ---------------------------------------


def test_carry_over_capped_waits_for_the_next_prague_day(monkeypatch):
    from deal_hunter.db import repository

    capped_at = datetime(2026, 10, 20, 23, 50, tzinfo=alerts.PRAGUE)
    deal = {
        "listing_id": 1,
        "gone_at": None,
        "updated_at": capped_at,
        "first_seen": capped_at - timedelta(days=1),
    }
    calls = []
    monkeypatch.setattr(repository, "deals_in_state", lambda conn, *states, **kw: [deal])
    monkeypatch.setattr(repository, "update_deal", lambda conn, lid, **f: calls.append((lid, f)))

    same_day = datetime(2026, 10, 20, 23, 55, tzinfo=alerts.PRAGUE)
    assert alerts.carry_over_capped(object(), same_day) == 0
    assert not calls  # the cap still stands today

    next_day = datetime(2026, 10, 21, 0, 10, tzinfo=alerts.PRAGUE)
    assert alerts.carry_over_capped(object(), next_day) == 1
    lid, fields = calls[0]
    assert lid == 1
    assert fields["state"] == "queued" and fields["first_flagged"] == next_day
    assert fields["reason"] == "carried over from 2026-10-20"


def test_carry_over_capped_skips_gone_and_stale_ads(monkeypatch):
    from deal_hunter.db import repository

    capped_at = datetime(2026, 10, 20, 10, 0, tzinfo=alerts.PRAGUE)
    next_day = datetime(2026, 10, 21, 10, 0, tzinfo=alerts.PRAGUE)
    gone = {"listing_id": 2, "gone_at": next_day, "updated_at": capped_at, "first_seen": capped_at}
    stale = {
        "listing_id": 3,
        "gone_at": None,
        "updated_at": capped_at,
        "first_seen": capped_at - timedelta(days=alerts.FRESH_DAYS + 1),
    }
    calls = []
    monkeypatch.setattr(repository, "deals_in_state", lambda conn, *states, **kw: [gone, stale])
    monkeypatch.setattr(repository, "update_deal", lambda conn, lid, **f: calls.append((lid, f)))

    assert alerts.carry_over_capped(object(), next_day) == 0
    assert not calls


def test_maybe_prune_runs_once_per_prague_day(monkeypatch):
    from deal_hunter.db import repository

    kv: dict[str, str] = {}
    prune_calls = []
    monkeypatch.setattr(repository, "kv_get", lambda conn, key, default=None: kv.get(key, default))
    monkeypatch.setattr(repository, "kv_set", lambda conn, key, value: kv.__setitem__(key, value))
    monkeypatch.setattr(repository, "prune", lambda conn, now: prune_calls.append(now) or {"scrape_runs": 3})
    monkeypatch.setattr("deal_hunter.triage.prune_cache", lambda cache_dir, now: 2)

    first = datetime(2026, 10, 20, 9, 0, tzinfo=alerts.PRAGUE)
    assert alerts.maybe_prune(object(), None, first) == "pruned 5 old row(s)/file(s)"
    assert len(prune_calls) == 1

    later_same_day = datetime(2026, 10, 20, 23, 0, tzinfo=alerts.PRAGUE)
    assert alerts.maybe_prune(object(), None, later_same_day) == ""
    assert len(prune_calls) == 1  # still gated: same Prague day

    next_day = datetime(2026, 10, 21, 0, 5, tzinfo=alerts.PRAGUE)
    assert alerts.maybe_prune(object(), None, next_day) == "pruned 5 old row(s)/file(s)"
    assert len(prune_calls) == 2


# --- explain: a plain-English (well, Czech) trace of the pipeline's verdict --------------------------


def listing_row(**kw):
    base = {
        "model": "rtx 3080 10gb",
        "category": "gpu",
        "features": {},
        "price_czk": 6000,
        "first_seen": NOW - timedelta(days=1),
        "gone_at": None,
    }
    return {**base, **kw}


def test_explain_before_candidacy():
    since = {"graficka": NOW - timedelta(days=10)}

    assert alerts.explain(listing_row(model=None), None, None, fp(), since, RULES, NOW) == [
        "Inzerát ještě nemá přiřazený model (nenormalizováno)."
    ]
    assert alerts.explain(listing_row(category="procesory"), None, None, None, since, RULES, NOW) == [
        "Kategorie procesory se zatím neceňuje (chybí katalog)."
    ]
    assert alerts.explain(listing_row(), None, None, None, since, RULES, NOW) == [
        "Chybí fair price: no fair price."
    ]
    assert alerts.explain(listing_row(price_czk=9000), None, None, fp(), since, RULES, NOW) == [
        "Sleva 10% je pod prahem 20%."
    ]
    assert alerts.explain(listing_row(price_czk=500), None, None, fp(), since, RULES, NOW) == [
        "Sleva 95% vypadá jako chyba v ceně (nad 90%)."
    ]
    old = listing_row(price_czk=6000, first_seen=NOW - timedelta(days=10))
    assert alerts.explain(old, None, None, fp(), since, RULES, NOW) == [
        "Inzerát je starší než 3 dny, kandidátem už nebude."
    ]
    assert alerts.explain(listing_row(price_czk=6000), None, None, fp(), since, RULES, NOW) == [
        "Zatím nebyl zpracován jako kandidát (vyřeší další kolo workeru)."
    ]


def test_explain_after_candidacy():
    since = {"graficka": NOW - timedelta(days=10)}
    row = listing_row()

    assert alerts.explain(row, {"state": "triage"}, None, fp(), since, RULES, NOW) == ["Čeká na LLM triage."]

    dropped = {"state": "dropped", "reason": "scam signals: deposit_or_prepay", "verdict": "Looks risky."}
    assert alerts.explain(row, dropped, None, fp(), since, RULES, NOW) == [
        "LLM inzerát zamítlo: scam signals: deposit_or_prepay.",
        'Verdikt LLM: "Looks risky."',
    ]

    low = {"state": "low_score", "reason": "score 0.050", "verdict": None}
    triage_row = {"result": {"one_line_verdict": "Fine but pricey."}}
    assert alerts.explain(row, low, triage_row, fp(), since, RULES, NOW) == [
        "Skóre bylo příliš nízké (score 0.050).",
        'Verdikt LLM: "Fine but pricey."',
    ]

    would_alert = {"state": "would_alert", "reason": "gated: n=5<15"}
    assert alerts.explain(row, would_alert, None, fp(), since, RULES, NOW) == [
        "Byl by to alert, ale: gated: n=5<15."
    ]

    gone_row = listing_row(gone_at=NOW)
    assert alerts.explain(gone_row, {"state": "queued", "score": 0.5}, None, fp(), since, RULES, NOW) == [
        "Inzerát už zmizel, příští kolo workeru ho uzavře jako expired."
    ]

    quiet_now = datetime(2026, 10, 20, 0, tzinfo=UTC)  # 02:00 Prague, inside 23-7
    assert alerts.explain(row, {"state": "queued", "score": 0.1}, None, fp(), since, RULES, quiet_now) == [
        "Čeká na konec nočního klidu."
    ]
    assert alerts.explain(row, {"state": "queued", "score": 0.9}, None, fp(), since, RULES, NOW) == [
        "Čeká ve frontě na odeslání."
    ]

    capped = {"state": "capped", "reason": "5 pushes today already"}
    assert alerts.explain(row, capped, None, fp(), since, RULES, NOW) == [
        "Denní limit upozornění byl vyčerpán (5 pushes today already)."
    ]

    duplicate = {"state": "duplicate", "reason": "repost of a recent push"}
    assert alerts.explain(row, duplicate, None, fp(), since, RULES, NOW) == [
        "Repost už odeslaného inzerátu (repost of a recent push)."
    ]

    expired = {"state": "expired", "reason": "ad gone"}
    assert alerts.explain(row, expired, None, fp(), since, RULES, NOW) == ["Vypršelo: ad gone."]

    assert alerts.explain(row, {"state": "ready"}, None, fp(), since, RULES, NOW) == [
        "Čeká na vyhodnocení (gate, dosah) v dalším kole."
    ]

    alerted = {"state": "alerted", "alerted_at": datetime(2026, 10, 20, 14, 5, tzinfo=UTC)}
    assert alerts.explain(row, alerted, None, fp(), since, RULES, NOW) == ["Odesláno 2026-10-20 16:05."]


# --- price_drop_step: ads no longer fresh, pushed only if they dropped further -----------------------


class FakeNtfy:
    def __init__(self, fail=False):
        self.posts = []
        self.fail = fail

    def vote_actions(self, listing_id, test=False):
        return []

    def publish(self, title, message, priority=3, tags=(), click=None, actions=(), attach=None):
        if self.fail:
            raise httpx.HTTPError("boom")
        self.posts.append((title, message, priority, tags, click))
        return "msg1"


def drop_row(**kw):
    row = {
        "listing_id": 42,
        "title": "Gigabyte RTX 3060 Gaming OC 12GB",
        "url": "https://bazos.cz/inzerat/42/",
        "source": "bazos",
        "images": [],
        "model": "rtx 3060 12gb",
        "category": "gpu",
        "features": {},
        "old_price": 10000,
        "new_price": 8000,  # 20 % below old_price
    }
    row.update(kw)
    return row


def patch_price_drop(monkeypatch, candidates, fair=None, sent_today=0, record=None):
    from deal_hunter.db import repository

    fair = {"rtx 3060 12gb": (10000, "high")} if fair is None else fair
    monkeypatch.setattr(repository, "price_drop_candidates", lambda *a, **k: candidates)
    monkeypatch.setattr(repository, "fair_price_on", lambda *a, **k: fair)
    monkeypatch.setattr(repository, "alerts_since", lambda *a, **k: 0)
    monkeypatch.setattr(repository, "price_drop_alerts_since", lambda *a, **k: sent_today)
    monkeypatch.setattr(
        repository,
        "record_price_drop_alert",
        lambda conn, lid, old, new, now: record.append((lid, old, new)) if record is not None else None,
    )


def test_price_drop_pushes_once_above_threshold(monkeypatch):
    recorded = []
    patch_price_drop(monkeypatch, [drop_row()], record=recorded)
    ntfy = FakeNtfy()
    stats = alerts.CycleStats()

    alerts.price_drop_step(object(), ntfy, RULES, NOW, stats)

    assert len(ntfy.posts) == 1
    title, message, *_ = ntfy.posts[0]
    assert title == "Gigabyte RTX 3060 Gaming OC 12GB"
    assert message == "Zlevněno: 10 000 Kč -> 8 000 Kč (20%)"
    assert stats.price_drops == 1
    assert recorded == [(42, 10000, 8000)]


def test_price_drop_second_cycle_does_not_repeat(monkeypatch):
    """The SQL query (`price_drop_candidates`) stops returning a listing once it's recorded; two
    cycles with that behaviour push at most once in total."""
    from deal_hunter.db import repository

    patch_price_drop(monkeypatch, [drop_row()])
    monkeypatch.setattr(
        repository, "price_drop_candidates", lambda *a, **k: []
    )  # placeholder, overwritten below
    calls = iter([[drop_row()], []])
    monkeypatch.setattr(repository, "price_drop_candidates", lambda *a, **k: next(calls))
    ntfy = FakeNtfy()

    alerts.price_drop_step(object(), ntfy, RULES, NOW, alerts.CycleStats())
    alerts.price_drop_step(object(), ntfy, RULES, NOW, alerts.CycleStats())

    assert len(ntfy.posts) == 1


def test_price_drop_small_drop_ignored(monkeypatch):
    """Defence in depth: even if the query ever returned a listing below the percent threshold,
    the step itself must not push it."""
    row = drop_row(new_price=9200)  # only 8 % below old_price, under RULES.price_drop_pct (0.15)
    patch_price_drop(monkeypatch, [row])
    ntfy = FakeNtfy()

    alerts.price_drop_step(object(), ntfy, RULES, NOW, alerts.CycleStats())

    assert ntfy.posts == []


def test_price_drop_quiet_hours_hold(monkeypatch):
    from deal_hunter.db import repository

    called = []
    monkeypatch.setattr(repository, "price_drop_candidates", lambda *a, **k: called.append(1) or [])
    quiet_now = datetime(2026, 10, 20, 0, tzinfo=UTC)  # 02:00 Prague, inside RULES' 23-7 quiet hours
    ntfy = FakeNtfy()

    alerts.price_drop_step(object(), ntfy, RULES, quiet_now, alerts.CycleStats())

    assert ntfy.posts == []
    assert not called  # it returns before even querying


def test_price_drop_gone_listing_skipped(monkeypatch):
    row = drop_row(gone_at=NOW)
    patch_price_drop(monkeypatch, [row])
    ntfy = FakeNtfy()

    alerts.price_drop_step(object(), ntfy, RULES, NOW, alerts.CycleStats())

    assert ntfy.posts == []


def test_price_drop_respects_daily_cap(monkeypatch):
    patch_price_drop(monkeypatch, [drop_row()], sent_today=RULES.max_per_day)
    ntfy = FakeNtfy()

    alerts.price_drop_step(object(), ntfy, RULES, NOW, alerts.CycleStats())

    assert ntfy.posts == []


def test_price_drop_disabled_or_no_ntfy_is_a_noop(monkeypatch):
    from deal_hunter.db import repository

    called = []
    monkeypatch.setattr(repository, "price_drop_candidates", lambda *a, **k: called.append(1) or [])

    alerts.price_drop_step(object(), None, RULES, NOW, alerts.CycleStats())
    alerts.price_drop_step(
        object(), FakeNtfy(), Rules(**{**RULES.__dict__, "enabled": False}), NOW, alerts.CycleStats()
    )

    assert not called


def test_format_price_drop_message():
    title, message, tags = alerts.format_price_drop(drop_row())
    assert title == "Gigabyte RTX 3060 Gaming OC 12GB"
    assert message == "Zlevněno: 10 000 Kč -> 8 000 Kč (20%)"
    assert "chart_with_downwards_trend" in tags
