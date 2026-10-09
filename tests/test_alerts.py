"""Alert rules without a database: the gate, quiet hours, priorities, message text, votes, reposts."""

from datetime import UTC, datetime, timedelta

import pytest

from deal_hunter import alerts
from deal_hunter.alerts import Rules, gate, in_quiet_hours, parse_votes
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
    assert "🤖 Clean card with receipt." in message and "📍 Brno 602 00" in message
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


def test_parse_votes_skips_junk():
    events = [
        {"id": "a", "time": 1791579144, "event": "message", "message": '{"ad": 5, "vote": "up"}'},
        {"id": "b", "time": 1791579145, "event": "message", "message": '{"ad": 5, "vote": "maybe"}'},
        {"id": "c", "time": 1791579146, "event": "message", "message": "hello"},
        {"id": "d", "time": 1791579147, "event": "open"},
        {"id": "e", "time": 1791579148, "event": "message", "message": '{"ad": "6", "vote": "down"}'},
    ]
    votes = parse_votes(events)
    assert [(v[0], v[1], v[2]) for v in votes] == [(5, "up", "a"), (6, "down", "e")]
    assert votes[0][3] == datetime.fromtimestamp(1791579144, UTC)


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
