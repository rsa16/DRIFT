from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from drift.cubes import (
    PairBuilder,
    WavelengthBinner,
    WavelengthSample,
    propagate_position,
)
from drift.ingest import cli
from drift.ingest.targets import TARGETS
from drift.planes import decode_plane

FIXTURE = Path(__file__).parent / "fixtures" / "archive"
PLANES = Path(__file__).parent / "fixtures" / "planes"


def _build(tmp_path: Path):
    ra, dec, pm_ra, pm_dec = TARGETS["barnard"]
    exposures = cli.load_fixture_exposures(FIXTURE, "barnard")
    return PairBuilder().build(
        "barnard",
        exposures,
        target_ra=ra,
        target_dec=dec,
        pm_ra_mas=pm_ra,
        pm_dec_mas=pm_dec,
    )


def _write(tmp_path: Path):
    result = _build(tmp_path)
    result.write(tmp_path / "planes")
    return result


def test_matching_uses_measured_wavelength_not_channel_index() -> None:
    samples = [
        WavelengthSample("dr1", 3.8435, 0.0357, "dr1-a"),
        WavelengthSample("dr1", 3.9101, 0.0362, "dr1-b"),
        WavelengthSample("qr3", 3.8708, 0.0355, "qr3-a"),
        WavelengthSample("qr3", 3.9380, 0.0361, "qr3-b"),
        WavelengthSample("qr3", 4.4198, 0.0381, "qr3-far"),
    ]
    assert not any(hasattr(sample, "channel") for sample in samples)

    forward = WavelengthBinner(channels=2).group(samples)
    reverse = WavelengthBinner(channels=2).group(list(reversed(samples)))

    def shape(bins):
        return [
            (
                item.index,
                tuple(sorted(sample.sample_id for sample in item.samples)),
            )
            for item in bins
        ]

    assert shape(forward) == shape(reverse)
    assert len(forward) == 1
    assert len(forward[0].samples) == 4
    assert forward[0].samples_by_epoch == {"dr1": 2, "qr3": 2}
    assert forward[0].channels == 2


def test_bins_are_at_least_two_channels_and_obey_the_half_bin_tolerance(
    tmp_path: Path,
) -> None:
    result = _build(tmp_path)

    assert result.bins
    assert all(item.channels >= 2 for item in result.bins)
    assert all(
        item.width_um >= 2 * min(s.bandwidth_um for s in item.samples)
        for item in result.bins
    )
    for item in result.bins:
        dr1 = item.wavelength_by_epoch["dr1"]
        qr3 = item.wavelength_by_epoch["qr3"]
        assert abs(dr1 - qr3) <= 0.5 * item.width_um

    assert len(result.planes) == 2 * len(result.bins)
    assert result.matched_bin_count == len(result.bins)
    assert result.matched_bin_count == len({p.bin_index for p in result.planes})


def test_reprojection_is_the_shared_north_up_stamp(tmp_path: Path) -> None:
    result = _build(tmp_path)
    ra, dec, pm_ra, pm_dec = TARGETS["barnard"]
    expected_ra, expected_dec = propagate_position(
        ra, dec, pm_ra, pm_dec, result.reference_mjd
    )

    assert result.ra_deg == pytest.approx(expected_ra, abs=1e-9)
    assert result.dec_deg == pytest.approx(expected_dec, abs=1e-9)
    wcs = result.wcs
    assert wcs.array_shape == (512, 512)
    assert list(wcs.wcs.ctype) == ["RA---TAN", "DEC--TAN"]
    assert wcs.wcs.crval[0] == pytest.approx(expected_ra)
    assert wcs.wcs.crval[1] == pytest.approx(expected_dec)
    matrix = wcs.pixel_scale_matrix
    assert matrix[0, 0] < 0 < matrix[1, 1]
    assert matrix[0, 1] == pytest.approx(0.0, abs=1e-12)
    assert matrix[1, 0] == pytest.approx(0.0, abs=1e-12)
    assert np.sqrt(np.abs(np.linalg.det(matrix))) * 3600 == pytest.approx(
        6.158, abs=1e-3
    )

    for plane in result.planes:
        assert plane.image.shape == (512, 512)
        assert plane.variance.shape == (512, 512)
        assert plane.flags.shape == (512, 512)
        assert plane.bad_mask.shape == (512, 512)


def test_flags_and_variance_are_propagated_per_plane(tmp_path: Path) -> None:
    result = _build(tmp_path)

    assert any(plane.flag_bad_frac > 0 for plane in result.planes)
    for plane in result.planes:
        total = int(np.count_nonzero(plane.footprint))
        bad = int(np.count_nonzero(plane.bad_mask & plane.footprint))
        assert total > 0
        assert plane.flag_bad_frac == pytest.approx(bad / total)
        assert plane.flag_fractions
        assert set(plane.flag_fractions) >= {"OVERFLOW", "OUTLIER"}
        assert np.all(np.isnan(plane.image[plane.bad_mask & plane.footprint]))
        valid = plane.footprint & ~plane.bad_mask & plane.matched_mask
        assert np.count_nonzero(valid) > 0
        assert np.all(np.isfinite(plane.image[valid]))
        assert np.all(plane.variance[valid] > 0)
        assert plane.sigma > 0


def test_sidecar_carries_every_required_field_and_matches_plane_hashes(
    tmp_path: Path,
) -> None:
    result = _write(tmp_path)
    sidecar = json.loads((tmp_path / "planes" / "barnard_pair.json").read_text())

    assert sidecar["target"] == "barnard"
    assert sidecar["matched_bin_count"] == len(result.bins)
    assert len(sidecar["planes"]) == 2 * len(result.bins)

    root = tmp_path / "planes"
    for entry in sidecar["planes"]:
        assert {
            "lambda_um",
            "dlambda_um",
            "release",
            "epoch_id",
            "source_url",
            "sha256",
            "sigma",
            "flag_bad_frac",
        } <= set(entry)
        assert entry["release"] in {"dr1", "qr3"}
        assert entry["epoch_id"] == entry["release"]
        assert entry["lambda_um"] > 0
        assert entry["dlambda_um"] >= 2 * 0.03
        assert entry["source_url"].startswith("https://irsa.ipac.caltech.edu/")
        assert entry["sigma"] > 0
        path = root / entry["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        header = path.read_bytes()[:4]
        assert header == b"SPX1"


def test_committed_planes_match_their_sidecar_and_headers() -> None:
    sidecar = json.loads((PLANES / "barnard_pair.json").read_text())
    assert sidecar["planes"]
    for entry in sidecar["planes"]:
        body = (PLANES / entry["path"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
        plane = decode_plane(body)
        assert plane.header.width == 512
        assert plane.header.height == 512
        assert plane.header.channels == entry["channels"]
        assert plane.header.channels >= 2


def test_builder_output_is_invariant_to_exposure_order(tmp_path: Path) -> None:
    ra, dec, pm_ra, pm_dec = TARGETS["barnard"]
    exposures = cli.load_fixture_exposures(FIXTURE, "barnard")
    builder = PairBuilder()
    forward = builder.build(
        "barnard",
        exposures,
        target_ra=ra,
        target_dec=dec,
        pm_ra_mas=pm_ra,
        pm_dec_mas=pm_dec,
    )
    reverse = builder.build(
        "barnard",
        list(reversed(exposures)),
        target_ra=ra,
        target_dec=dec,
        pm_ra_mas=pm_ra,
        pm_dec_mas=pm_dec,
    )

    def signature(result):
        return [
            (plane.bin_index, plane.epoch_id, round(plane.lambda_um, 6))
            for plane in result.planes
        ]

    assert signature(forward) == signature(reverse)


def test_two_passes_in_one_release_become_two_epochs() -> None:
    exposures = cli.load_fixture_exposures(FIXTURE, "barnard")
    qr3 = next(exposure for exposure in exposures if exposure.release == "qr3")
    separated = [
        replace(exposure, epoch_id="dr1", release="dr1")
        if exposure.release == "dr1"
        else exposure
        for exposure in exposures
    ]
    separated.append(replace(qr3, mjd=qr3.mjd - 500.0, epoch_id="dr1", release="dr1"))

    grouped = cli.assign_epoch_ids(separated)

    by_release: dict[str, set[str]] = {}
    for exposure in grouped:
        by_release.setdefault(exposure.release, set()).add(exposure.epoch_id)
    assert len(by_release["dr1"]) == 2
    assert len(by_release["qr3"]) == 1


def test_qr2_is_refused_by_domain_rule() -> None:
    with pytest.raises(ValueError, match="QR2"):
        cli.release_from_collection("spherex_qr2")
    assert cli.release_from_collection("spherex_dr1") == "dr1"


def test_pair_cli_writes_the_pair_with_egress_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args, **kwargs):
        raise AssertionError("pair command attempted network access")

    monkeypatch.setattr("drift.archive.UrllibTransport.get", blocked)

    exit_code = cli.main(
        [
            "pair",
            "--fixture",
            str(FIXTURE),
            "--target",
            "barnard",
            "--out",
            str(tmp_path / "out"),
        ]
    )

    assert exit_code == 0
    assert (tmp_path / "out" / "barnard_pair.json").is_file()
    assert list((tmp_path / "out").glob("barnard_*.spx"))
