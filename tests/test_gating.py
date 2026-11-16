"""Mode eligibility is per target and every refusal prints its reason."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from drift.api import create_app
from drift.config import Settings
from drift.ingest.synthetic import build_synthetic_snapshot

FORBIDDEN = ("discovery", "new object", "transient", "confirmed", "never seen before")


@pytest.fixture(scope="session")
def snapshot_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("gating-snapshots")
    build_synthetic_snapshot(root)
    return root


@pytest.fixture
def client(snapshot_root: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings.from_env(
                {
                    "DRIFT_SNAPSHOT_DIR": str(snapshot_root),
                    "DRIFT_CACHE_DIR": str(snapshot_root / "cache"),
                    "DRIFT_ALLOW_FIXTURE": "true",
                }
            )
        )
    )


def _manifest_client(tmp_path: Path, target: dict[str, object]) -> TestClient:
    snapshot_id = "test-gating-1"
    manifest = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "generated_at": "2026-11-15T00:00:00Z",
        "status": "published",
        "fixture": False,
        "targets": [target],
        "files": [],
        "alerts": [],
        "window": {"lo": 0.0, "hi": 1.0, "s": 0.1},
    }
    directory = tmp_path / "v1" / snapshot_id
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "latest.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "manifest": f"v1/{snapshot_id}/manifest.json",
            }
        ),
        encoding="utf-8",
    )
    return TestClient(
        create_app(
            Settings.from_env(
                {
                    "DRIFT_SNAPSHOT_DIR": str(tmp_path),
                    "DRIFT_CACHE_DIR": str(tmp_path / "cache"),
                }
            )
        )
    )


# AC3 -----------------------------------------------------------------------


def test_wise0855_survey_compare_is_disabled_with_a_printed_reason(
    client: TestClient,
) -> None:
    body = client.get("/api/v1/targets/wise0855").json()
    mode = body["modes"]["survey_compare"]

    assert mode["enabled"] is False
    assert mode["reason"]
    assert "one survey epoch" in mode["reason"].lower()
    assert "epoch_compare" not in body["modes"]
    assert body["modes"]["timeline"]["enabled"] is True
    assert body["timeline"]["band_reason"]


def test_mode_eligibility_exposes_the_documented_available_field(
    client: TestClient,
) -> None:
    mode = client.get("/api/v1/targets/wise0855").json()["modes"]["survey_compare"]

    assert mode["available"] is False
    assert mode["enabled"] is False


def test_the_epoch_wire_alias_still_works(client: TestClient) -> None:
    alias = client.get("/api/v1/targets/barnard/frames?mode=epoch&bins=4")
    canonical = client.get("/api/v1/targets/barnard/frames?mode=survey_compare&bins=4")

    assert alias.status_code == 200
    assert canonical.status_code == 200
    assert alias.json()["mode"] == "epoch"
    assert canonical.json()["bins"] == 4


# AC4 -----------------------------------------------------------------------


def test_one_epoch_is_never_offered_comparison(client: TestClient) -> None:
    detail = client.get("/api/v1/targets/wise0855").json()
    response = client.get("/api/v1/targets/wise0855/frames?mode=epoch")

    assert detail["modes"]["survey_compare"]["enabled"] is False
    assert response.status_code == 409
    assert "one survey epoch" in response.json()["detail"].lower()


def test_one_visit_disables_the_timeline_with_a_reason(tmp_path: Path) -> None:
    client = _manifest_client(
        tmp_path,
        {
            "id": "single",
            "visits": [
                {
                    "visit_id": "dr1-1-D4",
                    "mjd": 60000.0,
                    "release": "dr1",
                    "detector": "D4",
                    "frames": [
                        {
                            "band": 3,
                            "detector": "D4",
                            "href": "v1/x/planes/p.spx",
                        }
                    ],
                }
            ],
            "timeline": {"default_band": 3, "visits": 1},
            "windows": [{"band": 3, "lo": 0.0, "hi": 1.0, "s": 0.1}],
        },
    )

    detail = client.get("/api/v1/targets/single").json()

    assert detail["modes"]["timeline"]["enabled"] is False
    assert "needs two" in detail["modes"]["timeline"]["reason"]
    assert detail["modes"]["survey_compare"]["enabled"] is False
    assert detail["modes"]["survey_compare"]["reason"]


def test_default_band_is_chosen_by_visits_then_sigma_then_index(
    tmp_path: Path,
) -> None:
    def frame(band: int, sigma: float) -> dict[str, object]:
        return {
            "band": band,
            "detector": "D4",
            "sigma": sigma,
            "href": "v1/x/planes/p.spx",
        }

    client = _manifest_client(
        tmp_path,
        {
            "id": "tie",
            "visits": [
                {
                    "visit_id": f"dr1-{index}-D4",
                    "mjd": float(60000 + index),
                    "release": "dr1",
                    "detector": "D4",
                    "frames": [frame(1, 5.0), frame(2, 1.0)],
                }
                for index in range(3)
            ]
            + [
                {
                    "visit_id": f"dr1-{index}-D4",
                    "mjd": float(61000 + index),
                    "release": "dr1",
                    "detector": "D4",
                    "frames": [frame(3, 0.1)],
                }
                for index in range(2)
            ],
            "timeline": {"default_band": 3, "visits": 2},
            "windows": [
                {"band": 1, "lo": 0.0, "hi": 1.0, "s": 0.1},
                {"band": 2, "lo": 0.0, "hi": 1.0, "s": 0.1},
                {"band": 3, "lo": 0.0, "hi": 1.0, "s": 0.1},
            ],
        },
    )

    body = client.get("/api/v1/targets/tie").json()
    frames = client.get("/api/v1/targets/tie/frames?mode=timeline&visits=8").json()

    assert body["timeline"]["default_band"] == 2
    assert body["timeline"]["band_reason"]
    assert "most matched visits" in body["timeline"]["band_reason"]
    assert "lower noise" in body["timeline"]["band_reason"]
    assert frames["band"] == 2
    assert frames["default_band"] == 2


def test_default_band_counts_only_stackable_visits(tmp_path: Path) -> None:
    def visit(index: int, band: int, detector: str) -> dict[str, object]:
        return {
            "visit_id": f"dr1-{index}-{detector}",
            "mjd": float(60000 + index),
            "release": "dr1",
            "detector": detector,
            "frames": [
                {
                    "band": band,
                    "detector": detector,
                    "sigma": 1.0,
                    "href": "v1/x/planes/p.spx",
                }
            ],
        }

    client = _manifest_client(
        tmp_path,
        {
            "id": "detectors",
            "visits": [
                visit(1, 1, "D1"),
                visit(2, 1, "D2"),
                visit(3, 1, "D3"),
                visit(4, 2, "D4"),
                visit(5, 2, "D4"),
            ],
            "timeline": {"default_band": 1, "visits": 3},
            "windows": [
                {"band": 1, "lo": 0.0, "hi": 1.0, "s": 0.1},
                {"band": 2, "lo": 0.0, "hi": 1.0, "s": 0.1},
            ],
        },
    )

    body = client.get("/api/v1/targets/detectors").json()
    frames = client.get("/api/v1/targets/detectors/frames?mode=timeline").json()

    assert body["modes"]["timeline"]["enabled"] is True
    assert body["timeline"]["default_band"] == 2
    assert "Band 2 holds the most matched visits (2)" in body["timeline"]["band_reason"]
    assert frames["band"] == 2
    assert frames["default_band"] == 2
    assert len(frames["planes"]) == 2


# AC5 -----------------------------------------------------------------------


def test_new_refusal_copy_uses_no_forbidden_word(client: TestClient) -> None:
    text: list[str] = []
    for target in client.get("/api/v1/targets").json()["targets"]:
        text.extend(mode["reason"] for mode in target["modes"].values())
    for target_id in ("barnard", "wise0855"):
        detail = client.get(f"/api/v1/targets/{target_id}").json()
        text.extend(mode["reason"] for mode in detail["modes"].values())

    combined = " ".join(text).lower()
    for forbidden in FORBIDDEN:
        assert forbidden not in combined
