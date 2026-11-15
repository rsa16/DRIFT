"""The HTTP application.

Note: API is public, read-only, and unauthenticated.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from drift.config import Settings
from drift.manifest import (
    Manifest,
    ManifestError,
    SnapshotPointer,
    Target,
    is_fixture_manifest,
    load_latest,
    window_sha256,
)

logger = logging.getLogger("drift.api")

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+\.spx$")
DEFAULT_BLINK_BINS = 8
MAX_BLINK_BINS = 16
BLINK_PLANE_BUDGET = 16
DEFAULT_TIMELINE_VISITS = 8
MIN_TIMELINE_VISITS = 2


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
    return {"enabled": enabled, "reason": reason}


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
        frame.sigma
        for visit in target.visits
        for frame in visit.frames
        if frame.band == band and frame.sigma is not None
    ]
    return sum(values) / len(values) if values else float("inf")


def _resolve_band(target: Target, band: int | None) -> int | None:
    if band is not None:
        return band
    if target.timeline and target.timeline.default_band is not None:
        return target.timeline.default_band
    counts = _band_counts(target)
    if not counts:
        return None
    return max(
        counts,
        key=lambda item: (len(counts[item]), -_band_sigma(target, item), -item),
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
    band = target.timeline.default_band if target.timeline else None
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
        "dtype": "float16",
        "href": f"/api/v1/snapshots/{manifest.snapshot_id}/planes/{filename}",
        "static_href": f"/v1/{manifest.snapshot_id}/planes/{filename}",
        "bytes": frame.bytes,
        "sha256": frame.sha256,
        "window_sha256": window_hash,
    }


def _timeline_frames(
    manifest: Manifest, target: Target, band: int | None, requested: int
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

    selected = _thin(rows, min(requested, MAX_BLINK_BINS))
    kept_ids = {visit.visit_id for visit, _ in selected}
    omitted = [visit.visit_id for visit, _ in rows if visit.visit_id not in kept_ids]
    window = _window_payload(manifest, target, resolved)
    window_hash = str(window["sha256"])
    processing = _processing(manifest)

    planes: list[dict[str, Any]] = []
    visits: list[dict[str, Any]] = []
    for visit, frame in selected:
        plane = _plane_payload(manifest, frame, window_hash, len(planes))
        plane.update(
            {
                "visit_id": visit.visit_id,
                "mjd": visit.mjd,
                "date": visit.date,
                "release": visit.release,
                "survey": visit.survey,
                "collection": visit.collection,
                "obs_publisher_did": visit.obs_publisher_did,
            }
        )
        planes.append(plane)
        visits.append(
            {
                "visit_id": visit.visit_id,
                "mjd": visit.mjd,
                "date": visit.date,
                "release": visit.release,
                "survey": visit.survey,
                "collection": visit.collection,
                "obs_publisher_did": visit.obs_publisher_did,
            }
        )

    mjds = [visit.mjd for visit, _ in selected if visit.mjd is not None]
    return {
        "snapshot_id": manifest.snapshot_id,
        "fixture": is_fixture_manifest(manifest),
        "target_id": target.id,
        "mode": "timeline",
        "band": resolved,
        "detector": detector,
        "default_band": target.timeline.default_band if target.timeline else None,
        "visits_requested": requested,
        "visits": len(selected),
        "available_visits": len(rows),
        "degraded": bool(omitted),
        "frames_omitted": omitted,
        "refused_frames": refused,
        "span_days": (max(mjds) - min(mjds)) if len(mjds) > 1 else 0.0,
        "visits_meta": visits,
        "window": window,
        "pixel_scale_arcsec": processing["pixel_scale_arcsec"],
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
) -> dict[str, Any]:
    if mode == "timeline":
        return _timeline_frames(manifest, _target(manifest, target_id), band, visits)
    if mode in {"diff", "night_pair"}:
        raise HTTPException(
            status_code=409,
            detail=f"mode {mode!r} is not available in this snapshot",
        )
    if mode == "spectrum":
        raise HTTPException(
            status_code=409,
            detail="mode 'spectrum' is served by /spectrum, not /frames",
        )
    if mode not in {"epoch", "survey_compare"}:
        raise HTTPException(status_code=400, detail=f"unknown mode {mode!r}")

    target = _target(manifest, target_id)
    if len(target.epochs) < 2:
        raise HTTPException(
            status_code=409,
            detail="Only one survey epoch is available; survey compare needs two.",
        )
    epochs = sorted(
        target.epochs, key=lambda epoch: (epoch.mjd_min or 0.0, epoch.epoch_id)
    )
    pair = [epochs[0], epochs[-1]]
    shared = _shared_bands(pair)
    if not shared:
        raise HTTPException(
            status_code=409, detail="No wavelengths are matched across two epochs."
        )

    by_band: list[tuple[int, Any]] = []
    first_frames = {item.band: item for item in pair[0].frames if item.band is not None}
    for band in shared:
        frame = first_frames.get(band)
        if frame is not None:
            by_band.append((band, frame))
    by_band.sort(key=lambda item: (item[1].lambda_um or 0.0, item[0]))
    budget = max(1, BLINK_PLANE_BUDGET // len(pair))
    selected: list[tuple[int, Any]] = _thin(by_band, min(bins, budget))

    window = _window_payload(manifest)
    window_hash = str(window["sha256"])
    processing = _processing(manifest)
    planes: list[dict[str, Any]] = []
    for band_index, item in enumerate(selected):
        band = item[0]
        band_reference: Any = item[1]
        for epoch in pair:
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
            planes.append(
                {
                    "index": len(planes),
                    "band_index": band_index,
                    "band": band,
                    "epoch_id": epoch.epoch_id,
                    "release": epoch.release,
                    "survey": epoch.survey,
                    "mjd_min": epoch.mjd_min,
                    "mjd_max": epoch.mjd_max,
                    "lambda_um": lambda_um,
                    "lambda_lo_um": lambda_um - dlambda_um / 2.0,
                    "lambda_hi_um": lambda_um + dlambda_um / 2.0,
                    "dlambda_um": dlambda_um,
                    "width": processing["plane_px"],
                    "height": processing["plane_px"],
                    "dtype": "float16",
                    "href": (
                        f"/api/v1/snapshots/{manifest.snapshot_id}/planes/{filename}"
                    ),
                    "static_href": f"/v1/{manifest.snapshot_id}/planes/{filename}",
                    "bytes": plane_frame.bytes,
                    "sha256": plane_frame.sha256,
                    "window_sha256": window_hash,
                }
            )

    return {
        "snapshot_id": manifest.snapshot_id,
        "fixture": is_fixture_manifest(manifest),
        "target_id": target.id,
        "mode": mode,
        "bins": len(selected),
        "requested_bins": bins,
        "epochs": [
            {
                "epoch_id": epoch.epoch_id,
                "release": epoch.release,
                "survey": epoch.survey,
                "mjd_min": epoch.mjd_min,
                "mjd_max": epoch.mjd_max,
            }
            for epoch in pair
        ],
        "window": window,
        "pixel_scale_arcsec": processing["pixel_scale_arcsec"],
        "units": processing["units"],
        "planes": planes,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    app = FastAPI(
        title="DRIFT",
        description="Diffrential Infrared Frame Tracker",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

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
        )
        if payload.get("degraded"):
            response.headers["X-Spx-Degraded"] = "1"
        else:
            response.headers["X-Spx-Degraded"] = "0"
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

    return app


app = create_app()
