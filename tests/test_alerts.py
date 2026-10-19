"""Alert rules without a database: the gate, quiet hours, priorities, message text, votes, reposts."""

from datetime import UTC, datetime, timedelta

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
