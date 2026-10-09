"""`hunt db ...` commands."""

import argparse

from deal_hunter.db import repository as repo


def register(subparsers: argparse._SubParsersAction) -> None:
    db = subparsers.add_parser("db", help="database commands").add_subparsers(required=True)

    p = db.add_parser("migrate", help="create or update the database schema")
    p.set_defaults(func=cmd_migrate)


def cmd_migrate(args: argparse.Namespace) -> None:
    with repo.connect() as conn:
        applied = repo.migrate(conn)
    print(f"Applied: {', '.join(applied)}" if applied else "Schema is up to date.")
