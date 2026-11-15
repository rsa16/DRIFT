"""Generate a test snapshot for the API and renderer.

All IDs start with ``fixture-``, and the manifest sets ``fixture: true`` and
``status: "fixture"``. Must explicitly specify ``allow_fixture=True``.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from drift.manifest import SnapshotPointer, publish_snapshot, window_sha256
from drift.planes import encode_plane

PLANE_PX = 512
PIXEL_SCALE_ARCSEC = 6.158
FOV_DEG = 0.87
UNITS = "MJy/sr"
NOISE_SIGMA = 0.02
SEED = 1601

CITATION = (
    "This publication makes use of data products from the Spectro-Photometer "
    "for the History of the Universe, Epoch of Reionization and Ices Explorer "
    "(SPHEREx), which is a joint project of the Jet Propulsion Laboratory and "
    "the California Institute of Technology, and is funded by the National "
    "Aeronautics and Space Administration."
)

# (band, lambda_um, dlambda_um)
BANDS: tuple[tuple[int, float, float], ...] = (
    (1, 1.10, 0.05),
    (2, 1.59, 0.05),
    (3, 2.07, 0.05),
    (4, 2.56, 0.05),
    (5, 3.05, 0.05),
    (6, 3.53, 0.05),
    (7, 4.02, 0.05),
    (8, 4.50, 0.05),
)

EPOCHS: dict[str, dict[str, float | int]] = {
    "dr1": {"survey": 1, "mjd": 61120.550580},
    "qr3": {"survey": 2, "mjd": 61273.383164},
}


@dataclass(frozen=True)
class SyntheticTargetSpec:
    id: str
    name: str
    kind: str
    ra: float
    dec: float
    pm: tuple[float, float]
    releases: tuple[str, ...]
    caveats: tuple[str, ...]
    moving: bool


SYNTHETIC_TARGETS: tuple[SyntheticTargetSpec, ...] = (
    SyntheticTargetSpec(
        id="barnard",
        name="Barnard's Star (synthetic)",
        kind="M dwarf",
        ra=269.452075,
        dec=4.693365,
        pm=(-798.71, 10337.77),
        releases=("dr1", "qr3"),
        caveats=(
            "Synthetic star field, not archive pixels.",
            "Motion below one pixel is measured, not seen.",
        ),
        moving=True,
    ),
    SyntheticTargetSpec(
        id="wise0855",
        name="WISE 0855 (synthetic)",
        kind="brown dwarf",
        ra=133.7375,
        dec=-7.2402,
        pm=(-8118.0, 680.0),
        releases=("dr1",),
        caveats=(
            "Only one synthetic epoch exists, so epoch comparison is refused.",
            "Synthetic star field, not archive pixels.",
        ),
        moving=False,
    ),
)


def _add_gaussian(
    image: np.ndarray, x: float, y: float, amplitude: float, sigma: float
) -> None:
    height, width = image.shape
    half = max(1, int(math.ceil(5.0 * sigma)))
    x0, x1 = max(0, int(x) - half), min(width, int(x) + half + 1)
    y0, y1 = max(0, int(y) - half), min(height, int(y) + half + 1)
    if x0 >= x1 or y0 >= y1:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1]
    profile = np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2.0 * sigma * sigma))
    image[y0:y1, x0:x1] += np.asarray(amplitude * profile, dtype=np.float32)


def _base_field(spec: SyntheticTargetSpec, rng: np.random.Generator) -> np.ndarray:
    image = np.full((PLANE_PX, PLANE_PX), 1.0, dtype=np.float32)
    for _ in range(32):
        _add_gaussian(
            image,
            x=float(rng.uniform(8, PLANE_PX - 8)),
            y=float(rng.uniform(8, PLANE_PX - 8)),
            amplitude=float(rng.uniform(2.0, 14.0)),
            sigma=float(rng.uniform(1.0, 2.2)),
        )
    return image


def _band_image(
    spec: SyntheticTargetSpec,
    base: np.ndarray,
    release: str,
    band_index: int,
    rng: np.random.Generator,
) -> np.ndarray:
    gain = 0.85 + 0.08 * band_index
    image = (base * gain).astype(np.float32)
    if spec.moving:
        second = release == spec.releases[-1] and len(spec.releases) > 1
        x = 238.0 if second else 210.0
        _add_gaussian(image, x=x, y=250.0, amplitude=12.0 * gain, sigma=1.6)
        _add_gaussian(
            image,
            x=330.0,
            y=180.0,
            amplitude=(4.0 if second else 12.0) * gain,
            sigma=1.4,
        )
    image += rng.normal(0.0, NOISE_SIGMA, size=image.shape).astype(np.float32)
    return np.clip(image, 0.0, 65504.0)


def _isoformat(value: datetime | None) -> str:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=UTC)
    return resolved.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _stretch_window(arrays: list[np.ndarray]) -> dict[str, Any]:
    values = np.concatenate([array.reshape(-1) for array in arrays])
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


def build_synthetic_snapshot(
    snapshot_dir: str | Path,
    *,
    generated_at: datetime | str | None = None,
    seed: int = SEED,
) -> SnapshotPointer:
    """Write a synthetic schema-v1 snapshot and publish it."""
    snapshots = Path(snapshot_dir)
    snapshots.mkdir(parents=True, exist_ok=True)
    if isinstance(generated_at, str):
        generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    if generated_at is not None and generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    generated = generated_at.astimezone(UTC) if generated_at else datetime.now(UTC)
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")

    with tempfile.TemporaryDirectory(prefix=".fixture-", dir=snapshots) as temporary:
        staging = Path(temporary)
        planes_dir = staging / "planes"
        planes_dir.mkdir()

        arrays: list[np.ndarray] = []
        plane_entries: list[dict[str, Any]] = []
        target_arrays: dict[str, dict[tuple[str, int], np.ndarray]] = {}
        frame_index = 0

        for target_number, spec in enumerate(SYNTHETIC_TARGETS):
            rng = np.random.default_rng(seed + target_number)
            base = _base_field(spec, rng)
            target_arrays[spec.id] = {}
            for release in spec.releases:
                for band_index, (band, _lambda_um, _dlambda_um) in enumerate(BANDS):
                    image = _band_image(spec, base, release, band_index, rng)
                    arrays.append(image)
                    target_arrays[spec.id][(release, band)] = image
                    filename = f"{spec.id}_{band:02d}_{release}.spx"
                    blob = encode_plane(
                        image,
                        channels=band,
                        frame_index=frame_index,
                        scale=1.0,
                        offset=0.0,
                    )
                    (planes_dir / filename).write_bytes(blob)
                    plane_entries.append(
                        {
                            "path": f"v1/__SNAPSHOT__/planes/{filename}",
                            "filename": filename,
                            "bytes": len(blob),
                            "sha256": hashlib.sha256(blob).hexdigest(),
                        }
                    )
                    frame_index += 1

        digest = hashlib.sha256()
        for entry in sorted(plane_entries, key=lambda item: item["filename"]):
            name = str(entry["filename"])
            digest.update(name.encode("utf-8"))
            digest.update((planes_dir / name).read_bytes())
        snapshot_id = f"fixture-{stamp}-{digest.hexdigest()[:8]}"

        window = _stretch_window(arrays)
        generated_iso = _isoformat(generated)

        targets: list[dict[str, Any]] = []
        for spec in SYNTHETIC_TARGETS:
            epochs: list[dict[str, Any]] = []
            for release in spec.releases:
                epoch_meta = EPOCHS[release]
                frames: list[dict[str, Any]] = []
                for band, lambda_um, dlambda_um in BANDS:
                    image = target_arrays[spec.id][(release, band)]
                    filename = f"{spec.id}_{band:02d}_{release}.spx"
                    blob = (planes_dir / filename).read_bytes()
                    frames.append(
                        {
                            "band": band,
                            "lambda_um": lambda_um,
                            "dlambda_um": dlambda_um,
                            "obs_publisher_did": (
                                f"fixture://{spec.id}/{release}/SPHEREx-D{band}"
                            ),
                            "source_url": None,
                            "href": f"v1/{snapshot_id}/planes/{filename}",
                            "bytes": len(blob),
                            "sha256": hashlib.sha256(blob).hexdigest(),
                            "sigma": float(np.std(image)),
                            "flag_bad_frac": 0.0,
                            "saturated": False,
                        }
                    )
                epochs.append(
                    {
                        "epoch_id": f"fixture-{release}",
                        "survey": int(epoch_meta["survey"]),
                        "release": release,
                        "mjd_min": float(epoch_meta["mjd"]),
                        "mjd_max": float(epoch_meta["mjd"]),
                        "frames": frames,
                    }
                )
            targets.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "kind": spec.kind,
                    "ra": spec.ra,
                    "dec": spec.dec,
                    "pm": [spec.pm[0], spec.pm[1]],
                    "stale": False,
                    "epochs": epochs,
                    "spectrum": None,
                    "labels": [],
                    "caveats": list(spec.caveats),
                }
            )

        files = [
            {
                "path": str(entry["path"]).replace("__SNAPSHOT__", snapshot_id),
                "bytes": entry["bytes"],
                "sha256": entry["sha256"],
            }
            for entry in plane_entries
        ]

        manifest: dict[str, Any] = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "generated_at": generated_iso,
            "fixture": True,
            "generator": {
                "name": "spherex-epoch-viewer",
                "version": "0.1.0",
                "commit": "fixture",
            },
            "processing": {
                "epoch_gap_days": 153,
                "plane_px": PLANE_PX,
                "fov_deg": FOV_DEG,
                "pixel_scale_arcsec": PIXEL_SCALE_ARCSEC,
                "units": UNITS,
                "dtype": "float16",
                "reprojector": "synthetic-fixture",
                "band_bins": len(BANDS),
                "blink_bins": 8,
                "stretch": "lo=median-3sigma; hi=p99.9; s=3sigma",
            },
            "status": "fixture",
            "source": {
                "service": "synthetic fixture",
                "collections": ["fixture_dr1", "fixture_qr3"],
                "queried_at": generated_iso,
                "row_counts": {},
            },
            "watermark": {
                "prev_snapshot_id": None,
                "rows_sha256": digest.hexdigest(),
            },
            "targets": targets,
            "files": files,
            "alerts": [
                {
                    "level": "info",
                    "message": "Synthetic fixture snapshot - not archive data.",
                }
            ],
            "citation": CITATION,
            "window": window,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return publish_snapshot(staging, snapshots, allow_fixture=True)
