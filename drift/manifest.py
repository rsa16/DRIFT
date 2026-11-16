"""Manifest schema v1 and snapshot loading"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SUPPORTED_SCHEMA_VERSION = 1
SAFE_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ManifestError(ValueError):
    """Raised when a manifest or snapshot pointer cannot be read or validated."""


class FixtureSnapshotError(ManifestError):
    """A fixture snapshot was offered to the production publish path."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class Label(StrEnum):
    """The fixed label taxonomy, in precedence order."""

    CATALOGUED = "catalogued"
    CATALOGUED_AND_MOVED = "catalogued-and-moved"
    POSITION_STABLE = "position stable within measurement"
    NO_CATALOGUE_MATCH = 'no catalogue match candidate "not confirmed"'
    NO_MATCH_CROWDED = "no match but crowded (weak evidence)"
    AMBIGUOUS = "ambiguous N entries"
    NEIGHBOUR_RESIDUAL = "residual of a neighbour"
    ASTEROID_PREDICTED = "known asteroid predicted (SkyBoT)"
    ASTEROID_BELOW_DEPTH = "asteroid predicted but below depth"
    SATURATED = "saturated"
    TOO_FEW_PIXELS = "too few usable pixels"
    CANNOT_ASSESS = "cannot assess"


class ModeEligibility(_Model):
    """A mode that the data either supports or refuses with a printed reason."""

    enabled: bool
    available: bool | None = None
    reason: str = ""


class Window(_Model):
    lo: float
    hi: float
    s: float
    sha256: str | None = None


class BandWindow(Window):
    """One stretch window per band stack, shared by every frame in that stack."""

    band: int | None = None


class Frame(_Model):
    band: int | None = None
    detector: str | None = None
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


class Visit(_Model):
    """One exposure of the field through one detector: the unit a timeline steps."""

    visit_id: str
    mjd: float | None = None
    date: str | None = None
    release: str | None = None
    survey: int | None = None
    collection: str | None = None
    obs_publisher_did: str | None = None
    detector: str | None = None
    frames: list[Frame] = Field(default_factory=list)


class Timeline(_Model):
    default_band: int | None = None
    default_detector: str | None = None
    visits: int = 0
    span_days: float | None = None
    band_reason: str | None = None


class Epoch(_Model):
    epoch_id: str
    survey: int | None = None
    release: str | None = None
    mjd_min: float | None = None
    mjd_max: float | None = None
    frames: list[Frame] = Field(default_factory=list)
    visit_ids: list[str] = Field(default_factory=list)


class Target(_Model):
    id: str
    ra: float | None = None
    dec: float | None = None
    pm: list[float] | None = None
    stale: bool | None = None
    visits: list[Visit] = Field(default_factory=list)
    epochs: list[Epoch] = Field(default_factory=list)
    timeline: Timeline | None = None
    windows: list[BandWindow] = Field(default_factory=list)
    spectrum: dict[str, Any] | None = None
    labels: list[str] = Field(default_factory=list)

    def band_window(self, band: int | None) -> BandWindow | None:
        """Return the window for ``band``, falling back to the target's first."""
        for window in self.windows:
            if band is not None and window.band == band:
                return window
        return None


class FileEntry(_Model):
    path: str
    bytes: int | None = None
    sha256: str | None = None


class Manifest(_Model):
    schema_version: int
    snapshot_id: str
    generated_at: str | None = None
    fixture: bool = False
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


def is_fixture_manifest(manifest: Manifest) -> bool:
    return bool(
        manifest.fixture
        or manifest.status == "fixture"
        or manifest.snapshot_id.startswith("fixture-")
    )


def _window_values(window: Window | Mapping[str, Any]) -> dict[str, float]:
    if isinstance(window, Window):
        return {"lo": window.lo, "hi": window.hi, "s": window.s}
    try:
        return {
            "lo": float(window["lo"]),
            "hi": float(window["hi"]),
            "s": float(window["s"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError("a stretch window needs numeric lo, hi and s") from exc


def window_sha256(window: Window | Mapping[str, Any]) -> str:
    """Hash the canonical stretch window shared by every plane in a stack."""
    values = _window_values(window)
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def publish_snapshot(
    staging_dir: str | Path,
    snapshot_dir: str | Path,
    *,
    allow_fixture: bool = False,
    keep: int = 4,
) -> SnapshotPointer:
    """Publishes a staging snapshot and updates ``latest.json``."""
    staging = Path(staging_dir)
    snapshots = Path(snapshot_dir)
    manifest_path = staging / "manifest.json"
    try:
        manifest = validate(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(
            f"cannot read staging manifest {manifest_path}: {exc}"
        ) from exc

    if is_fixture_manifest(manifest) and not allow_fixture:
        raise FixtureSnapshotError(
            f"snapshot {manifest.snapshot_id!r} is a fixture; the production "
            "publish path requires allow_fixture=True"
        )
    if ".." in manifest.snapshot_id or not SAFE_SNAPSHOT_ID.fullmatch(
        manifest.snapshot_id
    ):
        raise ManifestError(
            f"unsafe snapshot_id {manifest.snapshot_id!r}; ids must be a single "
            "path segment of letters, digits, dots, dashes and underscores"
        )

    version_root = snapshots / "v1"
    version_root.mkdir(parents=True, exist_ok=True)
    destination = version_root / manifest.snapshot_id
    temporary = version_root / f".{manifest.snapshot_id}.tmp-{os.getpid()}"
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        shutil.copytree(staging, temporary)
        if destination.exists():
            shutil.rmtree(destination)
        temporary.replace(destination)
    except OSError as exc:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise ManifestError(
            f"cannot publish snapshot {manifest.snapshot_id}: {exc}"
        ) from exc

    pointer = SnapshotPointer(
        snapshot_id=manifest.snapshot_id,
        manifest=f"v1/{manifest.snapshot_id}/manifest.json",
        published_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    latest = snapshots / "latest.json"
    temporary_latest = snapshots / f".latest.json.tmp-{os.getpid()}"
    temporary_latest.write_text(pointer.model_dump_json(), encoding="utf-8")
    temporary_latest.replace(latest)

    if keep > 0:
        published = sorted(
            (
                item
                for item in version_root.iterdir()
                if item.is_dir() and not item.name.startswith(".")
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for expired in published[keep:]:
            shutil.rmtree(expired, ignore_errors=True)

    return pointer
