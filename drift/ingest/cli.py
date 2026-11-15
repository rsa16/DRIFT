"""Command line entry point for the ingest package."""

from __future__ import annotations

import argparse
import logging

from drift.ingest.record import record, replay


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s %(name)s %(message)s",
        )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(prog="python -m drift.ingest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    record_parser = subparsers.add_parser("record", help="record the archive fixture")
    record_parser.add_argument(
        "--fixture",
        required=True,
        help="directory to write the recorded fixture into",
    )
    record_parser.add_argument("--force", action="store_true")
    replay_parser = subparsers.add_parser(
        "replay", help="replay the fixture with no network"
    )
    replay_parser.add_argument(
        "--fixture",
        required=True,
        help="directory holding the recorded fixture",
    )

    args = parser.parse_args(argv)
    if args.command == "record":
        return record(args.fixture, force=args.force)
    return replay(args.fixture)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
