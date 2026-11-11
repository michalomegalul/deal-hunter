"""`hunt db ...` commands."""

import argparse

from deal_hunter.db import repository as repo


def register(subparsers: argparse._SubParsersAction) -> None:
    db = subparsers.add_parser("db", help="database commands").add_subparsers(required=True)

    p = db.add_parser("migrate", help="create or update the database schema")
    p.set_defaults(func=cmd_migrate)

    p = db.add_parser(
        "prune", help="delete/trim old data per the retention policy (also runs daily from the worker)"
    )
    p.set_defaults(func=cmd_prune)


def cmd_migrate(args: argparse.Namespace) -> None:
    with repo.connect() as conn:
        applied = repo.migrate(conn)
    print(f"Applied: {', '.join(applied)}" if applied else "Schema is up to date.")


def cmd_prune(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from deal_hunter import triage
    from deal_hunter.config import load_settings

    now = datetime.now(UTC)
    settings = load_settings()
    with repo.connect() as conn:
        counts = repo.prune(conn, now)
    counts["llm_cache"] = triage.prune_cache(Path(settings.llm_cache), now.timestamp())
    removed = {k: n for k, n in counts.items() if n}
    if not removed:
        print("Nothing to prune.")
        return
    for key, n in removed.items():
        print(f"{key}: {n}")
