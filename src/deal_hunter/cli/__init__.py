"""Command line entry point: `hunt scrape | work | prices | auctions | watch | flip | db | ...`."""

import argparse
import logging

from deal_hunter.cli import auctions, flips, prices, scrape, watch, worker
from deal_hunter.db.cli import register as register_db


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hunt", description="Bazoš deal hunter (GPUs, ex-office PCs/laptops, hard disks)"
    )
    sub = parser.add_subparsers(required=True)
    for module in (scrape, prices, worker, auctions, watch, flips):
        module.register(sub)
    register_db(sub)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)
