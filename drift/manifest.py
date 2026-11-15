"""Manifest schema v1 and snapshot loading"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SUPPORTED_SCHEMA_VERSION = 1


class ManifestError(ValueError):
    """Raised when a manifest or snapshot pointer cannot be read or validated."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class Window(_Model):
    lo: float
    hi: float
    s: float
    sha256: str | None = None


class Frame(_Model):
    band: int | None = None
    lambda_um: float | None = None
    dlambda_um: float | None = None
    obs_publisher_did: str | None = None
    source_url: str | None = None
    href: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    sigma: float | None = None
    flag_bad_frac: float | None = None
    saturated: bool | None = None


class Epoch(_Model):
    epoch_id: str
    survey: int | None = None
    release: str | None = None
    mjd_min: float | None = None
    mjd_max: float | None = None
    frames: list[Frame] = Field(default_factory=list)


class Target(_Model):
    id: str
    ra: float | None = None
    dec: float | None = None
    pm: list[float] | None = None
    stale: bool | None = None
    epochs: list[Epoch] = Field(default_factory=list)
    spectrum: dict[str, Any] | None = None
    labels: list[str] = Field(default_factory=list)


class FileEntry(_Model):
    path: str
    bytes: int | None = None
    sha256: str | None = None


class Manifest(_Model):
    schema_version: int
    snapshot_id: str
    generated_at: str | None = None
    generator: dict[str, Any] | None = None
    processing: dict[str, Any] | None = None
    status: str | None = None
    source: dict[str, Any] | None = None
    watermark: dict[str, Any] | None = None
    targets: list[Target] = Field(default_factory=list)
    files: list[FileEntry] = Field(default_factory=list)
    alerts: list[Any] = Field(default_factory=list)
    citation: str | None = None
    window: Window | None = None

    @model_validator(mode="after")
    def _check_schema_version(self) -> Manifest:
        if self.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported manifest schema_version {self.schema_version}; "
                f"this reader supports major version {SUPPORTED_SCHEMA_VERSION}"
            )
        return self


class SnapshotPointer(_Model):
    """The contents of ``latest.json``, the only mutable file in a snapshot."""

    snapshot_id: str
    manifest: str | None = None
    published_at: str | None = None

    def manifest_path(self, snapshot_dir: str | Path) -> Path:
        relative = self.manifest or f"v1/{self.snapshot_id}/manifest.json"
        return Path(snapshot_dir) / relative

    def load_manifest(self, snapshot_dir: str | Path) -> Manifest:
        path = self.manifest_path(snapshot_dir)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
        return validate(text)


def validate(payload: dict[str, Any] | str | bytes | bytearray) -> Manifest:
    """Validate a manifest payload and return the typed model."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError(f"manifest is not valid UTF-8 JSON: {exc}") from exc

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ManifestError(
            f"manifest must be a JSON object, got {type(payload).__name__}"
        )

    try:
        return Manifest.model_validate(payload)
    except ValidationError as exc:
        raise ManifestError(f"invalid manifest: {exc}") from exc


def load_latest(snapshot_dir: str | Path) -> SnapshotPointer | None:
    """Read ``latest.json``; return ``None`` when no snapshot has been published."""
    latest = Path(snapshot_dir) / "latest.json"
    if not latest.is_file():
        return None

    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read {latest}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ManifestError(f"{latest} must contain a JSON object")
    try:
        return SnapshotPointer.model_validate(payload)
    except ValidationError as exc:
        raise ManifestError(f"invalid snapshot pointer in {latest}: {exc}") from exc
