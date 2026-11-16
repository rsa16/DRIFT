"""Build a published snapshot from the recorded archive data.

The archive cutouts are decoded, reprojected, and grouped into visits. A visit
is added to a band's stack only when its measured wavelength is within half
that band's bin width of every other visit already in the stack.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from reproject import reproject_interp  # type: ignore[import-untyped]

from drift.archive import parse_sia_csv
from drift.cubes import (
    OUTPUT_SIZE,
    PIXEL_SCALE_ARCSEC,
    Reprojector,
    SpectralCutout,
    propagate_position,
    read_cutout,
)
from drift.ingest.cli import release_from_collection
from drift.ingest.fixture import FixtureStore
from drift.ingest.targets import TARGETS
from drift.manifest import SnapshotPointer, publish_snapshot, window_sha256
from drift.planes import encode_plane

DEFAULT_TARGETS = ("bamberga", "lpv", "barnard")
VISIT_WINDOW_DAYS = 2.0 / 24.0
MIN_BIN_CHANNELS = 2

# A bin can widen to include another visit, but beyond this size it covers the
# detector range rather than matching wavelengths. Six channels is the widest
# bin that keeps the recorded Bamberga pair within half a bin.
MAX_BIN_CHANNELS = 6
UNITS = "MJy/sr"
FOV_DEG = OUTPUT_SIZE * PIXEL_SCALE_ARCSEC / 3600.0
MJD_EPOCH = datetime(1858, 11, 17, tzinfo=UTC)
SURVEYS = {"dr1": 1, "qr3": 3}

CITATION = (
    "This publication makes use of data products from the Spectro-Photometer "
    "for the History of the Universe, Epoch of Reionization and Ices Explorer "
    "(SPHEREx), which is a joint project of the Jet Propulsion Laboratory and "
    "the California Institute of Technology, and is funded by the National "
    "Aeronautics and Space Administration."
)

TARGET_INFO: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "barnard": (
        "Barnard's Star",
        "M dwarf",
        (
            "Only visits whose measured wavelengths agree within half a bin "
            "share this stack; motion below one pixel is measured, not seen.",
        ),
    ),
    "bamberga": (
        "324 Bamberga",
        "main-belt asteroid",
        (
            "The two visits are 1.288 d apart. Both recorded cutouts cover "
            "0.1 deg, so the second visit does not contain the asteroid's "
            "1.29-day position; the stack shows the shared star field.",
        ),
    ),
    "lpv": (
        "NEP long-period variable",
        "LPV",
        (
            "Nine recorded D4 visits span 2.42-3.37 um. Only the visits that "
            "agree within half a bin of a matched window are stacked; the "
            "rest are excluded rather than compared at a different wavelength.",
        ),
    ),
}


class RealSnapshotError(ValueError):
    """The recorded fixture cannot produce a real snapshot."""


@dataclass(frozen=True)
class RecordedVisit:
    """One recorded exposure at the target position."""

    target_id: str
    detector: str
    release: str
    survey: int
    collection: str
    mjd: float
    source_url: str
    obs_publisher_did: str
    cutout: SpectralCutout
    wavelength_um: float
    bandwidth_um: float
    saturated: bool

    @property
    def visit_id(self) -> str:
        return f"{self.release}-{self.mjd:.5f}-{self.detector}"

    @property
    def date(self) -> str:
        return _isoformat(MJD_EPOCH + timedelta(days=self.mjd))


@dataclass
class BandStack:
    """The visits that share one matched wavelength window."""

    band: int
    detector: str
    centre_um: float
    width_um: float
    visits: list[RecordedVisit]
    planes: list[dict[str, Any]]
    window: dict[str, Any]


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _robust_sigma(image: np.ndarray) -> float:
    values = image[np.isfinite(image)]
    if values.size == 0:
        return 0.0
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    sigma = 1.4826 * mad
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = float(np.std(values))
    return max(0.0, sigma) if math.isfinite(sigma) else 0.0


def _stretch_window(arrays: list[np.ndarray]) -> dict[str, Any]:
    values = np.concatenate(
        [array.reshape(-1) for array in arrays if np.isfinite(array).any()]
    )
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise RealSnapshotError("a band stack has no finite pixels for a window")
    median = float(np.median(values))
    sigma = float(np.std(values))
    lo = median - 3.0 * sigma
    hi = float(np.percentile(values, 99.9))
    if hi <= lo:
        hi = median + 3.0 * sigma
    window: dict[str, Any] = {
        "lo": lo,
        "hi": hi,
        "s": max(3.0 * sigma, 1e-6),
    }
    window["sha256"] = window_sha256(window)
    return window


def _overflow_in_aperture(
    cutout: SpectralCutout, ra: float, dec: float, radius: int = 5
) -> bool:
    """Whether the OVERFLOW bit is set inside a small aperture at the target."""
    bits = [bit for bit, name in cutout.flag_names.items() if name == "OVERFLOW"]
    if not bits:
        return False
    x, y = cutout.sky_wcs.all_world2pix([ra], [dec], 0)
    x0, y0 = int(round(float(x[0]))), int(round(float(y[0])))
    if not (0 <= x0 < cutout.flags.shape[1] and 0 <= y0 < cutout.flags.shape[0]):
        return False
    window = cutout.flags[
        max(0, y0 - radius) : y0 + radius + 1,
        max(0, x0 - radius) : x0 + radius + 1,
    ]
    mask = np.zeros(window.shape, dtype=bool)
    for bit in bits:
        mask |= (window & (1 << bit)) != 0
    return bool(mask.any())


def load_recorded_visits(fixture: str | Path, target_id: str) -> list[RecordedVisit]:
    """Decode every recorded cutout for ``target_id`` and dedupe visits."""
    if target_id not in TARGETS:
        raise RealSnapshotError(f"unknown target {target_id!r}")
    ra, dec, pm_ra, pm_dec = TARGETS[target_id]
    store = FixtureStore(fixture)
    visits: list[RecordedVisit] = []
    for entry in store.index["cutouts"]:
        if entry.get("target") != target_id:
            continue
        mjd = float(entry["mjd"])
        cutout = read_cutout(store.cutout(str(entry["url"])))
        centre_ra, centre_dec = propagate_position(ra, dec, pm_ra, pm_dec, mjd)
        wavelength, bandwidth = cutout.target_wavelength(centre_ra, centre_dec)
        collection = str(entry["collection"])
        visits.append(
            RecordedVisit(
                saturated=_overflow_in_aperture(cutout, centre_ra, centre_dec),
                target_id=target_id,
                detector=str(entry["band"]),
                release=release_from_collection(collection),
                survey=SURVEYS.get(release_from_collection(collection), 0),
                collection=collection,
                mjd=mjd,
                source_url=str(entry["url"]),
                obs_publisher_did=str(entry["obs_publisher_did"]),
                cutout=cutout,
                wavelength_um=wavelength,
                bandwidth_um=bandwidth,
            )
        )
    if not visits:
        raise RealSnapshotError(f"fixture has no cutouts for {target_id!r}")
    return _dedupe_visits(visits)


def _dedupe_visits(visits: list[RecordedVisit]) -> list[RecordedVisit]:
    """Rows of one detector within two hours are one visit.

    Each detector is deduplicated on its own median wavelength, so adding or
    removing another detector cannot change which row a group keeps.
    """
    kept: list[RecordedVisit] = []
    for detector in sorted({visit.detector for visit in visits}):
        members = sorted(
            (visit for visit in visits if visit.detector == detector),
            key=lambda visit: visit.mjd,
        )
        centre = float(np.median([visit.wavelength_um for visit in members]))
        group: list[RecordedVisit] = []
        for visit in members:
            if group and visit.mjd - group[0].mjd > VISIT_WINDOW_DAYS:
                kept.append(
                    min(group, key=lambda item: abs(item.wavelength_um - centre))
                )
                group = []
            group.append(visit)
        if group:
            kept.append(min(group, key=lambda item: abs(item.wavelength_um - centre)))
    return sorted(kept, key=lambda visit: visit.mjd)


def _clusters(
    visits: list[RecordedVisit], width_um: float
) -> list[list[RecordedVisit]]:
    """Group visits whose pairwise wavelength differences fit half a bin."""
    half = 0.5 * width_um
    ordered = sorted(visits, key=lambda visit: visit.wavelength_um)
    clusters: list[list[RecordedVisit]] = []
    current: list[RecordedVisit] = []
    for visit in ordered:
        if (
            not current
            or visit.wavelength_um - current[0].wavelength_um <= half + 1e-12
        ):
            current.append(visit)
        else:
            clusters.append(current)
            current = [visit]
    if current:
        clusters.append(current)
    return clusters


def _matched_bands(
    visits: list[RecordedVisit],
) -> tuple[float, list[list[RecordedVisit]]]:
    """Choose the narrowest bin that admits the most visits, up to the cap."""
    bandwidth = float(np.median([visit.bandwidth_um for visit in visits]))
    if not math.isfinite(bandwidth) or bandwidth <= 0:
        raise RealSnapshotError("recorded visits have no usable bandwidth")

    best_size = 0
    best_width = MIN_BIN_CHANNELS * bandwidth
    best_clusters: list[list[RecordedVisit]] = []
    for channels in range(MIN_BIN_CHANNELS, MAX_BIN_CHANNELS + 1):
        width = channels * bandwidth
        clusters = _clusters(visits, width)
        size = max((len(cluster) for cluster in clusters), default=0)
        if size > best_size:
            best_size = size
            best_width = width
            best_clusters = clusters
    return best_width, best_clusters


def _reproject(
    visit: RecordedVisit,
    output_wcs: Any,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Reproject one cutout and return its image plus per-plane facts."""
    cutout = visit.cutout
    shape = tuple(output_wcs.array_shape)

    def rep(data: np.ndarray, *, order: str) -> tuple[np.ndarray, np.ndarray]:
        return reproject_interp(
            (data, cutout.sky_wcs),
            output_wcs,
            shape_out=shape,
            order=order,
            return_footprint=True,
        )

    image, footprint = rep(cutout.image, order="bilinear")
    variance, _ = rep(cutout.variance, order="bilinear")
    flags, _ = rep(cutout.flags.astype(float), order="nearest-neighbor")
    bad_mask, _ = rep(cutout.bad_mask.astype(float), order="nearest-neighbor")

    image = np.asarray(image, dtype=np.float64)
    variance = np.asarray(variance, dtype=np.float64)
    valid = (np.asarray(footprint) > 0) & np.isfinite(image)
    bad = np.asarray(bad_mask) >= 0.5
    image = np.where(valid & ~bad, image, np.nan)
    variance = np.where(
        valid & ~bad & np.isfinite(variance) & (variance >= 0),
        variance,
        np.nan,
    )
    total = int(np.count_nonzero(valid))
    bad_count = int(np.count_nonzero(bad & valid))
    return image, {
        "sigma": _robust_sigma(image),
        "flag_bad_frac": bad_count / total if total else 0.0,
        "saturated": visit.saturated,
    }


def _row_metadata(fixture: str | Path, targets: set[str]) -> tuple[dict[str, int], str]:
    """Count archive SIA2 rows and hash the recorded search responses."""
    store = FixtureStore(fixture)
    counts: dict[str, int] = {}
    digest = hashlib.sha256()
    searches = sorted(
        store.index["searches"], key=lambda item: str(item.get("url", ""))
    )
    for search in searches:
        if search.get("target") not in targets:
            continue
        body = store.response(str(search["url"]))
        digest.update(body)
        collection = str(search["collection"])
        counts[collection] = counts.get(collection, 0) + len(parse_sia_csv(body))
    return counts, digest.hexdigest()


def _build_target(
    target_id: str,
    visits: list[RecordedVisit],
    planes_dir: Path,
    frame_counter: list[int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build every matched band stack for one target."""
    ra, dec, pm_ra, pm_dec = TARGETS[target_id]
    bands_by_detector: dict[str, tuple[float, list[list[RecordedVisit]]]] = {}
    for detector in sorted({visit.detector for visit in visits}):
        members = [visit for visit in visits if visit.detector == detector]
        if len(members) < MIN_BIN_CHANNELS:
            continue
        bands_by_detector[detector] = _matched_bands(members)

    stacks: list[BandStack] = []
    band_number = 0
    for detector, (width_um, clusters) in bands_by_detector.items():
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            band_number += 1
            reference_mjd = float(np.mean([visit.mjd for visit in cluster]))
            centre_ra, centre_dec = propagate_position(
                ra, dec, pm_ra, pm_dec, reference_mjd
            )
            output_wcs = Reprojector().output_wcs(centre_ra, centre_dec)
            images: list[np.ndarray] = []
            frames: list[dict[str, Any]] = []
            for visit in sorted(cluster, key=lambda item: item.mjd):
                image, facts = _reproject(visit, output_wcs)
                images.append(image)
                filename = f"{target_id}_{band_number:02d}_{len(frames):02d}.spx"
                body = encode_plane(
                    image,
                    channels=MIN_BIN_CHANNELS,
                    frame_index=frame_counter[0],
                )
                frame_counter[0] += 1
                (planes_dir / filename).write_bytes(body)
                frames.append(
                    {
                        "band": band_number,
                        "detector": detector,
                        "lambda_um": round(visit.wavelength_um, 6),
                        "dlambda_um": round(width_um, 6),
                        "obs_publisher_did": visit.obs_publisher_did,
                        "source_url": visit.source_url,
                        "href": f"v1/__SNAPSHOT__/planes/{filename}",
                        "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "sigma": facts["sigma"],
                        "flag_bad_frac": facts["flag_bad_frac"],
                        "saturated": facts["saturated"],
                    }
                )
            window = _stretch_window(images)
            window["band"] = band_number
            centre = float(np.mean([visit.wavelength_um for visit in cluster]))
            stacks.append(
                BandStack(
                    band=band_number,
                    detector=detector,
                    centre_um=centre,
                    width_um=width_um,
                    visits=sorted(cluster, key=lambda item: item.mjd),
                    planes=frames,
                    window=window,
                )
            )

    if not stacks:
        raise RealSnapshotError(f"{target_id} has no band with two matched visits")

    # Band 1 is the stack with the most visits; ties break on lower noise.
    stacks.sort(
        key=lambda stack: (
            -len(stack.visits),
            float(np.mean([frame["sigma"] for frame in stack.planes])),
            stack.band,
        )
    )
    for index, stack in enumerate(stacks, start=1):
        stack.band = index
        for frame in stack.planes:
            frame["band"] = index
        stack.window["band"] = index

    by_visit: dict[str, dict[str, Any]] = {}
    by_visit_band: dict[str, BandStack] = {}
    for stack in stacks:
        for visit, frame in zip(stack.visits, stack.planes, strict=True):
            by_visit[visit.visit_id] = frame
            by_visit_band[visit.visit_id] = stack

    ordered_visits = sorted(
        (visit for visit in visits if visit.visit_id in by_visit),
        key=lambda visit: visit.mjd,
    )
    manifest_visits: list[dict[str, Any]] = []
    for visit in ordered_visits:
        frame = dict(by_visit[visit.visit_id])
        frame["href"] = str(frame["href"])
        manifest_visits.append(
            {
                "visit_id": visit.visit_id,
                "mjd": visit.mjd,
                "date": visit.date,
                "release": visit.release,
                "survey": visit.survey,
                "collection": visit.collection,
                "obs_publisher_did": visit.obs_publisher_did,
                "detector": visit.detector,
                "frames": [frame],
            }
        )

    epochs: list[dict[str, Any]] = []
    for release in sorted({visit.release for visit in ordered_visits}):
        members = [visit for visit in ordered_visits if visit.release == release]
        epoch_frames: list[dict[str, Any]] = []
        for stack in sorted(stacks, key=lambda item: item.band):
            candidates = [
                (visit, by_visit[visit.visit_id])
                for visit in members
                if by_visit_band[visit.visit_id].band == stack.band
            ]
            if not candidates:
                continue
            candidate, frame = min(
                candidates,
                key=lambda item: abs(item[0].wavelength_um - stack.centre_um),
            )
            epoch_frame = dict(frame)
            epoch_frame.update(
                {
                    "visit_id": candidate.visit_id,
                    "mjd": candidate.mjd,
                    "date": candidate.date,
                    "detector": candidate.detector,
                    "survey": candidate.survey,
                    "collection": candidate.collection,
                }
            )
            epoch_frames.append(epoch_frame)
        epochs.append(
            {
                "epoch_id": release,
                "survey": SURVEYS.get(release, 0),
                "release": release,
                "mjd_min": min(visit.mjd for visit in members),
                "mjd_max": max(visit.mjd for visit in members),
                "visit_ids": [visit.visit_id for visit in members],
                "frames": epoch_frames,
            }
        )

    default = stacks[0]
    mjds = [visit.mjd for visit in default.visits]
    name, kind, caveats = TARGET_INFO.get(target_id, (target_id, "target", tuple()))
    detector_visits = sum(1 for visit in visits if visit.detector == default.detector)
    labels = (
        ["saturated"]
        if any(
            frame["saturated"] for visit in manifest_visits for frame in visit["frames"]
        )
        else []
    )
    target = {
        "id": target_id,
        "name": name,
        "kind": kind,
        "ra": ra,
        "dec": dec,
        "pm": [pm_ra, pm_dec],
        "stale": False,
        "visits": manifest_visits,
        "epochs": epochs,
        "timeline": {
            "default_band": default.band,
            "default_detector": default.detector,
            "visits": len(default.visits),
            "span_days": max(mjds) - min(mjds),
            "band_reason": (
                f"Band {default.band} holds the most matched visits "
                f"({len(default.visits)} of {detector_visits} "
                f"{default.detector}); every pair agrees within half a "
                f"{default.width_um:.3f} um bin. Ties break on lower noise."
            ),
        },
        "windows": [
            dict(stack.window) for stack in sorted(stacks, key=lambda item: item.band)
        ],
        "spectrum": None,
        "labels": labels,
        "caveats": list(caveats),
    }
    return target, [dict(stack.window) for stack in stacks]


def build_real_snapshot(
    snapshot_dir: str | Path,
    *,
    fixture: str | Path,
    targets: tuple[str, ...] = DEFAULT_TARGETS,
    generated_at: datetime | str | None = None,
) -> SnapshotPointer:
    """Build and publish a schema-v1 snapshot from the recorded cutouts."""
    if not targets:
        raise RealSnapshotError("no targets requested for the real snapshot")
    snapshots = Path(snapshot_dir)
    snapshots.mkdir(parents=True, exist_ok=True)
    if isinstance(generated_at, str):
        generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    if generated_at is not None and generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    generated = generated_at.astimezone(UTC) if generated_at else datetime.now(UTC)
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")

    with tempfile.TemporaryDirectory(prefix=".build-", dir=snapshots) as temporary:
        staging = Path(temporary)
        planes_dir = staging / "planes"
        planes_dir.mkdir()

        target_payloads: list[dict[str, Any]] = []
        all_windows: list[dict[str, Any]] = []
        frame_counter = [0]
        row_counts, rows_sha256 = _row_metadata(fixture, set(targets))
        collections = set(row_counts)
        for target_id in targets:
            visits = load_recorded_visits(fixture, target_id)
            target, windows = _build_target(
                target_id, visits, planes_dir, frame_counter
            )
            target_payloads.append(target)
            all_windows.extend(windows)

        plane_files = sorted(planes_dir.glob("*.spx"))
        digest = hashlib.sha256()
        file_entries: list[dict[str, Any]] = []
        for path in plane_files:
            body = path.read_bytes()
            digest.update(path.name.encode("utf-8"))
            digest.update(body)
            file_entries.append(
                {
                    "path": f"v1/__SNAPSHOT__/planes/{path.name}",
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            )
        snapshot_id = f"{stamp}-{digest.hexdigest()[:8]}"
        generated_iso = _isoformat(generated)

        for target in target_payloads:
            for visit in target["visits"]:
                for frame in visit["frames"]:
                    frame["href"] = str(frame["href"]).replace(
                        "__SNAPSHOT__", snapshot_id
                    )
            for epoch in target["epochs"]:
                for frame in epoch["frames"]:
                    frame["href"] = str(frame["href"]).replace(
                        "__SNAPSHOT__", snapshot_id
                    )
        for entry in file_entries:
            entry["path"] = str(entry["path"]).replace("__SNAPSHOT__", snapshot_id)

        default_window = dict(all_windows[0])
        default_window.pop("band", None)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "generated_at": generated_iso,
            "fixture": False,
            "generator": {
                "name": "spherex-epoch-viewer",
                "version": "0.1.0",
                "commit": "recorded-fixture",
            },
            "processing": {
                "plane_px": OUTPUT_SIZE,
                "fov_deg": FOV_DEG,
                "pixel_scale_arcsec": PIXEL_SCALE_ARCSEC,
                "units": UNITS,
                "dtype": "float16",
                "reprojector": "reproject_interp+nearest",
                "band_match": (
                    f"{MIN_BIN_CHANNELS}-{MAX_BIN_CHANNELS} channels; every pair "
                    "within 0.5 * bin width"
                ),
                "timeline_visits": 16,
                "default_visits": 8,
                "blink_bins": 8,
                "stretch": "lo=median-3sigma; hi=p99.9; s=3sigma",
            },
            "status": "published",
            "source": {
                "service": "IRSA SIA2",
                "collections": sorted(collections),
                "queried_at": generated_iso,
                "row_counts": row_counts,
            },
            "watermark": {
                "prev_snapshot_id": None,
                "rows_sha256": rows_sha256,
            },
            "targets": target_payloads,
            "files": file_entries,
            "alerts": [],
            "citation": CITATION,
            "window": default_window,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return publish_snapshot(staging, snapshots)
