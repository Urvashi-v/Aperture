"""Command line entry point for the seeder.

    shop-seed --profile small
    shop-seed --profile medium --seed 99
    shop-seed --report
    shop-seed --list-profiles
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from shop.config import get_settings
from shop.logging_config import configure_logging
from shop.seed.profiles import PROFILES, get_profile
from shop.seed.seeder import SchemaMissingError, report_counts, seed_database


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shop-seed",
        description="Load a reproducible dataset into the sample-shop database.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help=f"Dataset size. One of: {', '.join(sorted(PROFILES))}. "
        "Defaults to SEED_PROFILE from the environment.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed. The same seed reproduces the same dataset exactly.",
    )
    parser.add_argument(
        "--no-analyze",
        action="store_true",
        help="Skip the trailing ANALYZE. Only useful when chaining seed runs; "
        "the planner needs statistics for the index pathologies to reproduce.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print the real row counts currently in the database and exit.",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="Print the available profiles and their target row counts.",
    )
    parser.add_argument(
        "--log-format",
        choices=("json", "console"),
        default="console",
        help="Log output format (default: console, since this is an interactive tool).",
    )
    return parser


def _print_profiles() -> None:
    print(f"{'profile':<8} {'users':>9} {'products':>10} {'orders':>10} "
          f"{'reviews':>10} {'posts':>10} {'~total':>12}")
    for name in ("tiny", "small", "medium", "large"):
        p = PROFILES[name]
        print(
            f"{p.name:<8} {p.users:>9,} {p.products:>10,} {p.orders:>10,} "
            f"{p.reviews:>10,} {p.posts:>10,} {p.approx_total_rows():>12,}"
        )
    print()
    for name in ("tiny", "small", "medium", "large"):
        print(f"  {name:<8} {PROFILES[name].description}")


def _print_counts(counts: dict[str, int]) -> None:
    width = max(len(name) for name in counts)
    total = 0
    for table, count in counts.items():
        total += count
        print(f"  {table:<{width}}  {count:>12,}")
    print(f"  {'TOTAL':<{width}}  {total:>12,}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_profiles:
        _print_profiles()
        return 0

    settings = get_settings()
    configure_logging(settings.shop_log_level, args.log_format)

    try:
        if args.report:
            counts = asyncio.run(report_counts(settings.asyncpg_dsn))
            print("\nRows currently in the database:")
            _print_counts(counts)
            return 0

        profile = get_profile(args.profile or settings.seed_profile)
        random_seed = args.seed if args.seed is not None else settings.seed_random_seed

        result = asyncio.run(
            seed_database(
                settings.asyncpg_dsn,
                profile,
                random_seed,
                analyze=not args.no_analyze,
            )
        )
    except SchemaMissingError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"\nERROR: could not reach PostgreSQL at "
            f"{settings.postgres_host}:{settings.postgres_port} ({exc}).\n"
            "Is it running?  docker compose up -d postgres",
            file=sys.stderr,
        )
        return 3

    print(
        f"\nSeeded profile '{result.profile}' (random seed {result.seed}) "
        f"in {result.duration_s:.1f}s."
    )
    print("Rows actually written:")
    _print_counts(result.counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
