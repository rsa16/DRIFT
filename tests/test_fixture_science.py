from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.coordinates import SkyCoord  # type: ignore[import-untyped]
from astropy.io import fits  # type: ignore[import-untyped]
from astropy.wcs import WCS  # type: ignore[import-untyped]

from drift.ingest.fixture import FixtureStore
from drift.ingest.targets import TARGETS

FIXTURE = Path(__file__).parent / "fixtures" / "archive"
SIP_KEYS = ("A_", "B_", "AP_", "BP_")
OUTPUT_STAMP = 512


def _strip_sip(header: fits.Header) -> fits.Header:
    stripped = header.copy()
    for key in list(stripped):
        if key.startswith(SIP_KEYS):
            del stripped[key]
    return stripped


def _barnard_cutouts() -> list[dict[str, Any]]:
    store = FixtureStore(FIXTURE)
    cutouts = [
        entry for entry in store.index["cutouts"] if entry.get("target") == "barnard"
    ]
    assert len(cutouts) >= 4
    return cutouts


def _centred_pixels(
    shape: int, data_shape: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    ny, nx = data_shape
    yy, xx = np.mgrid[0:shape, 0:shape]
    xx = xx.astype(float) - (shape - 1) / 2 + (nx - 1) / 2
    yy = yy.astype(float) - (shape - 1) / 2 + (ny - 1) / 2
    return xx, yy


def test_recorded_barnard_cutouts_reproduce_the_sip_ranges() -> None:
    per_collection: dict[str, dict[str, list[float]]] = {}
    for entry in _barnard_cutouts():
        collection = str(entry["collection"])
        with fits.open(FIXTURE / entry["path"]) as hdul:
            header = hdul["IMAGE"].header
            assert header["CTYPE1"] == "RA---TAN-SIP"
            assert header["CTYPE2"] == "DEC--TAN-SIP"

            wave_on = WCS(header, fobj=hdul, key="W")
            wave_off = WCS(_strip_sip(header), fobj=hdul, key="W")
            ny, nx = hdul["IMAGE"].data.shape
            x, y = np.meshgrid(np.arange(nx), np.arange(ny))
            sip_shift_nm = (
                wave_on.pixel_to_world_values(x, y)[0]
                - wave_off.pixel_to_world_values(x, y)[0]
            ) * 1_000.0

            sky_on = WCS(header, fobj=hdul).celestial
            sky_off = WCS(_strip_sip(header), fobj=hdul).celestial
            xx, yy = _centred_pixels(OUTPUT_STAMP, (ny, nx))
            ra_on, dec_on = sky_on.all_pix2world(xx, yy, 0)
            ra_off, dec_off = sky_off.all_pix2world(xx, yy, 0)
            displacement = (
                SkyCoord(ra_on, dec_on, unit="deg")
                .separation(SkyCoord(ra_off, dec_off, unit="deg"))
                .arcsec
            )

        ranges = per_collection.setdefault(
            collection, {"shift_nm": [], "displacement_arcsec": []}
        )
        ranges["shift_nm"].append(float(np.nanmax(np.abs(sip_shift_nm))))
        ranges["displacement_arcsec"].append(float(np.nanmax(displacement)))

    assert set(per_collection) == {"spherex_dr1", "spherex_qr3"}
    for collection, ranges in per_collection.items():
        assert len(ranges["shift_nm"]) >= 2, collection
        shift = max(ranges["shift_nm"])
        displacement = max(ranges["displacement_arcsec"])
        assert 5.0 <= shift <= 10.0, (collection, shift)
        assert 60.0 <= displacement <= 124.0, (collection, displacement)


def test_recorded_barnard_cutouts_have_shared_two_channel_bins() -> None:
    ra, dec, _, _ = TARGETS["barnard"]
    samples: list[tuple[str, float, float, str]] = []
    for entry in _barnard_cutouts():
        with fits.open(FIXTURE / entry["path"]) as hdul:
            stripped = _strip_sip(hdul["IMAGE"].header)
            wave = WCS(stripped, fobj=hdul, key="W")
            sky = WCS(stripped, fobj=hdul).celestial
            x, y = sky.all_world2pix([ra], [dec], 0)
            wavelength, bandwidth = wave.pixel_to_world_values(x[0], y[0])
        samples.append(
            (
                str(entry["collection"]),
                float(wavelength),
                float(bandwidth),
                str(entry["obs_publisher_did"]),
            )
        )

    assert len({sample[3] for sample in samples}) == len(samples)
    assert {sample[0] for sample in samples} == {"spherex_dr1", "spherex_qr3"}
    for collection in ("spherex_dr1", "spherex_qr3"):
        assert sum(sample[0] == collection for sample in samples) >= 2

    binwidth = 2.0 * float(np.median([sample[2] for sample in samples]))
    first = min(sample[1] for sample in samples)
    edge = first - binwidth / 2.0
    groups: dict[int, dict[str, list[float]]] = {}
    for collection, wavelength, _, _ in samples:
        index = int(np.floor((wavelength - edge) / binwidth))
        groups.setdefault(index, {"spherex_dr1": [], "spherex_qr3": []})[
            collection
        ].append(wavelength)

    shared: list[tuple[float, float]] = []
    for group in groups.values():
        if not group["spherex_dr1"] or not group["spherex_qr3"]:
            continue
        centre_a = float(np.mean(group["spherex_dr1"]))
        centre_b = float(np.mean(group["spherex_qr3"]))
        if abs(centre_a - centre_b) <= 0.5 * binwidth:
            shared.append((centre_a, centre_b))

    assert len(shared) >= 2


@pytest.mark.parametrize(
    "path",
    sorted((FIXTURE / "cutouts").glob("barnard_*.fits")),
    ids=lambda path: path.name,
)
def test_each_barnard_cutout_has_the_science_extensions(path: Path) -> None:
    with fits.open(path) as hdul:
        names = {hdu.name for hdu in hdul}
    assert {"IMAGE", "FLAGS", "VARIANCE", "WCS-WAVE"} <= names
    assert "EPSF" not in names
