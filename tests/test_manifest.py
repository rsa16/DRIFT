import json
from copy import deepcopy

import pytest

from drift.manifest import ManifestError, load_latest, validate

SPEC_EXAMPLE = {
    "schema_version": 1,
    "snapshot_id": "20261114T235900Z-<hash8>",
    "generated_at": "2026-11-14T23:59:00Z",
    "generator": {
        "name": "spherex-epoch-viewer",
        "version": "0.1.0",
        "commit": "<sha>",
    },
    "processing": {
        "epoch_gap_days": 0,
        "plane_px": 512,
        "fov_deg": 0.87,
        "dtype": "float16",
        "reprojector": "reproject_interp+nearest",
        "band_bins": 50,
        "blink_bins": 8,
    },
    "status": "published",
    "source": {
        "service": "IRSA SIA2",
        "collections": ["spherex_dr1", "spherex_qr3"],
        "queried_at": "2026-11-14T23:58:00Z",
        "row_counts": {},
    },
    "watermark": {"prev_snapshot_id": "<prev>", "rows_sha256": "<sha256>"},
    "targets": [
        {
            "id": "barnard",
            "ra": 269.452075,
            "dec": 4.693365,
            "pm": [-798.71, 10337.77],
            "stale": False,
            "epochs": [
                {
                    "epoch_id": "s1",
                    "survey": 1,
                    "release": "dr1",
                    "mjd_min": 60789.8,
                    "mjd_max": 60850.0,
                    "frames": [
                        {
                            "band": 12,
                            "lambda_um": 1.23,
                            "dlambda_um": 0.03,
                            "obs_publisher_did": "<did>",
                            "source_url": "https://example.invalid/cutout",
                            "href": "v1/<snapshot>/planes/barnard_b12_dr1.spx",
                            "bytes": 363251,
                            "sha256": "<sha256>",
                            "sigma": 0.0275,
                            "flag_bad_frac": 0.02,
                            "saturated": True,
                        }
                    ],
                }
            ],
            "spectrum": {
                "source": "dr1_cube",
                "bands": 100,
                "time_averaged": "2025-04-25/2026-05-14",
            },
            "labels": ["saturated"],
        }
    ],
    "files": [
        {
            "path": "v1/<snapshot>/planes/barnard_b12_dr1.spx",
            "bytes": 363251,
            "sha256": "<sha256>",
        }
    ],
    "alerts": [],
    "citation": "SPHEREx acknowledgement",
}


def test_accepts_the_spec_example_manifest():
    manifest = validate(deepcopy(SPEC_EXAMPLE))

    assert manifest.schema_version == 1
    assert manifest.snapshot_id == SPEC_EXAMPLE["snapshot_id"]
    assert manifest.targets[0].id == "barnard"
    assert manifest.targets[0].epochs[0].release == "dr1"
    assert manifest.targets[0].epochs[0].frames[0].lambda_um == 1.23
    assert manifest.files[0].bytes == 363251


def test_accepts_json_text_and_bytes():
    text = json.dumps(SPEC_EXAMPLE)

    assert validate(text).snapshot_id == SPEC_EXAMPLE["snapshot_id"]
    assert validate(text.encode()).snapshot_id == SPEC_EXAMPLE["snapshot_id"]


def test_rejects_an_unknown_schema_version_major():
    payload = deepcopy(SPEC_EXAMPLE)
    payload["schema_version"] = 2

    with pytest.raises(ManifestError, match="schema_version"):
        validate(payload)


def test_rejects_missing_or_malformed_json():
    with pytest.raises(ManifestError, match="JSON"):
        validate("{not json")
    with pytest.raises(ManifestError, match="schema_version"):
        validate({"snapshot_id": "x"})


def test_load_latest_returns_a_pointer_and_resolves_the_manifest(tmp_path):
    snapshot_id = SPEC_EXAMPLE["snapshot_id"]
    manifest_dir = tmp_path / "v1" / snapshot_id
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(json.dumps(SPEC_EXAMPLE))
    (tmp_path / "latest.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "manifest": f"v1/{snapshot_id}/manifest.json",
                "published_at": "2026-11-14T23:59:00Z",
            }
        )
    )

    pointer = load_latest(tmp_path)

    assert pointer is not None
    assert pointer.snapshot_id == snapshot_id
    assert pointer.load_manifest(tmp_path).snapshot_id == snapshot_id


def test_load_latest_returns_none_when_no_snapshot_exists(tmp_path):
    assert load_latest(tmp_path) is None
