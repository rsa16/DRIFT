"""Command line entry point for the ingest package."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import replace
from pathlib import Path

from drift.cubes import (
    DEFAULT_CHANNELS,
    Exposure,
    PairBuilder,
    propagate_position,
    read_cutout,
)
from drift.ingest.fixture import FixtureStore
from drift.ingest.record import record, replay
from drift.ingest.targets import TARGETS

DEFAULT_PAIR_OUTPUT = Path("build/planes")
EPOCH_GAP_DAYS = 180.0


def release_from_collection(collection: str) -> str:
    """Map a SIA2 collection to its release, ignoring the ``_deep`` survey mode."""
    release = collection.removeprefix("spherex_")
    if release == "qr2" or release.startswith("qr2_"):
        raise ValueError("QR2 never enters a comparison")
    if release.endswith("_deep"):
        release = release.removesuffix("_deep")
    return release


def assign_epoch_ids(
    exposures: list[Exposure], *, gap_days: float = EPOCH_GAP_DAYS
) -> list[Exposure]:
    """Split each release into observation-time clusters (~survey passes)."""
    by_release: dict[str, list[Exposure]] = {}
    for exposure in exposures:
        by_release.setdefault(exposure.release, []).append(exposure)

    grouped: list[Exposure] = []
    for release in sorted(by_release):
        ordered = sorted(by_release[release], key=lambda item: item.mjd)
        clusters: list[list[Exposure]] = []
        current = [ordered[0]]
        for exposure in ordered[1:]:
            if exposure.mjd - current[-1].mjd > gap_days:
                clusters.append(current)
                current = [exposure]
            else:
                current.append(exposure)
        clusters.append(current)
        for index, cluster in enumerate(clusters, start=1):
            epoch_id = release if len(clusters) == 1 else f"{release}-{index}"
            grouped.extend(replace(exposure, epoch_id=epoch_id) for exposure in cluster)
    return grouped


def load_fixture_exposures(fixture: str | Path, target: str) -> list[Exposure]:
    """Load every recorded cutout for ``target`` from fixture."""
    store = FixtureStore(fixture)
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    ra, dec, pm_ra, pm_dec = TARGETS[target]
    exposures: list[Exposure] = []
    for entry in store.index["cutouts"]:
        if entry.get("target") != target:
            continue
        body = store.cutout(str(entry["url"]))
        cutout = read_cutout(body)
        mjd = float(entry["mjd"])
        center_ra, center_dec = propagate_position(ra, dec, pm_ra, pm_dec, mjd)
        wavelength, bandwidth = cutout.target_wavelength(center_ra, center_dec)
        release = release_from_collection(str(entry["collection"]))
        exposures.append(
            Exposure(
                cutout=cutout,
                epoch_id=release,
                release=release,
                mjd=mjd,
                source_url=str(entry["url"]),
                obs_publisher_did=str(entry["obs_publisher_did"]),
                target_wavelength_um=wavelength,
                target_bandwidth_um=bandwidth,
                sample_id=str(entry["obs_publisher_did"]),
            )
        )
    if not exposures:
        raise ValueError(f"fixture has no cutouts for {target!r}")
    return assign_epoch_ids(exposures)


def pair(
    fixture: str | Path,
    target: str,
    out: str | Path,
    *,
    channels: int = DEFAULT_CHANNELS,
) -> int:
    """Build the matched-wavelength pair for one fixture target."""
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    ra, dec, pm_ra, pm_dec = TARGETS[target]
    exposures = load_fixture_exposures(fixture, target)
    result = PairBuilder(channels=channels).build(
        target,
        exposures,
        target_ra=ra,
        target_dec=dec,
        pm_ra_mas=pm_ra,
        pm_dec_mas=pm_dec,
    )
    sidecar = result.write(out)
    print(
        f"pair: {result.matched_bin_count} matched bins, "
        f"{len(result.planes)} planes, {sidecar}"
    )
    return 0


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
    record_parser.add_argument(
        "--cutout-size-deg",
        type=float,
        default=None,
        help="override every recorded cutout size in degrees",
    )
    replay_parser = subparsers.add_parser(
        "replay", help="replay the fixture with no network"
    )
    replay_parser.add_argument(
        "--fixture",
        required=True,
        help="directory holding the recorded fixture",
    )
    pair_parser = subparsers.add_parser(
        "pair", help="build a matched-wavelength epoch pair from a fixture"
    )
    pair_parser.add_argument(
        "--fixture",
        required=True,
        help="directory holding the recorded fixture",
    )
    pair_parser.add_argument("--target", required=True, help="fixture target id")
    pair_parser.add_argument(
        "--out",
        default=str(DEFAULT_PAIR_OUTPUT),
        help="directory for SPX1 planes and the metadata sidecar",
    )
    pair_parser.add_argument(
        "--channels",
        type=int,
        default=DEFAULT_CHANNELS,
        help="wavelength channels per matched bin (at least 2)",
    )
    fixture_parser = subparsers.add_parser("fixture", help="build a synthetic snapshot")
    fixture_parser.add_argument(
        "--synthetic",
        action="store_true",
        help="explicitly acknowledge that this snapshot is synthetic",
    )
    fixture_parser.add_argument(
        "--snapshot-dir",
        default=os.environ.get("DRIFT_SNAPSHOT_DIR", "/data/snapshots"),
        help="snapshot directory to publish into (default: DRIFT_SNAPSHOT_DIR)",
    )
    build_parser = subparsers.add_parser(
        "build", help="build a published snapshot from the recorded fixture"
    )
    build_parser.add_argument(
        "--fixture",
        required=True,
        help="directory holding the recorded fixture",
    )
    build_parser.add_argument(
        "--targets",
        default="bamberga,lpv,barnard",
        help="comma-separated target ids (default: bamberga,lpv,barnard)",
    )
    build_parser.add_argument(
        "--snapshot-dir",
        default=os.environ.get("DRIFT_SNAPSHOT_DIR", "/data/snapshots"),
        help="snapshot directory to publish into (default: DRIFT_SNAPSHOT_DIR)",
    )
    build_parser.add_argument(
        "--generated-at",
        default=None,
        help="fixed ISO timestamp for a reproducible snapshot id",
    )

    args = parser.parse_args(argv)
    if args.command == "record":
        return record(
            args.fixture,
            force=args.force,
            cutout_size_deg=args.cutout_size_deg,
        )
    if args.command == "pair":
        return pair(
            args.fixture,
            args.target,
            args.out,
            channels=args.channels,
        )
    if args.command == "replay":
        return replay(args.fixture)
    if args.command == "build":
        from drift.ingest.real import build_real_snapshot

        targets = tuple(
            item.strip() for item in args.targets.split(",") if item.strip()
        )
        pointer = build_real_snapshot(
            args.snapshot_dir,
            fixture=args.fixture,
            targets=targets,
            generated_at=args.generated_at,
        )
        print(f"build: published {pointer.snapshot_id} to {args.snapshot_dir}")
        return 0
    if not args.synthetic:
        parser.error(
            "fixture requires --synthetic; production snapshots use the "
            "archive ingestion path without allow_fixture"
        )

    from drift.ingest.synthetic import build_synthetic_snapshot

    pointer = build_synthetic_snapshot(args.snapshot_dir)
    print(f"fixture: published {pointer.snapshot_id} to {args.snapshot_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
