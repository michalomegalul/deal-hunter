import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def fixture():
    return fixture_text


# --- database fixtures (used by tests that skip themselves without DATABASE_URL) -----------------
# They run in a separate throwaway database (`<name>_test`), never the one in DATABASE_URL.


@pytest.fixture(scope="module")
def test_url():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    base = conninfo_to_dict(os.environ["DATABASE_URL"])
    name = f"{base.get('dbname', 'hunter')}_test"
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(**{**base, "dbname": name})
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def conn(test_url):
    from deal_hunter.db import repository as repo

    with repo.connect(test_url, autocommit=True) as conn:
        repo.migrate(conn)
        conn.execute(
            "TRUNCATE listings, price_changes, normalized, scrape_runs, fair_prices, feedback, kv, auction_lots, "
            "followups, flips, new_prices, watches, watch_hits RESTART IDENTITY CASCADE"
        )
        yield conn
