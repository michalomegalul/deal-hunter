import httpx
import pytest

from deal_hunter.http import PoliteClient


def client_for(handler, **kw):
    sleeps = []
    c = PoliteClient(
        "test-agent",
        min_delay=3.0,
        backoff=1.0,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        **kw,
    )
    return c, sleeps


def test_sends_user_agent_and_does_not_follow_redirects():
    seen = []

    def handler(request):
        seen.append(request.headers["User-Agent"])
        return httpx.Response(301, headers={"Location": "https://pc.bazos.cz/"})

    c, _ = client_for(handler)
    page = c.get("https://pc.bazos.cz/inzerat/1/x.php")
    assert (page.status, page.location) == (301, "https://pc.bazos.cz/")
    assert seen == ["test-agent"]


def test_waits_between_requests():
    c, sleeps = client_for(lambda r: httpx.Response(200, text="ok"))
    c.get("https://example.test/a")
    c.get("https://example.test/b")
    assert len(sleeps) == 1 and 2.9 < sleeps[0] <= 3.0


def test_retries_429_with_backoff_and_retry_after():
    calls = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503),
            httpx.Response(200, text="finally"),
        ]
    )
    c, sleeps = client_for(lambda r: next(calls))
    page = c.get("https://example.test/")
    assert page.text == "finally"
    backoffs = [s for s in sleeps if s > 3.0 or s in (1.0, 2.0)]
    assert 7.0 in backoffs and 2.0 in backoffs
    assert c.requests == 3


def test_gives_up_after_retries():
    c, _ = client_for(lambda r: httpx.Response(503), retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        c.get("https://example.test/")


def test_404_is_returned_not_raised():
    c, _ = client_for(lambda r: httpx.Response(410, text="gone"))
    assert c.get("https://example.test/").status == 410


def test_shared_throttle_is_consulted():
    class Throttle:
        calls = 0

        def wait_turn(self):
            Throttle.calls += 1

    c, _ = client_for(lambda r: httpx.Response(200), throttle=Throttle())
    c.get("https://example.test/")
    c.get("https://example.test/")
    assert Throttle.calls == 2
