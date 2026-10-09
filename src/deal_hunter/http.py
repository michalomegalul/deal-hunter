"""A polite HTTP client: one request at a time, a minimum gap between requests, backoff on errors."""

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class Page:
    url: str
    status: int
    text: str
    location: str | None = None  # redirect target, when status is 3xx
    content: bytes = b""  # the raw body (PDF attachments)
    content_type: str = ""


class PoliteClient:
    """Fetches pages with at least `min_delay` seconds between requests (even across calls).

    Redirects are not followed, so the caller can tell "the ad moved/was removed" from "here it is".
    429 and 5xx responses and network errors are retried with exponential backoff (honouring
    Retry-After); after `retries` attempts the last error is raised.
    """

    def __init__(
        self,
        user_agent: str,
        min_delay: float = 3.0,
        timeout: float = 20.0,
        retries: int = 4,
        backoff: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        throttle=None,
    ):
        self.min_delay = min_delay
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self.throttle = throttle  # optional shared limiter with .wait_turn() (see scraper.DbThrottle)
        self.requests = 0
        self._last = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "cs,en;q=0.5"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def _wait_turn(self) -> None:
        gap = self.min_delay - (time.monotonic() - self._last)
        if gap > 0:
            self.sleep(gap)
        if self.throttle is not None:
            self.throttle.wait_turn()
        self._last = time.monotonic()

    def get(self, url: str) -> Page:
        return self._request("GET", url)

    def post_json(self, url: str, body: dict) -> Page:
        """POST a JSON body (a site's own list API, as its list page calls it)."""
        return self._request("POST", url, json=body)

    def _request(self, method: str, url: str, **kwargs) -> Page:
        delay = self.backoff
        for attempt in range(1, self.retries + 1):
            self._wait_turn()
            self.requests += 1
            try:
                r = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise
                log.warning("%s: %s, retry %d in %.0fs", url, type(exc).__name__, attempt, delay)
                self.sleep(delay)
                delay *= 2
                continue
            if r.status_code in RETRY_STATUS and attempt < self.retries:
                wait = delay
                retry_after = r.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = max(wait, float(retry_after))
                log.warning("%s: HTTP %d, retry %d in %.0fs", url, r.status_code, attempt, wait)
                self.sleep(wait)
                delay *= 2
                continue
            if r.status_code in RETRY_STATUS:
                r.raise_for_status()
            return Page(
                url,
                r.status_code,
                r.text,
                r.headers.get("Location"),
                r.content,
                r.headers.get("Content-Type", ""),
            )
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
