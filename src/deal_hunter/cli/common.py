"""Helpers the command modules share: settings to clients, rules and connections."""

from deal_hunter import scraper
from deal_hunter.config import load_settings


def num(n) -> str:
    return f"{n:,}".replace(",", " ") if n is not None else "-"


def make_llm(settings):
    from deal_hunter.triage import OllamaClient

    return OllamaClient(settings.llm_model, settings.ollama_url) if settings.llm_model else None


def ntfy_client(settings, required: bool = False):
    from deal_hunter.alerts import Ntfy

    if not settings.ntfy_token:
        if required:
            raise SystemExit("Missing NTFY_TOKEN (the deal-hunter publisher token) in .env")
        return None
    return Ntfy(
        settings.ntfy_url,
        settings.ntfy_token,
        settings.ntfy_topic,
        settings.ntfy_public_url,
        settings.ntfy_feedback_topic,
        settings.ntfy_feedback_token,
        watch_topic=settings.ntfy_watch_topic,
        watch_token=settings.ntfy_watch_token,
    )


def load_estimates(conn, priors: bool = True):
    """Fair prices as the worker sees them (with new-price/auction priors), or the raw medians."""
    from deal_hunter import pricing
    from deal_hunter.db import repository as repo

    if not priors:
        return pricing.estimate_all(repo.price_samples(conn, pricing.WINDOW_DAYS))
    settings = load_settings()
    return pricing.run(conn, settings.alert_min_samples, settings.usd_czk, save=False)


def make_new_price_http(settings, throttle_conn=None):
    """The polite client for Zboží.cz and Datart.cz: >= 6 s apart, shared across processes via
    http_throttle row 3."""
    from deal_hunter.config import NEW_PRICE_USER_AGENT, USER_AGENT
    from deal_hunter.http import PoliteClient

    throttle = (
        scraper.DbThrottle(throttle_conn, settings.new_price_min_delay, row=3)
        if throttle_conn is not None
        else None
    )
    agent = settings.user_agent if settings.user_agent != USER_AGENT else NEW_PRICE_USER_AGENT
    return PoliteClient(agent, min_delay=settings.new_price_min_delay, throttle=throttle, timeout=30)


def make_auction_rules(settings):
    from deal_hunter.auction_cycle import AuctionRules
    from deal_hunter.auctions import Costs

    return AuctionRules(
        enabled=settings.auctions_enabled,
        alerts=settings.alerts_enabled,
        list_hours=settings.auction_list_hours,
        details_per_cycle=settings.auction_details_per_cycle,
        extract_per_cycle=settings.auction_extract_per_cycle,
        max_per_day=settings.auction_max_per_day,
        costs=Costs(
            hourly_rate=settings.auction_hourly_rate,
            minutes_per_unit=settings.auction_minutes_per_unit,
            resale_factor=settings.auction_resale_factor,
            sell_per_week=settings.auction_sell_per_week,
            lot_cost=settings.auction_lot_cost,
            lot_hours=settings.auction_lot_hours,
            target_margin=settings.auction_target_margin,
            min_profit=settings.auction_min_profit,
        ),
    )


def make_auction_http(settings, throttle_conn=None):
    """The polite client for nms.gov.cz: >= 5 s apart, shared across processes via http_throttle row 2."""
    from deal_hunter.config import AUCTION_USER_AGENT, USER_AGENT
    from deal_hunter.http import PoliteClient

    throttle = (
        scraper.DbThrottle(throttle_conn, settings.auction_min_delay, row=2)
        if throttle_conn is not None
        else None
    )
    agent = settings.user_agent if settings.user_agent != USER_AGENT else AUCTION_USER_AGENT
    return PoliteClient(agent, min_delay=settings.auction_min_delay, throttle=throttle, timeout=40)
