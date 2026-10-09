"""Settings loaded from the environment (and a local .env file)."""

import os
from dataclasses import dataclass
from importlib.metadata import version

from dotenv import load_dotenv

REPO_URL = "https://github.com/michalomegalul/deal-hunter"
USER_AGENT = f"deal-hunter/{version('deal-hunter')} (+{REPO_URL}; personal GPU price research, ~1 request per 3 s)"

# Hard ceiling for `hunt backfill`, whatever --pages says: we seed history, we don't mirror the site.
MAX_BACKFILL_PAGES = 15


@dataclass(frozen=True)
class Settings:
    user_agent: str
    min_delay: float  # seconds between any two requests to Bazoš (never below 3)
    pages: int  # category pages read by every scrape cycle
    loop_minutes: float  # scrape cycle interval
    recheck_hours: float  # how often active listings are re-checked
    recheck_limit: int  # max detail pages per recheck run


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        user_agent=os.getenv("HUNTER_USER_AGENT", USER_AGENT),
        min_delay=max(3.0, float(os.getenv("HUNTER_MIN_DELAY", "3.5"))),
        pages=int(os.getenv("HUNTER_PAGES", "2")),
        loop_minutes=float(os.getenv("HUNTER_LOOP_MINUTES", "10")),
        recheck_hours=float(os.getenv("HUNTER_RECHECK_HOURS", "6")),
        recheck_limit=int(os.getenv("HUNTER_RECHECK_LIMIT", "1500")),
    )
