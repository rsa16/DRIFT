"""Generate a test snapshot for the API and renderer.

All IDs start with ``fixture-``, and the manifest sets ``fixture: true`` and
``status: "fixture"``. Must explicitly specify ``allow_fixture=True``.

Target carry a chronological list of visits. Each visit carries one
frame per band, and very band stack gets its own stretch window.
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

from drift.manifest import SnapshotPointer, publish_snapshot, window_sha256
from drift.planes import encode_plane

PLANE_PX = 512
PIXEL_SCALE_ARCSEC = 6.158
FOV_DEG = 0.87
UNITS = "MJy/sr"
NOISE_SIGMA = 0.02
SEED = 1601
MJD_EPOCH = datetime(1858, 11, 17, tzinfo=UTC)

CITATION = (
    "This publication makes use of data products from the Spectro-Photometer "
    "for the History of the Universe, Epoch of Reionization and Ices Explorer "
    "(SPHEREx), which is a joint project of the Jet Propulsion Laboratory and "
    "the California Institute of Technology, and is funded by the National "
    "Aeronautics and Space Administration."
)

# Measured SPHEREx detector wavelength ranges (µm), one dichroic array each.
DETECTOR_RANGES: dict[str, tuple[float, float]] = {
    "D1": (0.75, 1.11),
    "D2": (1.11, 1.64),
    "D3": (1.64, 2.42),
    "D4": (2.42, 3.82),
    "D5": (3.82, 4.42),
    "D6": (4.42, 5.00),
}
BAND_COUNT = 8


def _bands_for(detector: str) -> tuple[tuple[int, float, float], ...]:
    """Split one detector's wavelength range into its synthetic bands.

    A detector fixes the wavelength a position is sampled at, so a fixture frame
    labelled D4 must not report a D1 wavelength.
    """
    low, high = DETECTOR_RANGES[detector]
    width = (high - low) / BAND_COUNT
    return tuple(
        (index + 1, low + width * (index + 0.5), width) for index in range(BAND_COUNT)
    )


SURVEYS: dict[str, int] = {"dr1": 1, "qr3": 3}

BAMBERGA_VISITS: tuple[tuple[str, float], ...] = (
    ("dr1", 61151.568527),
    ("dr1", 61152.856503),
)

LPV_VISITS: tuple[tuple[str, float], ...] = (
    ("dr1", 60790.22),
    ("dr1", 60850.41),
    ("dr1", 60960.05),
    ("dr1", 60982.19),
    ("dr1", 61048.33),
    ("dr1", 61074.33),
    ("dr1", 61113.29),
    ("qr3", 61192.46),
    ("qr3", 61283.76),
)


@dataclass(frozen=True)
class SyntheticTargetSpec:
    id: str
    name: str
    kind: str
    ra: float
    dec: float
    pm: tuple[float, float]
    detector: str
    visits: tuple[tuple[str, float], ...]
    caveats: tuple[str, ...]
    motion_px: float = 0.0
    brightness: float = 0.0
    default_band: int | None = None

    @property
    def releases(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for release, _mjd in self.visits:
            if release not in ordered:
                ordered.append(release)
        return tuple(ordered)


SYNTHETIC_TARGETS: tuple[SyntheticTargetSpec, ...] = (
    SyntheticTargetSpec(
        id="barnard",
        name="Barnard's Star (synthetic)",
        kind="M dwarf",
        ra=269.452075,
        dec=4.693365,
        pm=(-798.71, 10337.77),
        detector="D5",
        visits=(
            ("dr1", 61116.300000),
            ("dr1", 61120.550580),
            ("dr1", 61127.100000),
            ("dr1", 61134.600000),
            ("qr3", 61270.867116),
            ("qr3", 61271.343553),
            ("qr3", 61273.383164),
            ("qr3", 61280.200000),
        ),
        caveats=(
            "Please keep in mind this is a test star field.",
            "Motion below one pixel may not be seen.",
        ),
        motion_px=1.85,
    ),
    SyntheticTargetSpec(
        id="wise0855",
        name="WISE 0855 (synthetic)",
        kind="brown dwarf",
        ra=133.7375,
        dec=-7.2402,
        pm=(-8118.0, 680.0),
        detector="D5",
        visits=(
            ("dr1", 61100.700000),
            ("dr1", 61120.200000),
            ("dr1", 61174.287218),
        ),
        caveats=(
            "Only one synthetic survey epoch exists, so survey compare is refused.",
            "Synthetic star field, not archive pixels.",
        ),
    ),
    SyntheticTargetSpec(
        id="bamberga",
        name="324 Bamberga (synthetic)",
        kind="main-belt asteroid",
        ra=305.2657,
        dec=-29.3597,
        pm=(0.0, 0.0),
        detector="D1",
        visits=BAMBERGA_VISITS,
        caveats=(
            "Synthetic star field, not archive pixels.",
            "Two visits 1.288 d apart; one stamp holds about two days of track.",
        ),
        motion_px=217.3,
    ),
    SyntheticTargetSpec(
        id="lpv",
        name="NEP long-period variable (synthetic)",
        kind="LPV",
        ra=273.55016,
        dec=64.66284,
        pm=(0.0, 0.0),
        detector="D4",
        visits=LPV_VISITS,
        caveats=(
            "Synthetic light curve, not archive pixels.",
            "Nine visits span DR1 and QR3; the change is also shown within DR1 alone.",
        ),
        brightness=0.27,
        # The measured λ-matched sub-band of the real light curve: 3.018–3.063 µm.
        default_band=4,
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


def _base_field(rng: np.random.Generator) -> np.ndarray:
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


def _visit_image(
    spec: SyntheticTargetSpec,
    base: np.ndarray,
    visit_index: int,
    band_index: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Draw one band of one visit: static stars, plus any brightness or motion."""
    gain = 0.85 + 0.08 * band_index
    visits = len(spec.visits)
    fraction = visit_index / max(1, visits - 1)
    change = 1.0 + spec.brightness * (fraction - 0.5)
    image = (base * gain * change).astype(np.float32)
    if spec.motion_px:
        _add_gaussian(
            image,
            x=210.0 + spec.motion_px * fraction,
            y=250.0,
            amplitude=12.0 * gain * change,
            sigma=1.6,
        )
    elif spec.brightness:
        _add_gaussian(
            image,
            x=256.0,
            y=256.0,
            amplitude=12.0 * gain * change,
            sigma=1.8,
        )
    _add_gaussian(image, x=330.0, y=180.0, amplitude=4.0 * gain, sigma=1.4)
    image += rng.normal(0.0, NOISE_SIGMA, size=image.shape).astype(np.float32)
    return np.clip(image, 0.0, 65504.0)


def _isoformat(value: datetime | None) -> str:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=UTC)
    return resolved.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _date_from_mjd(mjd: float) -> str:
    return _isoformat(MJD_EPOCH + timedelta(days=mjd))


def _visit_id(release: str, mjd: float, detector: str) -> str:
    return f"{release}-{mjd:.5f}-{detector}"


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


def _default_band(
    target_arrays: dict[tuple[int, int], np.ndarray], bands: list[int]
) -> int:
    """Choose the band with the most visits, tie-broken by lowest noise, then index."""
    counts: dict[int, int] = {}
    sigmas: dict[int, float] = {}
    for (_visit_index, band), image in target_arrays.items():
        counts[band] = counts.get(band, 0) + 1
        sigmas.setdefault(band, float(np.std(image)))
    return max(bands, key=lambda band: (counts[band], -sigmas[band], -band))


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

        plane_entries: list[dict[str, Any]] = []
        target_arrays: dict[str, dict[tuple[int, int], np.ndarray]] = {}
        frame_index = 0

        bands_by_target: dict[str, tuple[tuple[int, float, float], ...]] = {}
        for target_number, spec in enumerate(SYNTHETIC_TARGETS):
            rng = np.random.default_rng(seed + target_number)
            base = _base_field(rng)
            target_arrays[spec.id] = {}
            bands = _bands_for(spec.detector)
            bands_by_target[spec.id] = bands
            for visit_index, (_release, _mjd) in enumerate(spec.visits):
                for band_index, (band, _lambda_um, _dlambda_um) in enumerate(bands):
                    image = _visit_image(spec, base, visit_index, band_index, rng)
                    target_arrays[spec.id][(visit_index, band)] = image
                    filename = f"{spec.id}_{band:02d}_{visit_index:02d}.spx"
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
        generated_iso = _isoformat(generated)

        targets: list[dict[str, Any]] = []
        windows_by_target: dict[str, list[dict[str, Any]]] = {}
        for spec in SYNTHETIC_TARGETS:
            bands = bands_by_target[spec.id]
            band_ids = [band for band, _lambda, _dlambda in bands]
            arrays = target_arrays[spec.id]
            windows: list[dict[str, Any]] = []
            for band, _lambda_um, _dlambda_um in bands:
                stack = [
                    arrays[(visit_index, band)]
                    for visit_index in range(len(spec.visits))
                    if (visit_index, band) in arrays
                ]
                window = _stretch_window(stack)
                window["band"] = band
                windows.append(window)
            windows_by_target[spec.id] = windows

            visits: list[dict[str, Any]] = []
            for visit_index, (release, mjd) in enumerate(spec.visits):
                frames: list[dict[str, Any]] = []
                for band, lambda_um, dlambda_um in bands:
                    image = arrays[(visit_index, band)]
                    filename = f"{spec.id}_{band:02d}_{visit_index:02d}.spx"
                    blob = (planes_dir / filename).read_bytes()
                    frames.append(
                        {
                            "band": band,
                            "detector": spec.detector,
                            "lambda_um": lambda_um,
                            "dlambda_um": dlambda_um,
                            "obs_publisher_did": (
                                f"fixture://{spec.id}/{release}/SPHEREx-{spec.detector}"
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
                visits.append(
                    {
                        "visit_id": _visit_id(release, mjd, spec.detector),
                        "mjd": mjd,
                        "date": _date_from_mjd(mjd),
                        "release": release,
                        "survey": SURVEYS[release],
                        "collection": f"fixture_{release}",
                        "obs_publisher_did": (
                            f"fixture://{spec.id}/{release}/SPHEREx-{spec.detector}"
                        ),
                        "detector": spec.detector,
                        "frames": frames,
                    }
                )

            epochs: list[dict[str, Any]] = []
            for release in spec.releases:
                members = [visit for visit in visits if visit["release"] == release]
                epochs.append(
                    {
                        "epoch_id": f"fixture-{release}",
                        "survey": SURVEYS[release],
                        "release": release,
                        "mjd_min": min(visit["mjd"] for visit in members),
                        "mjd_max": max(visit["mjd"] for visit in members),
                        "visit_ids": [visit["visit_id"] for visit in members],
                        # A derived grouping keeps one representative frame per
                        # band so the secondary survey compare still works.
                        "frames": json.loads(json.dumps(members[-1]["frames"])),
                    }
                )

            default_band = spec.default_band or _default_band(arrays, band_ids)
            mjds = [visit["mjd"] for visit in visits]
            targets.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "kind": spec.kind,
                    "ra": spec.ra,
                    "dec": spec.dec,
                    "pm": [spec.pm[0], spec.pm[1]],
                    "stale": False,
                    "visits": visits,
                    "epochs": epochs,
                    "timeline": {
                        "default_band": default_band,
                        "default_detector": spec.detector,
                        "visits": len(visits),
                        "span_days": max(mjds) - min(mjds),
                    },
                    "windows": windows,
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

        default_window = dict(windows_by_target[SYNTHETIC_TARGETS[0].id][0])
        default_window.pop("band", None)

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
                "plane_px": PLANE_PX,
                "fov_deg": FOV_DEG,
                "pixel_scale_arcsec": PIXEL_SCALE_ARCSEC,
                "units": UNITS,
                "dtype": "float16",
                "reprojector": "synthetic-fixture",
                "band_bins": BAND_COUNT,
                "blink_bins": 8,
                "timeline_visits": 16,
                "default_visits": 8,
                "stretch": "lo=median-3sigma; hi=p99.9; s=3sigma",
            },
            "status": "fixture",
            "source": {
                "service": "synthetic fixture",
                "collections": sorted(
                    {
                        f"fixture_{release}"
                        for spec in SYNTHETIC_TARGETS
                        for release in spec.releases
                    }
                ),
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
            "window": default_window,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return publish_snapshot(staging, snapshots, allow_fixture=True)
