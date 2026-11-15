from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.coordinates import SkyCoord  # type: ignore[import-untyped]
from astropy.io import fits  # type: ignore[import-untyped]
from astropy.wcs import WCS  # type: ignore[import-untyped]

from drift.cubes import Reprojector, WaveAxis, read_cutout
from drift.ingest.fixture import FixtureStore

FIXTURE = Path(__file__).parent / "fixtures" / "archive"
OUTPUT_STAMP = 512


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


def test_wavelength_sip_shift_is_5_to_10_nm() -> None:
    per_collection: dict[str, list[float]] = {}
    for entry in _barnard_cutouts():
        with fits.open(FIXTURE / entry["path"]) as hdul:
            header = hdul["IMAGE"].header.copy()
            axis = WaveAxis.from_hdul(hdul)
            sip_on = WCS(header, fobj=hdul, key="W")
            ny, nx = hdul["IMAGE"].data.shape
            x, y = np.meshgrid(np.arange(nx), np.arange(ny))
            shift_nm = (
                sip_on.pixel_to_world_values(x, y)[0] - axis.wavelength_um
            ) * 1_000.0
        per_collection.setdefault(str(entry["collection"]), []).append(
            float(np.nanmax(np.abs(shift_nm)))
        )

    assert set(per_collection) == {"spherex_dr1", "spherex_qr3"}
    for collection, shifts in per_collection.items():
        assert 5.0 <= max(shifts) <= 10.0, (collection, max(shifts))


def test_stripping_sip_from_sky_displaces_sources_60_to_124_arcsec() -> None:
    per_collection: dict[str, list[float]] = {}
    for entry in _barnard_cutouts():
        with fits.open(FIXTURE / entry["path"]) as hdul:
            header = hdul["IMAGE"].header.copy()
            sky_on = Reprojector.sky_wcs(header, fobj=hdul)
            sky_off = WCS(WaveAxis.strip_sip(header), fobj=hdul).celestial
            ny, nx = hdul["IMAGE"].data.shape
            xx, yy = _centred_pixels(OUTPUT_STAMP, (ny, nx))
            ra_on, dec_on = sky_on.all_pix2world(xx, yy, 0)
            ra_off, dec_off = sky_off.all_pix2world(xx, yy, 0)
            displacement = (
                SkyCoord(ra_on, dec_on, unit="deg")
                .separation(SkyCoord(ra_off, dec_off, unit="deg"))
                .arcsec
            )
        per_collection.setdefault(str(entry["collection"]), []).append(
            float(np.nanmax(displacement))
        )

    assert set(per_collection) == {"spherex_dr1", "spherex_qr3"}
    for collection, displacements in per_collection.items():
        assert 60.0 <= max(displacements) <= 124.0, (
            collection,
            max(displacements),
        )


def test_target_wavelength_uses_the_sip_on_sky_axis() -> None:
    entry = _barnard_cutouts()[0]
    with fits.open(FIXTURE / entry["path"]) as hdul:
        header = hdul["IMAGE"].header.copy()
        cutout = read_cutout((FIXTURE / entry["path"]).read_bytes())
        wave = WaveAxis.from_hdul(hdul)
        ra, dec = entry["center"]
        sip_on_x, sip_on_y = cutout.sky_wcs.all_world2pix([ra], [dec], 0)
        sip_off = WCS(WaveAxis.strip_sip(header), fobj=hdul).celestial
        sip_off_x, sip_off_y = sip_off.all_world2pix([ra], [dec], 0)

    on_lambda, _ = wave.measure(float(sip_on_x[0]), float(sip_on_y[0]))
    off_lambda, _ = wave.measure(float(sip_off_x[0]), float(sip_off_y[0]))

    assert abs((on_lambda - off_lambda) * 1_000.0) > 0.5
    measured, _ = cutout.target_wavelength(ra, dec)
    assert measured == pytest.approx(on_lambda, abs=1e-9)


def test_output_grid_is_512_north_up_at_native_scale() -> None:
    wcs = Reprojector().output_wcs(269.4462, 4.7693)
    assert wcs.array_shape == (512, 512)
    assert wcs.wcs.ctype[0] == "RA---TAN"
    assert wcs.wcs.ctype[1] == "DEC--TAN"
    scale = np.sqrt(np.abs(np.linalg.det(wcs.pixel_scale_matrix))) * 3600.0
    assert scale == pytest.approx(6.158, abs=1e-3)
    matrix = wcs.pixel_scale_matrix
    assert matrix[0, 1] == pytest.approx(0.0, abs=1e-12)
    assert matrix[1, 0] == pytest.approx(0.0, abs=1e-12)
    assert matrix[0, 0] < 0 < matrix[1, 1]
