"""Watchlists: watch add | list | stop | seed | listen."""

import argparse
import time

from deal_hunter.cli.common import ntfy_client
from deal_hunter.config import load_settings


def cmd_watch_add(args: argparse.Namespace) -> None:
    from deal_hunter import watchlist
    from deal_hunter.db import repository as repo

    command = watchlist.parse_command("watch " + " ".join(args.text))
    if command.kind != "add":
        raise SystemExit(command.error or "could not parse that watch")
    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        print(watchlist.execute(conn, command, created_by="cli"))


def cmd_watch_list(args: argparse.Namespace) -> None:
    from deal_hunter import watchlist
    from deal_hunter.db import repository as repo

    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        print(watchlist.execute(conn, watchlist.Command("list")))


def cmd_watch_stop(args: argparse.Namespace) -> None:
    from deal_hunter import watchlist
    from deal_hunter.db import repository as repo

    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        print(watchlist.execute(conn, watchlist.Command("stop", watch_id=args.id)))


def cmd_watch_seed(args: argparse.Namespace) -> None:
    from deal_hunter import watchlist
    from deal_hunter.db import repository as repo

    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        added = watchlist.seed(conn)
    print(f"Seeded {added} new watch(es); {len(watchlist.SEEDS) - added} already existed.")


def cmd_watch_listen(args: argparse.Namespace) -> None:
    """Commands from the watch ntfy topic: live (--loop, the service) or what is cached now (once)."""
    from deal_hunter import watchlist
    from deal_hunter.db import repository as repo

    settings = load_settings()
    if not settings.ntfy_watch_token:
        if not args.loop:
            raise SystemExit("Missing NTFY_WATCH_TOKEN (read access to the watch topic) in .env")
        # The service (compose) runs by default even before the owner sets this up: idle quietly
        # instead of crash-looping, same spirit as cmd_scrape_fb without FB_COOKIES_FILE.
        print("watch listener disabled: set NTFY_WATCH_TOKEN to enable it (see .env.example)")
        while True:
            time.sleep(3600)
    ntfy = ntfy_client(settings, required=True)
    with repo.connect(autocommit=True) as conn:
        repo.migrate(conn)
        ctx = watchlist.Context(conn, ntfy)
        if args.loop:
            watchlist.forever(ctx)
            return
        since = repo.kv_get(conn, watchlist.LAST_ID_KEY) or "all"
        events = ntfy.poll_watch(since)
        for event in events:
            if event.get("event") != "message":
                continue
            print(f"{event['id']}: {watchlist.handle(ctx, event)}")
            repo.kv_set(conn, watchlist.LAST_ID_KEY, event["id"])
        print(f"{sum(e.get('event') == 'message' for e in events)} messages since {since}")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("watch", help="watchlists: standing searches matched against every new listing")
    wsub = p.add_subparsers(required=True)
    w = wsub.add_parser("add", help="add a watch, e.g. `hunt watch add ddr4 16gb max 1500 km 50 #homelab`")
    w.add_argument("text", nargs="+")
    w.set_defaults(func=cmd_watch_add)
    w = wsub.add_parser("list", help="list every watch and its id")
    w.set_defaults(func=cmd_watch_list)
    w = wsub.add_parser("stop", help="deactivate a watch by id")
    w.add_argument("id", type=int)
    w.set_defaults(func=cmd_watch_stop)
    w = wsub.add_parser("seed", help="add the default homelab watches (idempotent)")
    w.set_defaults(func=cmd_watch_seed)
    w = wsub.add_parser("listen", help="handle add/list/stop/help commands from the watch ntfy topic")
    w.add_argument("--loop", action="store_true", help="service: hold the live stream open")
    w.set_defaults(func=cmd_watch_listen)
