"""Settings validation: HUNTER_SOURCES and HUNTER_HOME_PSC (config.py, geo.py)."""

import logging

import pytest

from deal_hunter import config, geo

ENV_KEYS = ("HUNTER_SOURCES", "HUNTER_HOME_PSC", "HUNTER_HOME_LAT", "HUNTER_HOME_LON")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_unknown_source_raises_at_startup(monkeypatch):
    monkeypatch.setenv("HUNTER_SOURCES", "bazos,totally-made-up")
    with pytest.raises(SystemExit, match="Unknown HUNTER_SOURCES: totally-made-up"):
        config.load_settings()


def test_known_sources_including_sbazar_do_not_raise(monkeypatch):
    # sbazar has no parser and robots.txt blocks it, but it is still a KNOWN_SOURCES entry (the
    # status page lists it as permanently blocked), so load_settings must accept it.
    monkeypatch.setenv("HUNTER_SOURCES", "bazos,aukro,mironet,fb,sbazar")
    assert config.load_settings().sources == ("bazos", "aukro", "mironet", "fb", "sbazar")


def test_unknown_postcode_falls_back_to_prague_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="deal_hunter.geo"):
        point = geo.home_point("00000", None, None)
    assert point == geo.PRAGUE_CENTRE
    assert any("00000" in r.message and "Prague centre" in r.message for r in caplog.records)


def test_unknown_postcode_with_coordinates_falls_back_to_them_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="deal_hunter.geo"):
        point = geo.home_point("00000", 49.2, 16.6)
    assert point == (49.2, 16.6)
    assert any("00000" in r.message and "HUNTER_HOME_LAT" in r.message for r in caplog.records)


def test_known_postcode_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="deal_hunter.geo"):
        geo.home_point("602 00", None, None)
    assert caplog.records == []


def test_no_postcode_configured_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="deal_hunter.geo"):
        assert geo.home_point(None, None, None) == geo.PRAGUE_CENTRE
    assert caplog.records == []
