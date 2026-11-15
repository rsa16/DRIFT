"""The HTTP application.

Note: API is public, read-only, and unauthenticated.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from drift.config import Settings
from drift.manifest import ManifestError, load_latest

logger = logging.getLogger("drift.api")


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


def health_payload(settings: Settings) -> dict[str, Any]:
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
        return body

    if pointer is None:
        return body

    body["snapshot_id"] = pointer.snapshot_id
    manifest_path = pointer.manifest_path(snapshot_dir)
    try:
        manifest = pointer.load_manifest(snapshot_dir)
    except ManifestError:
        logger.exception("snapshot manifest is unreadable: %s", manifest_path)
        body["snapshot_age_s"] = _age_seconds(_mtime(manifest_path.parent))
        return body

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
        )
    return body


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
    def health() -> dict[str, Any]:
        return health_payload(resolved)

    return app


app = create_app()
