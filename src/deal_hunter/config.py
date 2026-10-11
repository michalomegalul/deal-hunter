"""Settings loaded from the environment (and a local .env file)."""

import os
from dataclasses import dataclass
from importlib.metadata import version

from dotenv import load_dotenv

from deal_hunter.geo import home_point

REPO_URL = "https://github.com/michalomegalul/deal-hunter"
VERSION = version("deal-hunter")
USER_AGENT = f"deal-hunter/{VERSION} (+{REPO_URL}; personal price research, ~1 request per 3 s)"
AUCTION_USER_AGENT = f"deal-hunter/{VERSION} (+{REPO_URL}; personal price research, a few requests per hour)"
NEW_PRICE_USER_AGENT = (
    f"deal-hunter/{VERSION} (+{REPO_URL}; personal price research, ~50 product pages a week)"
)

# Hard ceiling for `hunt backfill`, whatever --pages says: we seed history, we don't mirror the site.
MAX_BACKFILL_PAGES = 15


@dataclass(frozen=True)
class Settings:
    user_agent: str
    min_delay: float  # seconds between any two requests to Bazoš (never below 3)
    pages: int  # GPU category pages read by every scrape cycle (other sections: bazos.SECTIONS)
    sections: tuple[str, ...]  # Bazoš sections to scrape: graficka, pc, notebook, hdd
    loop_minutes: float  # scrape cycle interval
    recheck_hours: float  # how often active listings are re-checked
    recheck_limit: int  # max detail pages per recheck run
    # --- more sources, same pipeline (Phase 10) ---
    sources: tuple[str, ...]  # HUNTER_SOURCES=bazos,sbazar,aukro,fb; default keeps today's behaviour
    aukro_pages: int  # category pages read per cycle, per Aukro category (60 items/page)
    fb_cookies_file: str  # Cookie-Editor JSON export path; empty = fb source disabled entirely
    fb_queries: tuple[tuple[str, str], ...]  # [(search text, section), ...] from HUNTER_FB_QUERIES
    fb_city: str
    fb_radius_km: int
    fb_min_gap: float  # seconds between queries within one fb cycle (low and slow)
    fb_max_gap: float
    fb_cycle_min_minutes: float  # the fb cycle itself runs every 20-30 min (jittered), not the 10-min loop
    fb_cycle_max_minutes: float
    # --- worker: pricing, LLM triage, alerts ---
    work_minutes: float  # worker cycle interval
    ollama_url: str
    llm_model: str  # empty = no triage (candidates wait)
    llm_cache: str  # directory for cached LLM answers (in the data volume)
    triage_per_cycle: int
    ntfy_url: str  # where we publish and poll (LAN)
    ntfy_public_url: str  # what the phone's 👍/👎 buttons call (works off the LAN too)
    ntfy_topic: str
    ntfy_token: str
    ntfy_feedback_topic: str
    ntfy_feedback_token: str  # write-only token for the feedback topic, embedded in the buttons
    alerts_enabled: bool
    alert_min_score: float
    alert_max_per_day: int
    alert_min_samples: int
    alert_min_days: float
    quiet_hours: str  # '23-7', Europe/Prague
    quiet_bypass_score: float
    candidate_discount: float
    # --- auctions (nms.gov.cz), run inside the worker ---
    auctions_enabled: bool
    auction_min_delay: float  # seconds between requests to the auction site (never below 5)
    auction_list_hours: float  # how often the lot lists are read
    auction_details_per_cycle: int
    auction_extract_per_cycle: int
    auction_max_per_day: int  # auction pushes per day, on top of HUNTER_ALERT_MAX_PER_DAY
    auction_hourly_rate: float
    auction_minutes_per_unit: float
    auction_resale_factor: float
    auction_sell_per_week: float
    auction_lot_cost: float
    auction_lot_hours: float
    auction_target_margin: float
    auction_min_profit: float
    # --- new prices (Zboží.cz, launch MSRPs) and follow-ups (Phase 8) ---
    usd_czk: float  # launch MSRPs to Kč (display and the sanity check only)
    new_prices_enabled: bool  # weekly Zboží.cz product pages, inside the worker
    new_price_days: float  # how often each product page is read
    new_price_min_delay: float  # seconds between requests to Zboží.cz (never below 6)
    new_price_per_cycle: int
    offer_discount: float  # the follow-up's proposed price: this much below asking (5-10 %)
    # --- distance from home and disks (Phase 9) ---
    home: tuple[float, float]  # HUNTER_HOME_PSC, else HUNTER_HOME_LAT/LON, else Prague centre
    pickup_km: float  # pickup radius; 0 turns the distance rule off
    pickup_km_by_category: tuple[tuple[str, float], ...]  # HUNTER_PICKUP_KM_HDD=300 -> (('hdd', 300.0),)
    shipping_czk: int  # added to the price of an out-of-range ad that ships
    travel_czk_per_km: float  # shown next to the distance (there and back); 0 = not shown
    hdd_min_tb: float  # server fit: smallest disk worth alerting


def _float_or_none(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else None


HDD_PICKUP_KM_DEFAULT = 300.0  # disks ship easily; wider than HUNTER_PICKUP_KM unless overridden


def pickup_by_category(env=None) -> tuple[tuple[str, float], ...]:
    """HUNTER_PICKUP_KM_<CATEGORY>=km for any category (HDD, GPU, OFFICE_PC, OFFICE_LAPTOP; OFFICE = both).

    HDD defaults to HDD_PICKUP_KM_DEFAULT (300 km) even with no env var set; HUNTER_PICKUP_KM_HDD
    (including =0, to fall back to the plain HUNTER_PICKUP_KM) overrides it like any other category.
    """
    env = os.environ if env is None else env
    out: dict[str, float] = {}
    for name, value in sorted(env.items()):
        if not name.startswith("HUNTER_PICKUP_KM_") or not value.strip():
            continue
        category = name.removeprefix("HUNTER_PICKUP_KM_").lower()
        for c in ("office_pc", "office_laptop") if category == "office" else (category,):
            out.setdefault(c, float(value))
    out.setdefault("hdd", HDD_PICKUP_KM_DEFAULT)
    return tuple(sorted(out.items()))


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def fb_queries(env=None) -> tuple[tuple[str, str], ...]:
    """HUNTER_FB_QUERIES='rtx 3060:graficka,thinkpad t480:notebook' -> (('rtx 3060', 'graficka'), ...).

    A query without ':<section>' defaults to 'graficka' (most FB GPU searches are plain model names).
    """
    env = os.environ if env is None else env
    raw = env.get("HUNTER_FB_QUERIES", "").strip()
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        query, _, section = part.partition(":")
        out.append((query.strip(), section.strip() or "graficka"))
    return tuple(out)


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        user_agent=os.getenv("HUNTER_USER_AGENT", USER_AGENT),
        min_delay=max(3.0, float(os.getenv("HUNTER_MIN_DELAY", "3.5"))),
        pages=int(os.getenv("HUNTER_PAGES", "2")),
        sections=tuple(
            s.strip()
            for s in os.getenv("HUNTER_SECTIONS", "graficka,pc,notebook,hdd").split(",")
            if s.strip()
        ),
        loop_minutes=float(os.getenv("HUNTER_LOOP_MINUTES", "10")),
        recheck_hours=float(os.getenv("HUNTER_RECHECK_HOURS", "6")),
        recheck_limit=int(os.getenv("HUNTER_RECHECK_LIMIT", "1500")),
        sources=tuple(s.strip() for s in os.getenv("HUNTER_SOURCES", "bazos").split(",") if s.strip()),
        aukro_pages=int(os.getenv("HUNTER_AUKRO_PAGES", "1")),
        fb_cookies_file=os.getenv("FB_COOKIES_FILE", ""),
        fb_queries=fb_queries(),
        fb_city=os.getenv("HUNTER_FB_CITY", "prague"),
        fb_radius_km=int(os.getenv("HUNTER_FB_RADIUS_KM", "40")),
        fb_min_gap=float(os.getenv("HUNTER_FB_MIN_GAP", "8")),
        fb_max_gap=float(os.getenv("HUNTER_FB_MAX_GAP", "15")),
        fb_cycle_min_minutes=float(os.getenv("HUNTER_FB_CYCLE_MIN_MINUTES", "20")),
        fb_cycle_max_minutes=float(os.getenv("HUNTER_FB_CYCLE_MAX_MINUTES", "30")),
        work_minutes=float(os.getenv("HUNTER_WORK_MINUTES", "5")),
        ollama_url=os.getenv("HUNTER_OLLAMA_URL", "http://192.168.4.19:11434"),
        llm_model=os.getenv("HUNTER_LLM_MODEL", "gemma3:12b"),
        llm_cache=os.getenv("HUNTER_LLM_CACHE", "data/llm_cache"),
        triage_per_cycle=int(os.getenv("HUNTER_TRIAGE_PER_CYCLE", "6")),
        ntfy_url=os.getenv("HUNTER_NTFY_URL", "http://192.168.4.36"),
        ntfy_public_url=os.getenv("HUNTER_NTFY_PUBLIC_URL", "https://ntfy.dobsinsky.dev"),
        ntfy_topic=os.getenv("HUNTER_NTFY_TOPIC", "deals"),
        ntfy_token=os.getenv("NTFY_TOKEN", ""),
        ntfy_feedback_topic=os.getenv("HUNTER_NTFY_FEEDBACK_TOPIC", "deals-feedback"),
        ntfy_feedback_token=os.getenv("NTFY_FEEDBACK_TOKEN", ""),
        alerts_enabled=_flag("HUNTER_ALERTS_ENABLED", "false"),
        alert_min_score=float(os.getenv("HUNTER_ALERT_MIN_SCORE", "0.12")),
        alert_max_per_day=int(os.getenv("HUNTER_ALERT_MAX_PER_DAY", "5")),
        alert_min_samples=int(os.getenv("HUNTER_ALERT_MIN_SAMPLES", "15")),
        alert_min_days=float(os.getenv("HUNTER_ALERT_MIN_DAYS", "7")),
        quiet_hours=os.getenv("HUNTER_QUIET_HOURS", "23-7"),
        quiet_bypass_score=float(os.getenv("HUNTER_QUIET_BYPASS_SCORE", "0.3")),
        candidate_discount=float(os.getenv("HUNTER_CANDIDATE_DISCOUNT", "0.2")),
        auctions_enabled=_flag("HUNTER_AUCTIONS", "true"),
        auction_min_delay=max(5.0, float(os.getenv("HUNTER_AUCTION_MIN_DELAY", "6"))),
        auction_list_hours=float(os.getenv("HUNTER_AUCTION_LIST_HOURS", "2.5")),
        auction_details_per_cycle=int(os.getenv("HUNTER_AUCTION_DETAILS_PER_CYCLE", "12")),
        auction_extract_per_cycle=int(os.getenv("HUNTER_AUCTION_EXTRACT_PER_CYCLE", "3")),
        auction_max_per_day=int(os.getenv("HUNTER_AUCTION_MAX_PER_DAY", "3")),
        auction_hourly_rate=float(os.getenv("HUNTER_AUCTION_HOURLY_RATE", "200")),
        auction_minutes_per_unit=float(os.getenv("HUNTER_AUCTION_MINUTES_PER_UNIT", "40")),
        auction_resale_factor=float(os.getenv("HUNTER_AUCTION_RESALE_FACTOR", "0.9")),
        auction_sell_per_week=float(os.getenv("HUNTER_AUCTION_SELL_PER_WEEK", "5")),
        auction_lot_cost=float(os.getenv("HUNTER_AUCTION_LOT_COST", "500")),
        auction_lot_hours=float(os.getenv("HUNTER_AUCTION_LOT_HOURS", "2")),
        auction_target_margin=float(os.getenv("HUNTER_AUCTION_TARGET_MARGIN", "0.3")),
        auction_min_profit=float(os.getenv("HUNTER_AUCTION_MIN_PROFIT", "1500")),
        usd_czk=float(os.getenv("HUNTER_USD_CZK", "23.5")),
        new_prices_enabled=_flag("HUNTER_NEW_PRICES", "true"),
        new_price_days=float(os.getenv("HUNTER_NEW_PRICE_DAYS", "7")),
        new_price_min_delay=max(6.0, float(os.getenv("HUNTER_NEW_PRICE_MIN_DELAY", "6.5"))),
        new_price_per_cycle=int(os.getenv("HUNTER_NEW_PRICE_PER_CYCLE", "10")),
        offer_discount=min(0.10, max(0.05, float(os.getenv("HUNTER_OFFER_DISCOUNT", "0.07")))),
        home=home_point(
            os.getenv("HUNTER_HOME_PSC"), _float_or_none("HUNTER_HOME_LAT"), _float_or_none("HUNTER_HOME_LON")
        ),
        pickup_km=float(os.getenv("HUNTER_PICKUP_KM", "40")),
        pickup_km_by_category=pickup_by_category(),
        shipping_czk=int(os.getenv("HUNTER_SHIPPING_CZK", "150")),
        travel_czk_per_km=float(os.getenv("HUNTER_TRAVEL_CZK_PER_KM", "0")),
        hdd_min_tb=float(os.getenv("HUNTER_HDD_MIN_TB", "8")),
    )
