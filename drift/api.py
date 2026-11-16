"""The HTTP application.

Note: API is public, read-only, and unauthenticated.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.gzip import GZipMiddleware

from drift.config import Settings
from drift.cubes import DifferenceStack
from drift.manifest import (
    Manifest,
    ManifestError,
    ModeEligibility,
    SnapshotPointer,
    Target,
    is_fixture_manifest,
    load_latest,
    window_sha256,
)
from drift.planes import decode_plane, encode_plane

logger = logging.getLogger("drift.api")

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+\.spx$")
DERIVED_FILENAME = re.compile(r"^[a-f0-9]{64}\.spx$")
DEFAULT_BLINK_BINS = 8
MAX_BLINK_BINS = 16
BLINK_PLANE_BUDGET = 16
DEFAULT_TIMELINE_VISITS = 8
MIN_TIMELINE_VISITS = 2
COMPRESSED_BODY_BUDGET = 8 * 1024 * 1024
MIN_RESOLUTION = 64
RGB_CHANNELS = ("r", "g", "b")


@dataclass(frozen=True)
class BudgetPlan:
    """The number and resolution selected for a response."""

    count: int
    resolution: int
    thinned: bool
    resolution_reduced: bool


@dataclass(frozen=True)
class Budget:
    """Limits for the number and compressed size of response planes."""

    max_planes: int = BLINK_PLANE_BUDGET
    max_compressed_bytes: int = COMPRESSED_BODY_BUDGET

    def adaptive_bins(self, epoch_count: int) -> int:
        """Return the number of bins that fits for the given epoch count."""
        if epoch_count <= 0:
            return 0
        return max(1, min(DEFAULT_BLINK_BINS, self.max_planes // epoch_count))

    def plan(
        self,
        available: int,
        requested: int,
        compressed_at_full_resolution: int,
    ) -> BudgetPlan:
        count = min(available, requested, self.max_planes)
        resolution = 512
        compressed = compressed_at_full_resolution
        while compressed > self.max_compressed_bytes and resolution > MIN_RESOLUTION:
            resolution //= 2
            compressed //= 4
        return BudgetPlan(
            count=count,
            resolution=resolution,
            thinned=count < available,
            resolution_reduced=resolution < 512,
        )


class HealthResponse(BaseModel):
    snapshot_id: str | None = None
    snapshot_age_s: int | None = None
    plane_count: int = 0
    disk_free_bytes: int


def _existing_target(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _disk_free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(_existing_target(path)).free)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _age_seconds(timestamp: datetime | None) -> int | None:
    if timestamp is None:
        return None
    return max(0, int((datetime.now(UTC) - timestamp).total_seconds()))


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def health_payload(settings: Settings) -> HealthResponse:
    """Return snapshot id, age, plane count, and free disk space."""
    snapshot_dir = settings.snapshot_dir
    body: dict[str, Any] = {
        "snapshot_id": None,
        "snapshot_age_s": None,
        "plane_count": 0,
        "disk_free_bytes": _disk_free_bytes(snapshot_dir),
    }

    try:
        pointer = load_latest(snapshot_dir)
    except ManifestError:
        logger.exception("latest.json is unreadable; reporting no snapshot")
        return HealthResponse.model_validate(body)

    if pointer is None:
        return HealthResponse.model_validate(body)

    body["snapshot_id"] = pointer.snapshot_id
    manifest_path = pointer.manifest_path(snapshot_dir)
    try:
        manifest = pointer.load_manifest(snapshot_dir)
    except ManifestError:
        logger.exception("snapshot manifest is unreadable: %s", manifest_path)
        body["snapshot_age_s"] = _age_seconds(_mtime(manifest_path.parent))
        return HealthResponse.model_validate(body)

    age = _age_seconds(_parse_timestamp(manifest.generated_at))
    if age is None:
        age = _age_seconds(_mtime(manifest_path.parent))
    body["snapshot_age_s"] = age
    body["plane_count"] = sum(
        1 for entry in manifest.files if entry.path.endswith(".spx")
    )
    if body["plane_count"] == 0:
        body["plane_count"] = sum(
            len(epoch.frames) for target in manifest.targets for epoch in target.epochs
        ) or sum(
            len(visit.frames) for target in manifest.targets for visit in target.visits
        )
    return HealthResponse.model_validate(body)


def _current_pointer(settings: Settings) -> SnapshotPointer:
    try:
        pointer = load_latest(settings.snapshot_dir)
    except ManifestError as exc:
        raise HTTPException(status_code=503, detail="snapshot unavailable") from exc
    if pointer is None:
        raise HTTPException(status_code=404, detail="no snapshot published")
    return pointer


def _current_manifest(settings: Settings) -> Manifest:
    pointer = _current_pointer(settings)
    try:
        manifest = pointer.load_manifest(settings.snapshot_dir)
    except ManifestError as exc:
        raise HTTPException(status_code=503, detail="snapshot unavailable") from exc
    if is_fixture_manifest(manifest) and not settings.allow_fixture:
        raise HTTPException(
            status_code=503,
            detail=(
                "fixture snapshot requires DRIFT_ALLOW_FIXTURE=1; "
                "the production path refuses synthetic data"
            ),
        )
    return manifest


def _target(manifest: Manifest, target_id: str) -> Target:
    for target in manifest.targets:
        if target.id == target_id:
            return target
    raise HTTPException(status_code=404, detail=f"unknown target {target_id!r}")


def _shared_bands(epochs: list[Any]) -> list[int]:
    if len(epochs) < 2:
        return []
    shared: set[int] = set()
    for epoch in epochs:
        bands = {frame.band for frame in epoch.frames if frame.band is not None}
        shared = bands if not shared else shared & bands
    return sorted(int(band) for band in shared)


def _mode(enabled: bool, reason: str) -> dict[str, Any]:
    return ModeEligibility(
        enabled=enabled, available=enabled, reason=reason
    ).model_dump(mode="json")


def _band_counts(target: Target) -> dict[int, list[Any]]:
    """Count visits that carry each band."""
    counts: dict[int, list[Any]] = {}
    for visit in target.visits:
        bands = {frame.band for frame in visit.frames if frame.band is not None}
        for band in bands:
            counts.setdefault(int(band), []).append(visit)
    return counts


def _band_sigma(target: Target, band: int) -> float:
    values = [
        frame.sigma for _, frame in _stack(target, band)[0] if frame.sigma is not None
    ]
    return sum(values) / len(values) if values else float("inf")


def _default_band(target: Target) -> int | None:
    """Most stackable λ-matched visits, then lowest σ, then band index."""
    bands = _band_counts(target)
    if not bands:
        return target.timeline.default_band if target.timeline else None
    counts = {band: len(_stack(target, band)[0]) for band in bands}
    return max(
        counts,
        key=lambda item: (counts[item], -_band_sigma(target, item), -item),
    )


def _resolve_band(target: Target, band: int | None) -> int | None:
    if band is not None:
        return band
    return _default_band(target)


def _band_reason(target: Target, band: int | None) -> str:
    if band is None:
        return "No band is available for a visit timeline."
    if (
        target.timeline
        and target.timeline.default_band == band
        and target.timeline.band_reason
    ):
        return target.timeline.band_reason
    matched = len(_stack(target, band)[0])
    return (
        f"Band {band} holds the most matched visits ({matched}); "
        "ties break on lower noise, then band index."
    )


def _stack(
    target: Target, band: int | None
) -> tuple[list[tuple[Any, Any]], str | None, list[str]]:
    """Chronological (visit, frame) pairs at one band through one detector."""
    if band is None:
        return [], None, []
    rows = sorted(
        (
            (visit, frame)
            for visit in target.visits
            for frame in visit.frames
            if frame.band == band
        ),
        key=lambda item: (item[0].mjd or 0.0, item[0].visit_id),
    )
    if not rows:
        return [], None, []

    tally: dict[str, int] = {}
    for visit, frame in rows:
        detector = frame.detector or visit.detector or "unknown"
        tally[detector] = tally.get(detector, 0) + 1
    keep = max(tally, key=lambda item: (tally[item], item))
    kept = [
        (visit, frame)
        for visit, frame in rows
        if (frame.detector or visit.detector or "unknown") == keep
    ]
    refused = [
        visit.visit_id
        for visit, frame in rows
        if (frame.detector or visit.detector or "unknown") != keep
    ]
    return kept, keep, refused


def _timeline_bands(target: Target) -> list[int]:
    return sorted(
        band for band in _band_counts(target) if len(_stack(target, band)[0]) >= 2
    )


def _timeline_reason(target: Target, band: int | None) -> str:
    if band is None:
        return "No band is available for a visit timeline."
    rows, _, _ = _stack(target, band)
    if len(rows) < 2:
        return (
            f"Only {len(rows)} visit at band {band}; a timeline needs two. "
            "Single visits are shown as a still, not a sequence."
        )
    return ""


def _target_modes(target: Target) -> dict[str, dict[str, Any]]:
    band = _resolve_band(target, None)
    timeline = (
        _mode(True, "")
        if not _timeline_reason(target, band)
        else _mode(False, _timeline_reason(target, band))
    )
    if len(target.epochs) < 2:
        compare = _mode(
            False,
            "Only one survey epoch is available; survey compare needs two.",
        )
    elif not _shared_bands(target.epochs):
        compare = _mode(False, "No wavelengths are matched across two epochs.")
    else:
        compare = _mode(True, "")
    spectrum = (
        _mode(True, "")
        if target.spectrum
        else _mode(False, "No spectrum is available for this target.")
    )
    return {
        "timeline": timeline,
        "survey_compare": compare,
        "spectrum": spectrum,
    }


def _processing(manifest: Manifest) -> dict[str, Any]:
    processing = manifest.processing or {}
    plane_px = int(processing.get("plane_px") or 512)
    pixel_scale = processing.get("pixel_scale_arcsec")
    if pixel_scale is None:
        pixel_scale = float(processing.get("fov_deg") or 0.87) * 3600.0 / plane_px
    return {
        "plane_px": plane_px,
        "pixel_scale_arcsec": float(pixel_scale),
        "units": str(processing.get("units") or "MJy/sr"),
        "blink_bins": int(processing.get("blink_bins") or DEFAULT_BLINK_BINS),
    }


def _target_payload(
    manifest: Manifest, target: Target, *, detail: bool = False
) -> dict[str, Any]:
    fixture = is_fixture_manifest(manifest)
    payload = target.model_dump(mode="json")
    frames = sum(len(visit.frames) for visit in target.visits)
    if frames == 0:
        frames = sum(len(epoch.frames) for epoch in target.epochs)
    band = _default_band(target)
    payload.update(
        {
            "snapshot_id": manifest.snapshot_id,
            "fixture": fixture,
            "epoch_count": len(target.epochs),
            "visit_count": len(target.visits),
            "frame_count": frames,
            "modes": _target_modes(target),
            "fixture_marked": fixture,
        }
    )
    timeline = dict(payload.get("timeline") or {})
    timeline["default_band"] = band
    timeline["band_reason"] = _band_reason(target, band)
    timeline["bands"] = _timeline_bands(target)
    payload["timeline"] = timeline
    if detail:
        payload.update(_processing(manifest))
        payload["window"] = _window_payload(manifest, target, band)
    else:
        payload.pop("epochs", None)
        payload.pop("visits", None)
        payload.pop("windows", None)
    return payload


def _thin(items: list[Any], count: int) -> list[Any]:
    if len(items) <= count:
        return items
    if count == 1:
        return [items[len(items) // 2]]
    indexes = {round(index * (len(items) - 1) / (count - 1)) for index in range(count)}
    return [items[index] for index in sorted(indexes)]


def _thin_over_mjd(rows: list[Any], count: int) -> list[Any]:
    """Select visits spread across the MJD range."""
    if len(rows) <= count:
        return rows
    if count <= 1:
        return [rows[len(rows) // 2]]

    if any(row[0].mjd is None for row in rows):
        # Without a time coordinate there is no honest MJD axis; fall back to
        # list-uniform thinning rather than treating a missing MJD as 0.
        return _thin(rows, count)
    mjds = [float(row[0].mjd) for row in rows]
    start, end = min(mjds), max(mjds)
    if end <= start:
        return _thin(rows, count)

    chosen: list[int] = []
    used: set[int] = set()
    for step in range(count):
        target = start + (end - start) * step / (count - 1)
        index = min(
            (index for index in range(len(rows)) if index not in used),
            key=lambda index: (abs(mjds[index] - target), index),
        )
        chosen.append(index)
        used.add(index)
    return [rows[index] for index in sorted(chosen)]


def _window_payload(
    manifest: Manifest, target: Target | None = None, band: int | None = None
) -> dict[str, Any]:
    """Return the window for one band stack, falling back to the snapshot window."""
    window = target.band_window(band) if target is not None else None
    source = window or manifest.window
    if source is None:
        raise HTTPException(status_code=503, detail="snapshot has no stretch window")
    payload = source.model_dump(mode="json")
    if not payload.get("sha256"):
        payload["sha256"] = window_sha256(payload)
    return payload


def _plane_payload(
    manifest: Manifest, frame: Any, window_hash: str, index: int
) -> dict[str, Any]:
    filename = Path(frame.href or "").name
    if not SAFE_FILENAME.fullmatch(filename):
        raise HTTPException(
            status_code=503,
            detail=f"snapshot has an unsafe plane path for band {frame.band}",
        )
    lambda_um = float(frame.lambda_um or 0.0)
    dlambda_um = float(frame.dlambda_um or 0.0)
    processing = _processing(manifest)
    return {
        "index": index,
        "band": frame.band,
        "detector": frame.detector,
        "lambda_um": lambda_um,
        "lambda_lo_um": lambda_um - dlambda_um / 2.0,
        "lambda_hi_um": lambda_um + dlambda_um / 2.0,
        "dlambda_um": dlambda_um,
        "width": processing["plane_px"],
        "height": processing["plane_px"],
        "pixel_scale_arcsec": processing["pixel_scale_arcsec"],
        "dtype": "float16",
        "href": f"/api/v1/snapshots/{manifest.snapshot_id}/planes/{filename}",
        "static_href": f"/v1/{manifest.snapshot_id}/planes/{filename}",
        "bytes": frame.bytes,
        "sha256": frame.sha256,
        "sigma": frame.sigma,
        "flag_bad_frac": frame.flag_bad_frac,
        "source_url": frame.source_url,
        "saturated": frame.saturated,
        "window_sha256": window_hash,
    }


def _source_plane_path(
    manifest: Manifest, frame: Any, snapshot_dir: Path | None
) -> Path:
    if snapshot_dir is None:
        raise HTTPException(status_code=503, detail="snapshot directory unavailable")
    filename = Path(frame.href or "").name
    if not SAFE_FILENAME.fullmatch(filename):
        raise HTTPException(
            status_code=503,
            detail=f"snapshot has an unsafe plane path for band {frame.band}",
        )
    path = Path(snapshot_dir) / "v1" / manifest.snapshot_id / "planes" / filename
    if not path.is_file():
        raise HTTPException(
            status_code=503, detail=f"snapshot plane {filename} is missing"
        )
    return path


def _store_derived(
    manifest: Manifest, body: bytes, cache_dir: Path | None
) -> tuple[str, str, int]:
    """Store a derived plane by content hash and return its URL."""
    if cache_dir is None:
        raise HTTPException(status_code=503, detail="derived plane cache unavailable")
    digest = hashlib.sha256(body).hexdigest()
    filename = f"{digest}.spx"
    root = Path(cache_dir) / "derived" / manifest.snapshot_id
    path = root / filename
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            descriptor, temporary = tempfile.mkstemp(
                dir=root, prefix=f".{digest}.", suffix=".tmp"
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(body)
                Path(temporary).replace(path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"derived plane cache is unavailable: {exc}",
        ) from exc
    href = f"/api/v1/snapshots/{manifest.snapshot_id}/derived/{filename}"
    return href, digest, len(body)


def _compressed_bytes(bodies: list[bytes]) -> int:
    # zlib is deliberately conservative: if this fits, a brotli body does too.
    return sum(len(zlib.compress(body, 6)) for body in bodies)


def _fit_plane_bodies(
    manifest: Manifest,
    planes: list[dict[str, Any]],
    bodies: list[bytes],
    cache_dir: Path | None,
) -> tuple[int, bool]:
    """Reduce plane resolution when the compressed response is too large."""
    budget = Budget()
    resolution = max((int(plane.get("width") or 0) for plane in planes), default=0)
    compressed = _compressed_bytes(bodies)
    planned = budget.plan(
        available=len(bodies),
        requested=len(bodies),
        compressed_at_full_resolution=compressed,
    )
    reduced = False
    if not planned.resolution_reduced:
        return compressed, False
    while compressed > budget.max_compressed_bytes and resolution > MIN_RESOLUTION:
        decoded = [decode_plane(body) for body in bodies]
        bodies = [
            encode_plane(
                item.pixels[::2, ::2],
                channels=item.header.channels,
                frame_index=item.header.frame_index,
            )
            for item in decoded
        ]
        resolution = max(1, resolution // 2)
        compressed = _compressed_bytes(bodies)
        reduced = True

    if reduced:
        for plane, body in zip(planes, bodies, strict=True):
            href, digest, size = _store_derived(manifest, body, cache_dir)
            item = decode_plane(body)
            plane.update(
                {
                    "href": href,
                    "static_href": href,
                    "sha256": digest,
                    "bytes": size,
                    "width": item.header.width,
                    "height": item.header.height,
                    "pixel_scale_arcsec": float(plane.get("pixel_scale_arcsec") or 0.0)
                    * (
                        int(plane.get("width") or item.header.width) / item.header.width
                    ),
                    "resolution_reduced": True,
                }
            )
    return compressed, reduced


def _epoch_for_visit(target: Target, visit: Any) -> str | None:
    for epoch in target.epochs:
        if visit.visit_id in epoch.visit_ids:
            return epoch.epoch_id
    return visit.release


def _timeline_frames(
    manifest: Manifest,
    target: Target,
    band: int | None,
    requested: int,
    *,
    snapshot_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    resolved = _resolve_band(target, band)
    if resolved is None:
        raise HTTPException(
            status_code=409, detail="No band is available for a timeline."
        )
    rows, detector, refused = _stack(target, resolved)
    reason = _timeline_reason(target, resolved)
    if reason:
        raise HTTPException(status_code=409, detail=reason)

    planned = Budget().plan(
        available=len(rows),
        requested=requested,
        compressed_at_full_resolution=0,
    )
    selected = _thin_over_mjd(rows, planned.count)
    kept_ids = {visit.visit_id for visit, _ in selected}
    omitted = [visit.visit_id for visit, _ in rows if visit.visit_id not in kept_ids]
    window = _window_payload(manifest, target, resolved)
    window_hash = str(window["sha256"])
    processing = _processing(manifest)

    planes: list[dict[str, Any]] = []
    visits: list[dict[str, Any]] = []
    bodies: list[bytes] = []
    for visit, frame in selected:
        plane = _plane_payload(manifest, frame, window_hash, len(planes))
        plane.update(
            {
                "visit_id": visit.visit_id,
                "epoch_id": _epoch_for_visit(target, visit),
                "mjd": visit.mjd,
                "date": visit.date,
                "release": visit.release,
                "survey": visit.survey,
                "collection": visit.collection,
                "obs_publisher_did": visit.obs_publisher_did,
            }
        )
        planes.append(plane)
        if snapshot_dir is not None:
            bodies.append(
                _source_plane_path(manifest, frame, snapshot_dir).read_bytes()
            )
        visits.append(
            {
                "visit_id": visit.visit_id,
                "epoch_id": _epoch_for_visit(target, visit),
                "mjd": visit.mjd,
                "date": visit.date,
                "release": visit.release,
                "survey": visit.survey,
                "collection": visit.collection,
                "obs_publisher_did": visit.obs_publisher_did,
            }
        )

    if bodies and len(bodies) == len(planes):
        compressed, reduced = _fit_plane_bodies(manifest, planes, bodies, cache_dir)
    else:
        compressed, reduced = 0, False
    mjds = [visit.mjd for visit, _ in selected if visit.mjd is not None]
    return {
        "snapshot_id": manifest.snapshot_id,
        "fixture": is_fixture_manifest(manifest),
        "target_id": target.id,
        "mode": "timeline",
        "band": resolved,
        "detector": detector,
        "default_band": _default_band(target),
        "visits_requested": requested,
        "visits": len(selected),
        "available_visits": len(rows),
        "plane_count": len(planes),
        "compressed_bytes": compressed,
        "degraded": bool(omitted) or reduced,
        "frames_omitted": omitted,
        "resolution_reduced": reduced,
        "refused_frames": refused,
        "span_days": (max(mjds) - min(mjds)) if len(mjds) > 1 else 0.0,
        "visits_meta": visits,
        "window": window,
        "pixel_scale_arcsec": processing["pixel_scale_arcsec"],
        "source_plane_px": processing["plane_px"],
        "units": processing["units"],
        "planes": planes,
    }


def _visit_index(rows: list[Any], value: str | int | None, *, what: str) -> int:
    if value is None or value == "":
        return 0
    text = str(value)
    for index, (visit, _frame) in enumerate(rows):
        if visit.visit_id == text:
            return index
    try:
        index = int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{what} {text!r} is not a visit in this stack",
        ) from exc
    if index < 0 or index >= len(rows):
        raise HTTPException(
            status_code=400,
            detail=f"{what} index {index} is outside the stack",
        )
    return index


def _visit_meta(visit: Any, frame: Any) -> dict[str, Any]:
    return {
        "visit_id": visit.visit_id,
        "mjd": visit.mjd,
        "date": visit.date,
        "release": visit.release,
        "survey": visit.survey,
        "collection": visit.collection,
        "obs_publisher_did": visit.obs_publisher_did,
        "detector": frame.detector or visit.detector,
        "band": frame.band,
        "sigma": frame.sigma,
        "lambda_um": frame.lambda_um,
        "dlambda_um": frame.dlambda_um,
    }


def _diff_frames(
    manifest: Manifest,
    target: Target,
    band: int | None,
    reference: str | None,
    visit: str | None,
    *,
    snapshot_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    resolved = _resolve_band(target, band)
    if resolved is None:
        raise HTTPException(
            status_code=409, detail="No band is available for a difference view."
        )
    rows, detector, refused = _stack(target, resolved)
    reason = _timeline_reason(target, resolved)
    if reason:
        raise HTTPException(status_code=409, detail=reason)

    reference_index = _visit_index(rows, reference, what="reference visit")
    reference_visit, reference_frame = rows[reference_index]
    if visit is None:
        candidates = [row for index, row in enumerate(rows) if index != reference_index]
        # The reference plane is part of the diff response's provenance, so
        # leave one slot for it inside the 16-plane stack budget.
        thinned = _thin_over_mjd(candidates, MAX_BLINK_BINS - 1)
    else:
        current_index = _visit_index(rows, visit, what="current visit")
        if current_index == reference_index:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The difference reference and the selected visit are the "
                    "same; choose another visit."
                ),
            )
        thinned = [rows[current_index]]

    if visit is None:
        kept_ids = {visit_item.visit_id for visit_item, _frame in thinned}
        omitted = [
            visit_item.visit_id
            for visit_item, _frame in rows
            if visit_item.visit_id not in kept_ids
            and visit_item.visit_id != reference_visit.visit_id
        ]
    else:
        omitted = []

    reference_path = _source_plane_path(manifest, reference_frame, snapshot_dir)
    reference_plane = decode_plane(reference_path.read_bytes())
    window = _window_payload(manifest, target, resolved)
    window_hash = str(window["sha256"])
    processing = _processing(manifest)

    planes: list[dict[str, Any]] = []
    bodies: list[bytes] = []
    for current_visit, current_frame in thinned:
        current_path = _source_plane_path(manifest, current_frame, snapshot_dir)
        current_plane = decode_plane(current_path.read_bytes())
        difference = DifferenceStack(
            reference_sigma=reference_frame.sigma,
            current_sigma=current_frame.sigma,
        ).difference(reference_plane.pixels, current_plane.pixels)
        body = encode_plane(
            difference.image,
            channels=int(current_frame.band or 0),
            frame_index=len(planes),
        )
        href, digest, size = _store_derived(manifest, body, cache_dir)
        planes.append(
            {
                **_plane_payload(
                    manifest,
                    current_frame,
                    window_hash,
                    len(planes),
                ),
                "href": href,
                "static_href": href,
                "sha256": digest,
                "bytes": size,
                "width": current_plane.header.width,
                "height": current_plane.header.height,
                "difference": True,
                "role": "difference",
                "reference_visit_id": reference_visit.visit_id,
                "source_sigma": current_frame.sigma,
                "reference_sigma": reference_frame.sigma,
                "sigma_squared": difference.sigma_squared,
                "sigma": difference.sigma,
                "saturated": bool(current_frame.saturated or reference_frame.saturated),
                "reference_saturated": bool(reference_frame.saturated),
                "flag_bad_frac": max(
                    float(current_frame.flag_bad_frac or 0.0),
                    float(reference_frame.flag_bad_frac or 0.0),
                ),
                "cross_release": (current_visit.release != reference_visit.release),
                "reference_lambda_um": reference_frame.lambda_um,
                "bandpass_delta_um": abs(
                    float(current_frame.lambda_um or 0.0)
                    - float(reference_frame.lambda_um or 0.0)
                ),
                "bandpass_fraction": (
                    abs(
                        float(current_frame.lambda_um or 0.0)
                        - float(reference_frame.lambda_um or 0.0)
                    )
                    / float(current_frame.dlambda_um or 1.0)
                ),
                "visit_id": current_visit.visit_id,
                "epoch_id": _epoch_for_visit(target, current_visit),
                "mjd": current_visit.mjd,
                "date": current_visit.date,
                "release": current_visit.release,
                "survey": current_visit.survey,
                "collection": current_visit.collection,
                "obs_publisher_did": current_visit.obs_publisher_did,
            }
        )
        bodies.append(body)

    compressed, reduced = _fit_plane_bodies(manifest, planes, bodies, cache_dir)
    return {
        "snapshot_id": manifest.snapshot_id,
        "fixture": is_fixture_manifest(manifest),
        "target_id": target.id,
        "mode": "diff",
        "band": resolved,
        "detector": detector,
        "reference": {
            **_visit_meta(reference_visit, reference_frame),
            "index": reference_index,
            "href": (
                f"/api/v1/snapshots/{manifest.snapshot_id}/planes/"
                f"{Path(reference_frame.href or '').name}"
            ),
        },
        "visits_requested": len(thinned),
        "visits": len(thinned),
        "available_visits": len(rows) - 1,
        "plane_count": len(planes),
        "compressed_bytes": compressed,
        "degraded": bool(omitted) or reduced,
        "frames_omitted": omitted,
        "resolution_reduced": reduced,
        "cross_release": any(bool(plane["cross_release"]) for plane in planes),
        "max_bandpass_fraction": max(
            (float(plane["bandpass_fraction"]) for plane in planes), default=0.0
        ),
        "refused_frames": refused,
        "window": window,
        "pixel_scale_arcsec": processing["pixel_scale_arcsec"],
        "source_plane_px": processing["plane_px"],
        "units": processing["units"],
        "planes": planes,
    }


def _rgb_frames(
    manifest: Manifest,
    target: Target,
    band: int | None,
    *,
    snapshot_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    resolved = _resolve_band(target, band)
    if resolved is None:
        raise HTTPException(
            status_code=409, detail="No band is available for temporal RGB."
        )
    rows, detector, refused = _stack(target, resolved)
    if len(rows) < 3:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Temporal RGB needs three visits at band {resolved}; this stack "
                f"has {len(rows)}. Change band or choose a target with three visits."
            ),
        )

    selected = _thin_over_mjd(rows, 3)
    kept_ids = {visit.visit_id for visit, _frame in selected}
    omitted = [
        visit.visit_id for visit, _frame in rows if visit.visit_id not in kept_ids
    ]
    window = _window_payload(manifest, target, resolved)
    window_hash = str(window["sha256"])
    processing = _processing(manifest)
    planes: list[dict[str, Any]] = []
    bodies: list[bytes] = []
    for channel, (visit, frame) in zip(RGB_CHANNELS, selected, strict=True):
        plane = _plane_payload(manifest, frame, window_hash, len(planes))
        plane.update(
            {
                "channel": channel,
                "role": "rgb",
                "visit_id": visit.visit_id,
                "epoch_id": _epoch_for_visit(target, visit),
                "mjd": visit.mjd,
                "date": visit.date,
                "release": visit.release,
                "survey": visit.survey,
                "collection": visit.collection,
                "obs_publisher_did": visit.obs_publisher_did,
            }
        )
        planes.append(plane)
        if snapshot_dir is not None:
            bodies.append(
                _source_plane_path(manifest, frame, snapshot_dir).read_bytes()
            )

    if bodies and len(bodies) == len(planes):
        compressed, reduced = _fit_plane_bodies(manifest, planes, bodies, cache_dir)
    else:
        compressed, reduced = 0, False
    return {
        "snapshot_id": manifest.snapshot_id,
        "fixture": is_fixture_manifest(manifest),
        "target_id": target.id,
        "mode": "rgb",
        "band": resolved,
        "detector": detector,
        "channels": list(RGB_CHANNELS),
        "visits": len(selected),
        "available_visits": len(rows),
        "plane_count": len(planes),
        "compressed_bytes": compressed,
        "degraded": bool(omitted) or reduced,
        "frames_omitted": omitted,
        "resolution_reduced": reduced,
        "refused_frames": refused,
        "window": window,
        "pixel_scale_arcsec": processing["pixel_scale_arcsec"],
        "source_plane_px": processing["plane_px"],
        "units": processing["units"],
        "planes": planes,
    }


def frames_payload(
    manifest: Manifest,
    target_id: str,
    mode: str,
    bins: int,
    *,
    band: int | None = None,
    visits: int = DEFAULT_TIMELINE_VISITS,
    reference: str | None = None,
    visit: str | None = None,
    epochs: str | None = None,
    snapshot_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    if mode == "timeline":
        return _timeline_frames(
            manifest,
            _target(manifest, target_id),
            band,
            visits,
            snapshot_dir=snapshot_dir,
            cache_dir=cache_dir,
        )
    target = _target(manifest, target_id)
    if mode == "diff":
        return _diff_frames(
            manifest,
            target,
            band,
            reference,
            visit,
            snapshot_dir=snapshot_dir,
            cache_dir=cache_dir,
        )
    if mode == "rgb":
        return _rgb_frames(
            manifest,
            target,
            band,
            snapshot_dir=snapshot_dir,
            cache_dir=cache_dir,
        )
    if mode == "night_pair":
        raise HTTPException(
            status_code=409,
            detail="mode 'night_pair' is not available in this snapshot",
        )
    if mode == "spectrum":
        raise HTTPException(
            status_code=409,
            detail="mode 'spectrum' is served by /spectrum, not /frames",
        )
    if mode not in {"epoch", "survey_compare"}:
        raise HTTPException(status_code=400, detail=f"unknown mode {mode!r}")

    ordered_epochs = sorted(
        target.epochs, key=lambda epoch: (epoch.mjd_min or 0.0, epoch.epoch_id)
    )
    if len(ordered_epochs) < 2:
        raise HTTPException(
            status_code=409,
            detail="Only one survey epoch is available; survey compare needs two.",
        )
    if epochs is not None and epochs.strip().lower() == "all":
        selected_epochs = list(ordered_epochs)
    elif epochs:
        selected_epochs = []
        for token in (part.strip() for part in epochs.split(",")):
            if not token:
                continue
            match = next(
                (epoch for epoch in ordered_epochs if epoch.epoch_id == token), None
            )
            if match is None:
                try:
                    match = ordered_epochs[int(token)]
                except (ValueError, IndexError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"epoch {token!r} is not in this target's timeline",
                    ) from exc
            if match not in selected_epochs:
                selected_epochs.append(match)
        if len(selected_epochs) < 2:
            raise HTTPException(
                status_code=400,
                detail="a survey compare needs two epochs; specify two or more",
            )
        selected_epochs.sort(key=lambda epoch: (epoch.mjd_min or 0.0, epoch.epoch_id))
    else:
        # The compare surface is a two-epoch blink by default; an explicit
        # ``epochs=all`` asks for the adaptive 8 x 2 / 5 x 3 / 4 x 4 stack.
        selected_epochs = [ordered_epochs[0], ordered_epochs[-1]]

    budget = Budget()
    omitted_epochs: list[str] = []
    if len(selected_epochs) > budget.max_planes:
        omitted_epochs = [
            f"epoch {epoch.epoch_id}" for epoch in selected_epochs[budget.max_planes :]
        ]
        selected_epochs = selected_epochs[: budget.max_planes]

    shared = _shared_bands(selected_epochs)
    if not shared:
        raise HTTPException(
            status_code=409, detail="No wavelengths are matched across these epochs."
        )

    by_band: list[tuple[int, Any]] = []
    first_frames = {
        item.band: item for item in selected_epochs[0].frames if item.band is not None
    }
    for band in shared:
        frame = first_frames.get(band)
        if frame is not None:
            by_band.append((band, frame))
    by_band.sort(key=lambda item: (item[1].lambda_um or 0.0, item[0]))
    budget_bins = budget.adaptive_bins(len(selected_epochs))
    requested_bins = min(bins, len(by_band))
    requested_bands = _thin(by_band, requested_bins)
    selected: list[tuple[int, Any]] = _thin(
        requested_bands, min(len(requested_bands), budget_bins)
    )
    selected_bands = {item[0] for item in selected}
    # Only bands the budget forced out are omitted; asking for four bins from
    # an eight-band target is not degradation.
    omitted_bins = [
        f"band {band}" for band, _frame in requested_bands if band not in selected_bands
    ]
    omitted = omitted_epochs + omitted_bins

    # A real compare stack usually shares one matched band, so use that band's
    # window rather than the snapshot-wide fallback (which is another target's
    # statistics). Multi-band compares keep the one-window contract by falling
    # back to the snapshot window.
    window_band = shared[0] if len(shared) == 1 else None
    window = _window_payload(manifest, target, window_band)
    window_hash = str(window["sha256"])
    processing = _processing(manifest)
    planes: list[dict[str, Any]] = []
    bodies: list[bytes] = []
    for band_index, item in enumerate(selected):
        band = item[0]
        band_reference: Any = item[1]
        for epoch in selected_epochs:
            plane_frame: Any = next(
                (item for item in epoch.frames if item.band == band),
                None,
            )
            if plane_frame is None:
                continue
            filename = Path(plane_frame.href or "").name
            if not SAFE_FILENAME.fullmatch(filename):
                raise HTTPException(
                    status_code=503,
                    detail=f"snapshot has an unsafe plane path for band {band}",
                )
            lambda_um = float(plane_frame.lambda_um or band_reference.lambda_um or 0.0)
            dlambda_um = float(
                plane_frame.dlambda_um or band_reference.dlambda_um or 0.0
            )
            extra = plane_frame.model_dump(mode="json")
            planes.append(
                {
                    "index": len(planes),
                    "band_index": band_index,
                    "band": band,
                    "epoch_id": epoch.epoch_id,
                    "release": epoch.release,
                    "survey": epoch.survey,
                    "detector": plane_frame.detector or extra.get("detector"),
                    "mjd_min": epoch.mjd_min,
                    "mjd_max": epoch.mjd_max,
                    "visit_id": extra.get("visit_id"),
                    "mjd": extra.get("mjd"),
                    "date": extra.get("date"),
                    "saturated": plane_frame.saturated,
                    "sigma": plane_frame.sigma,
                    "lambda_um": lambda_um,
                    "lambda_lo_um": lambda_um - dlambda_um / 2.0,
                    "lambda_hi_um": lambda_um + dlambda_um / 2.0,
                    "dlambda_um": dlambda_um,
                    "width": processing["plane_px"],
                    "height": processing["plane_px"],
                    "pixel_scale_arcsec": processing["pixel_scale_arcsec"],
                    "dtype": "float16",
                    "href": (
                        f"/api/v1/snapshots/{manifest.snapshot_id}/planes/{filename}"
                    ),
                    "static_href": f"/v1/{manifest.snapshot_id}/planes/{filename}",
                    "bytes": plane_frame.bytes,
                    "sha256": plane_frame.sha256,
                    "source_url": plane_frame.source_url,
                    "window_sha256": window_hash,
                }
            )
            if snapshot_dir is not None:
                bodies.append(
                    _source_plane_path(manifest, plane_frame, snapshot_dir).read_bytes()
                )

    if bodies and len(bodies) == len(planes):
        compressed, reduced = _fit_plane_bodies(manifest, planes, bodies, cache_dir)
    else:
        compressed, reduced = 0, False
    return {
        "snapshot_id": manifest.snapshot_id,
        "fixture": is_fixture_manifest(manifest),
        "target_id": target.id,
        "mode": mode,
        "bins": len(selected),
        "requested_bins": bins,
        "plane_count": len(planes),
        "compressed_bytes": compressed,
        "degraded": bool(omitted) or reduced,
        "frames_omitted": omitted,
        "resolution_reduced": reduced,
        "epochs": [
            {
                "epoch_id": epoch.epoch_id,
                "release": epoch.release,
                "survey": epoch.survey,
                "mjd_min": epoch.mjd_min,
                "mjd_max": epoch.mjd_max,
            }
            for epoch in selected_epochs
        ],
        "window": window,
        "pixel_scale_arcsec": processing["pixel_scale_arcsec"],
        "source_plane_px": processing["plane_px"],
        "units": processing["units"],
        "planes": planes,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    app = FastAPI(
        title="DRIFT",
        description="Differential Infrared Frame Tracker",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # The blink payload is float16 planes; browsers accept gzip, and without
    # this the "compressed body" budget would describe bytes never sent.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error for %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error"},
        )

    @app.get("/api/v1/health")
    def health() -> HealthResponse:
        return health_payload(resolved)

    @app.get("/api/v1/manifest")
    def manifest() -> dict[str, Any]:
        return _current_manifest(resolved).model_dump(mode="json")

    @app.get("/api/v1/targets")
    def targets() -> dict[str, Any]:
        current = _current_manifest(resolved)
        return {
            "snapshot_id": current.snapshot_id,
            "fixture": is_fixture_manifest(current),
            "targets": [_target_payload(current, target) for target in current.targets],
        }

    @app.get("/api/v1/targets/{target_id}")
    def target_detail(target_id: str) -> dict[str, Any]:
        current = _current_manifest(resolved)
        return _target_payload(current, _target(current, target_id), detail=True)

    @app.get("/api/v1/targets/{target_id}/frames")
    def target_frames(
        target_id: str,
        response: Response,
        mode: str = Query("timeline"),
        bins: int = Query(DEFAULT_BLINK_BINS, ge=1, le=MAX_BLINK_BINS),
        band: int | None = Query(None, ge=0),
        visits: int = Query(
            DEFAULT_TIMELINE_VISITS, ge=MIN_TIMELINE_VISITS, le=MAX_BLINK_BINS
        ),
        reference: str | None = Query(None),
        visit: str | None = Query(None),
        epochs: str | None = Query(None),
    ) -> dict[str, Any]:
        current = _current_manifest(resolved)
        payload = frames_payload(
            current,
            target_id,
            mode,
            bins,
            band=band,
            visits=visits,
            reference=reference,
            visit=visit,
            epochs=epochs,
            snapshot_dir=resolved.snapshot_dir,
            cache_dir=resolved.cache_dir,
        )
        response.headers["X-Spx-Degraded"] = "1" if payload.get("degraded") else "0"
        return payload

    @app.get("/api/v1/snapshots/{snapshot_id}/planes/{filename}")
    def snapshot_plane(snapshot_id: str, filename: str) -> FileResponse:
        current = _current_manifest(resolved)
        if snapshot_id != current.snapshot_id or not SAFE_FILENAME.fullmatch(filename):
            raise HTTPException(status_code=404, detail="plane not found")
        planes_root = (resolved.snapshot_dir / "v1" / snapshot_id / "planes").resolve()
        path = (planes_root / filename).resolve()
        if path.parent != planes_root or not path.is_file():
            raise HTTPException(status_code=404, detail="plane not found")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/api/v1/snapshots/{snapshot_id}/derived/{filename}")
    def derived_plane(snapshot_id: str, filename: str) -> FileResponse:
        current = _current_manifest(resolved)
        if snapshot_id != current.snapshot_id or not DERIVED_FILENAME.fullmatch(
            filename
        ):
            raise HTTPException(status_code=404, detail="derived plane not found")
        root = (Path(resolved.cache_dir) / "derived" / snapshot_id).resolve()
        path = (root / filename).resolve()
        if path.parent != root or not path.is_file():
            raise HTTPException(status_code=404, detail="derived plane not found")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    return app


app = create_app()
