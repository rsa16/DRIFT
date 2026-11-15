"""Record and replay fixture data.

Dev-tooling only, snapshot builds shouldn't be using this.
IRSA 503s a LOT, so easier to cache with this.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import logging
import tempfile
import time
from datetime import UTC, datetime
from math import cos, isclose, radians
from pathlib import Path
from typing import Any

from drift.archive import (
    ArchiveError,
    ByteFetcher,
    Collection,
    CollectionDiscovery,
    CutoutFetcher,
    HttpResponse,
    RetryPolicy,
    Sia2Client,
    Transport,
    UrllibTransport,
    fits_hdu_names,
    parse_sia_csv,
)
from drift.ingest.fixture import FixtureError, FixtureStore, FixtureTransport
from drift.ingest.targets import (
    CUBE_APERTURE,
    CUBE_CENTER,
    CUBE_URL,
    FIXTURE_CUTOUTS,
    FIXTURE_SEARCHES,
    SEED_COLLECTIONS,
    TARGETS,
)


class RecordingError(RuntimeError):
    """The live archive could not supply a faithful fixture."""


class _OfflineTransport:
    """Guard for local fixture checks: a network attempt here is a bug."""

    def get(self, url: str, timeout: float) -> HttpResponse:
        raise RecordingError(f"local fixture check tried to fetch {url}")


def _propagate(
    ra: float, dec: float, pm_ra_mas: float, pm_dec_mas: float, mjd: float
) -> tuple[float, float]:
    years = (mjd - 51544.5) / 365.25
    ra_deg = ra + (pm_ra_mas * years / 3_600_000.0) / max(1e-6, abs(cos(radians(dec))))
    dec_deg = dec + pm_dec_mas * years / 3_600_000.0
    return ra_deg, dec_deg


def select_row(
    rows: list[dict[str, str]], collection: str, band: str, mjd: float
) -> dict[str, str]:
    """Return the row closest in time for a collection and band."""
    candidates = [
        row
        for row in rows
        if row.get("obs_collection") == collection
        and row.get("energy_bandpassname") == f"SPHEREx-{band}"
        and row.get("access_url")
    ]
    scored: list[tuple[float, dict[str, str]]] = []
    for row in candidates:
        try:
            observed = float(row["t_min"])
        except (KeyError, ValueError):
            continue
        scored.append((abs(observed - mjd), row))
    if not scored:
        raise RecordingError(
            f"no usable {band} row in {collection} for the requested epoch"
        )
    return min(scored, key=lambda item: item[0])[1]


def sample_rows(
    rows: list[dict[str, str]], max_rows: int | None
) -> list[dict[str, str]]:
    """Thin rows evenly to ``max_rows``, always keeping the first and last."""
    if max_rows is None or len(rows) <= max_rows:
        return rows
    if max_rows <= 1:
        return rows[:1]
    step = (len(rows) - 1) / (max_rows - 1)
    return [rows[round(index * step)] for index in range(max_rows)]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _write_collections(root: Path, collections: list[Collection] | None = None) -> None:
    path = root / "collections.csv"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["collection", "release_date"])
    rows = (
        [(item.name, item.release_date or "") for item in collections]
        if collections is not None
        else list(SEED_COLLECTIONS)
    )
    if not any(name == "spherex_qr9" for name, _ in rows):
        rows.append(("spherex_qr9", "2027-01-15"))
    writer.writerows(rows)
    _atomic_write(path, buffer.getvalue().encode("utf-8"))


def _record_collections(
    root: Path, *, transport: Transport, policy: RetryPolicy
) -> None:
    try:
        collections = CollectionDiscovery(transport=transport, policy=policy).list()
        print(f"record: discovered {len(collections)} spherex collections")
    except ArchiveError as exc:
        print(f"record: collection discovery unavailable ({exc}); using seed list")
        collections = None
    _write_collections(root, collections)


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    """Write SIA2 rows as CSV in the same shape the archive returns."""
    if not rows:
        _atomic_write(path, b"")
        return
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(rows[0].keys()), lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write(path, buffer.getvalue().encode("utf-8"))


def fixture_complete(root: Path) -> bool:
    """True when the fixture at ``root`` matches the recording plan and hashes."""
    try:
        store = FixtureStore(root)
        if store.index.get("schema_version") != 1:
            return False
        collections = {item.name: item.release_date for item in store.collections()}
        required_collections = {
            name: release
            for name, release in SEED_COLLECTIONS
            if name.startswith("spherex_")
        }
        for name, release in required_collections.items():
            if collections.get(name) != release:
                return False
        if "spherex_qr9" not in collections:
            return False

        client = Sia2Client(transport=_OfflineTransport())
        fetcher = CutoutFetcher(transport=_OfflineTransport())

        searches = store.index.get("searches")
        cutouts = store.index.get("cutouts")
        if not isinstance(searches, list) or len(searches) != len(FIXTURE_SEARCHES):
            return False
        if not isinstance(cutouts, list) or len(cutouts) != len(FIXTURE_CUTOUTS):
            return False

        by_search_path = {
            str(entry["path"]): entry for entry in searches if "path" in entry
        }
        for spec in FIXTURE_SEARCHES:
            relative = f"sia/{spec.filename}"
            entry = by_search_path.get(relative)
            if not isinstance(entry, dict):
                return False
            if (
                entry.get("target") != spec.target
                or entry.get("collection") != spec.collection
            ):
                return False
            body = store.response(str(entry["url"]))
            if entry.get("sha256") != hashlib.sha256(body).hexdigest():
                return False
            rows = parse_sia_csv(body)
            if len(rows) != entry.get("row_count"):
                return False
            ra, dec = TARGETS[spec.target][:2]
            pos = entry.get("pos")
            if not isinstance(pos, list) or len(pos) != 2:
                return False
            if not (
                isclose(float(pos[0]), ra, rel_tol=0, abs_tol=1e-9)
                and isclose(float(pos[1]), dec, rel_tol=0, abs_tol=1e-9)
            ):
                return False
            expected_url = client.search_url(
                spec.collection, pos=(ra, dec), size_deg=0.01
            )
            if entry.get("url") != expected_url:
                return False
            if not isclose(
                float(entry.get("size_deg", -1)), 0.01, rel_tol=0, abs_tol=1e-12
            ):
                return False

        by_cutout_path = {
            str(entry["path"]): entry for entry in cutouts if "path" in entry
        }
        for cutout_spec in FIXTURE_CUTOUTS:
            relative = f"cutouts/{cutout_spec.filename}"
            entry = by_cutout_path.get(relative)
            if not isinstance(entry, dict):
                return False
            if (
                entry.get("target") != cutout_spec.target
                or entry.get("collection") != cutout_spec.collection
                or entry.get("band") != cutout_spec.band
                or entry.get("strip_epsf") is not True
            ):
                return False
            body = store.cutout(str(entry["url"]))
            if entry.get("sha256") != hashlib.sha256(body).hexdigest():
                return False
            names = fits_hdu_names(body)
            if "IMAGE" not in names or "EPSF" in names:
                return False
            center = entry.get("center")
            if not isinstance(center, list) or len(center) != 2:
                return False
            if cutout_spec.center is not None:
                expected = cutout_spec.center
            else:
                j2000_ra, j2000_dec, pm_ra, pm_dec = TARGETS[cutout_spec.target]
                expected = _propagate(
                    j2000_ra,
                    j2000_dec,
                    pm_ra,
                    pm_dec,
                    float(entry["mjd"]),
                )
            if not (
                isclose(float(center[0]), expected[0], rel_tol=0, abs_tol=1e-9)
                and isclose(float(center[1]), expected[1], rel_tol=0, abs_tol=1e-9)
            ):
                return False
            expected_cutout_url = fetcher.cutout_url(
                str(entry["access_url"]),
                center=(float(center[0]), float(center[1])),
                size_deg=cutout_spec.size_deg,
            )
            if entry.get("url") != expected_cutout_url:
                return False
            if not isclose(
                float(entry.get("size_deg", -1)),
                cutout_spec.size_deg,
                rel_tol=0,
                abs_tol=1e-12,
            ):
                return False

        cube = store.index.get("cube_slice")
        if not isinstance(cube, dict) or not cube.get("path"):
            return False
        cube_path = root / str(cube["path"])
        cube_bytes = cube_path.read_bytes()
        if cube.get("sha256") != hashlib.sha256(cube_bytes).hexdigest():
            return False
        cube_names = fits_hdu_names(cube_bytes)
        if "IMAGE" not in cube_names or "SPECTRAL_CHANNELS" not in cube_names:
            return False
    except (ArchiveError, ValueError, KeyError, TypeError, OSError):
        return False
    return True


def _record_cube_slice(
    root: Path, *, force: bool, transport: Transport | None = None
) -> dict[str, Any]:
    destination = root / "cube" / "lpv_cube_slice.fits"
    if destination.is_file() and not force:
        return {
            "path": "cube/lpv_cube_slice.fits",
            "url": CUBE_URL,
            "ra": CUBE_CENTER[0],
            "dec": CUBE_CENTER[1],
            "aperture": CUBE_APERTURE,
            "bytes": destination.stat().st_size,
            "sha256": _sha256_file(destination),
        }

    try:
        fits: Any = importlib.import_module("astropy.io.fits")
        wcs_module: Any = importlib.import_module("astropy.wcs")
        coordinates: Any = importlib.import_module("astropy.coordinates")
        units: Any = importlib.import_module("astropy.units")
    except ImportError as exc:  # pragma: no cover - build-time dependency
        raise RecordingError(
            "recording the cube slice needs astropy; run "
            "`uv run --with astropy python -m drift.ingest record "
            f"--fixture {root} --force`"
        ) from exc

    body = ByteFetcher(
        transport=transport or UrllibTransport(),
        policy=RetryPolicy(timeout_s=120.0),
    ).fetch(CUBE_URL)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "cube.fits"
        source.write_bytes(body)
        with fits.open(source, memmap=True) as hdul:
            image_hdu = hdul["IMAGE"]
            celestial = wcs_module.WCS(image_hdu.header, hdul).celestial
            center = coordinates.SkyCoord(
                CUBE_CENTER[0] * units.deg,
                CUBE_CENTER[1] * units.deg,
                frame="icrs",
            )
            x, y = celestial.world_to_pixel(center)
            x0 = int(round(float(x))) - CUBE_APERTURE // 2
            y0 = int(round(float(y))) - CUBE_APERTURE // 2
            sl = slice(y0, y0 + CUBE_APERTURE), slice(x0, x0 + CUBE_APERTURE)

            outputs = []
            for name in ("IMAGE", "NHITS", "FLAGS"):
                hdu = hdul[name]
                data = hdu.data[:, sl[0], sl[1]]
                header = hdu.header.copy()
                header["CRPIX1"] = float(header["CRPIX1"]) - x0
                header["CRPIX2"] = float(header["CRPIX2"]) - y0
                header["SLICE_X0"] = x0
                header["SLICE_Y0"] = y0
                header["SLICE_RA"] = (CUBE_CENTER[0], "slice centre RA [deg]")
                header["SLICEDC"] = (CUBE_CENTER[1], "slice centre Dec [deg]")
                outputs.append(fits.ImageHDU(data=data, header=header, name=name))

            spectral = hdul["SPECTRAL_CHANNELS"]
            wave = hdul["WCS-WAVE"]
            final = fits.HDUList(
                [
                    fits.PrimaryHDU(header=hdul[0].header),
                    *outputs,
                    fits.BinTableHDU(
                        data=spectral.data,
                        header=spectral.header.copy(),
                        name="SPECTRAL_CHANNELS",
                    ),
                    fits.BinTableHDU(
                        data=wave.data,
                        header=wave.header.copy(),
                        name="WCS-WAVE",
                    ),
                ]
            )
            final.writeto(destination, overwrite=True)

    return {
        "path": "cube/lpv_cube_slice.fits",
        "url": CUBE_URL,
        "ra": CUBE_CENTER[0],
        "dec": CUBE_CENTER[1],
        "aperture": CUBE_APERTURE,
        "bytes": destination.stat().st_size,
        "sha256": _sha256_file(destination),
    }


def record(
    fixture: str | Path,
    *,
    force: bool = False,
    transport: Transport | None = None,
) -> int:
    """Record the archive fixture under ``fixture``, or do nothing if complete."""
    root = Path(fixture)
    if not force and fixture_complete(root):
        print(f"record: fixture already complete at {root}; no network calls")
        return 0

    root.mkdir(parents=True, exist_ok=True)
    (root / "sia").mkdir(exist_ok=True)
    (root / "cutouts").mkdir(exist_ok=True)
    (root / "cube").mkdir(exist_ok=True)

    transport = transport or UrllibTransport()
    policy = RetryPolicy()
    client = Sia2Client(transport=transport, policy=policy)
    fetcher = CutoutFetcher(transport=transport, policy=policy)

    previous: dict[str, dict[str, Any]] = {}
    index_path = root / "index.json"
    if index_path.is_file():
        try:
            old_index = json.loads(index_path.read_text(encoding="utf-8"))
            previous = {
                str(entry["path"]): entry for entry in old_index.get("searches", [])
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            previous = {}

    rows_by_file: dict[str, list[dict[str, str]]] = {}
    searches: list[dict[str, Any]] = []
    for spec in FIXTURE_SEARCHES:
        ra, dec = TARGETS[spec.target][:2]
        path = root / "sia" / spec.filename
        archive_rows: int | None = None
        if not force and path.is_file():
            rows = parse_sia_csv(path.read_bytes())
            archive_rows = previous.get(f"sia/{spec.filename}", {}).get(
                "archive_row_count"
            )
        else:
            body = client.search_body(spec.collection, pos=(ra, dec), size_deg=0.01)
            rows = parse_sia_csv(body)
            archive_rows = len(rows)
            rows = sample_rows(rows, spec.max_rows)
            if len(rows) != archive_rows:
                write_rows(path, rows)
            else:
                _atomic_write(path, body)
        rows_by_file[spec.filename] = rows
        entry: dict[str, Any] = {
            "target": spec.target,
            "collection": spec.collection,
            "pos": [ra, dec],
            "size_deg": 0.01,
            "url": client.search_url(spec.collection, pos=(ra, dec), size_deg=0.01),
            "path": f"sia/{spec.filename}",
            "row_count": len(rows),
            "sampled": spec.max_rows is not None,
            "archive_row_count": archive_rows,
            "sha256": _sha256_file(path),
        }
        searches.append(entry)
        print(
            f"record: {spec.target}/{spec.collection}: {len(rows)} rows"
            + (
                f" (sampled from {archive_rows})"
                if archive_rows is not None and archive_rows != len(rows)
                else ""
            )
        )

    cutouts: list[dict[str, Any]] = []
    all_rows = [row for rows in rows_by_file.values() for row in rows]
    for cutout_spec in FIXTURE_CUTOUTS:
        time.sleep(2.0)
        if cutout_spec.center is not None:
            center = cutout_spec.center
        else:
            j2000_ra, j2000_dec, pm_ra, pm_dec = TARGETS[cutout_spec.target]
            center = _propagate(
                j2000_ra,
                j2000_dec,
                pm_ra,
                pm_dec,
                cutout_spec.target_mjd,
            )
        row = select_row(
            all_rows,
            cutout_spec.collection,
            cutout_spec.band,
            cutout_spec.target_mjd,
        )
        path = root / "cutouts" / cutout_spec.filename
        if not force and path.is_file():
            body = path.read_bytes()
        else:
            body = fetcher.fetch(
                row["access_url"],
                center=center,
                size_deg=cutout_spec.size_deg,
                strip_epsf=True,
            )
            _atomic_write(path, body)
        cutouts.append(
            {
                "target": cutout_spec.target,
                "collection": cutout_spec.collection,
                "band": cutout_spec.band,
                "mjd": float(row["t_min"]),
                "obs_publisher_did": row["obs_publisher_did"],
                "access_url": row["access_url"],
                "center": [center[0], center[1]],
                "size_deg": cutout_spec.size_deg,
                "url": fetcher.cutout_url(
                    row["access_url"], center=center, size_deg=cutout_spec.size_deg
                ),
                "path": f"cutouts/{cutout_spec.filename}",
                "bytes": len(body),
                "strip_epsf": True,
                "sha256": _sha256_file(path),
            }
        )
        print(f"record: cutout {cutout_spec.filename}: {len(body)} bytes")

    cube = _record_cube_slice(root, force=force, transport=transport)
    _record_collections(root, transport=transport, policy=policy)

    index = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "collections": "collections.csv",
        "searches": searches,
        "cutouts": cutouts,
        "cube_slice": cube,
    }
    _atomic_write(
        index_path,
        (json.dumps(index, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(
        f"record: wrote {len(searches)} searches and {len(cutouts)} cutouts to {root}"
    )
    return 0


def replay(fixture: str | Path) -> int:
    """Replay the fixture at ``fixture`` through the real clients, offline."""
    root = Path(fixture)
    store = FixtureStore(root)
    transport = FixtureTransport(store)
    quiet = logging.getLogger("drift.fixture")
    quiet.disabled = True
    client = Sia2Client(transport=transport, log=quiet)
    fetcher = CutoutFetcher(transport=transport, log=quiet)
    discovery = CollectionDiscovery(transport=transport, log=quiet)

    collections = discovery.list()
    total_rows = 0
    for entry in store.index["searches"]:
        body = store.response(entry["url"])
        if entry.get("sha256") != hashlib.sha256(body).hexdigest():
            raise FixtureError(f"search hash mismatch for {entry['path']}")
        rows = client.search(
            entry["collection"],
            pos=tuple(entry["pos"]),
            size_deg=entry["size_deg"],
        )
        if len(rows) != entry["row_count"]:
            raise FixtureError(
                f"row count mismatch for {entry['path']}: "
                f"{len(rows)} != {entry['row_count']}"
            )
        total_rows += len(rows)

    for entry in store.index["cutouts"]:
        recorded = store.cutout(entry["url"])
        if entry.get("sha256") != hashlib.sha256(recorded).hexdigest():
            raise FixtureError(f"cutout hash mismatch for {entry['path']}")
        replay_body = fetcher.fetch(
            entry["access_url"],
            center=tuple(entry["center"]),
            size_deg=entry["size_deg"],
            strip_epsf=True,
        )
        if replay_body != recorded:
            raise FixtureError(f"cutout replay differs for {entry['path']}")

    cube = store.index["cube_slice"]
    cube_path = root / cube["path"]
    cube_bytes = cube_path.read_bytes()
    if cube.get("sha256") != hashlib.sha256(cube_bytes).hexdigest():
        raise FixtureError("cube slice hash mismatch")
    names = fits_hdu_names(cube_bytes)

    print(
        f"replay: {len(collections)} collections, "
        f"{len(store.index['searches'])} searches / {total_rows} rows, "
        f"{len(store.index['cutouts'])} cutouts, cube [{', '.join(names)}]"
    )
    return 0
