"""Contract tests for the primary visit-timeline mode."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from drift.api import create_app, frames_payload
from drift.config import Settings
from drift.ingest.synthetic import build_synthetic_snapshot
from drift.manifest import validate


@pytest.fixture(scope="session")
def snapshot_root(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("timeline-snapshots")
    build_synthetic_snapshot(root)
    return root


@pytest.fixture
def client(snapshot_root) -> TestClient:
    settings = Settings.from_env(
        {
            "DRIFT_SNAPSHOT_DIR": str(snapshot_root),
            "DRIFT_CACHE_DIR": str(snapshot_root / "cache"),
            "DRIFT_ALLOW_FIXTURE": "true",
        }
    )
    return TestClient(create_app(settings))


def _frames(client: TestClient, query: str) -> Any:
    return client.get(f"/api/v1/targets/{query}")


# AC1 -----------------------------------------------------------------------


def test_default_mode_is_the_visit_timeline(client: TestClient) -> None:
    response = _frames(client, "barnard/frames")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "timeline"
    assert body["band"] > 0
    assert body["detector"]
    assert len(body["planes"]) == len(body["visits_meta"])


def test_timeline_returns_chronological_visit_planes_with_one_window(
    client: TestClient,
) -> None:
    body = _frames(client, "barnard/frames?mode=timeline&visits=8").json()

    assert body["visits_requested"] == 8
    assert body["visits"] == 8
    assert len(body["planes"]) == 8
    mjds = [plane["mjd"] for plane in body["planes"]]
    assert mjds == sorted(mjds)
    assert body["degraded"] is False
    assert body["frames_omitted"] == []

    windows = [plane["window_sha256"] for plane in body["planes"]]
    assert len(set(windows)) == 1
    assert body["window"]["sha256"] == windows[0]
    assert body["window"]["lo"] < body["window"]["hi"]

    for plane in body["planes"]:
        assert plane["visit_id"]
        assert plane["date"].endswith("Z")
        assert isinstance(plane["mjd"], float)
        assert plane["release"] in {"dr1", "qr3"}
        assert plane["detector"] == body["detector"]
        assert plane["band"] == body["band"]
        assert plane["dtype"] == "float16"
        assert plane["width"] == plane["height"] == 512
        assert plane["lambda_lo_um"] <= plane["lambda_um"] <= plane["lambda_hi_um"]


def test_every_visit_carries_its_release_and_date(client: TestClient) -> None:
    body = _frames(client, "lpv/frames?mode=timeline&visits=16").json()

    releases = [plane["release"] for plane in body["planes"]]
    assert set(releases) == {"dr1", "qr3"}
    assert len(body["planes"]) == 9
    assert body["span_days"] > 400


def test_a_stack_never_mixes_detectors(client: TestClient) -> None:
    body = _frames(client, "bamberga/frames?mode=timeline&visits=8").json()

    assert {plane["detector"] for plane in body["planes"]} == {body["detector"]}
    assert body["refused_frames"] == []


# Bamberga motion -----------------------------------------------------------


def test_bamberga_timeline_holds_its_two_visits_1_288_days_apart(
    client: TestClient,
) -> None:
    body = _frames(client, "bamberga/frames?mode=timeline&visits=8").json()

    assert body["visits"] == 2
    assert body["visits_requested"] == 8
    assert body["degraded"] is False
    gap = body["planes"][1]["mjd"] - body["planes"][0]["mjd"]
    assert gap == pytest.approx(1.288, abs=0.001)
    assert body["span_days"] == pytest.approx(gap)


# Budget and degradation ----------------------------------------------------


def test_requesting_more_visits_than_exist_is_not_degradation(
    client: TestClient,
) -> None:
    response = _frames(client, "barnard/frames?mode=timeline&visits=16")

    assert response.status_code == 200
    body = response.json()
    assert body["visits"] == 8
    assert body["available_visits"] == 8
    assert body["degraded"] is False
    assert response.headers["x-spx-degraded"] == "0"


def test_an_over_budget_stack_thins_over_mjd_and_says_so() -> None:
    visits = [
        {
            "visit_id": f"dr1-{60000 + index}.0-D4",
            "mjd": float(60000 + index),
            "date": f"2026-01-{index + 1:02d}T00:00:00Z",
            "release": "dr1",
            "survey": 1,
            "detector": "D4",
            "frames": [{"band": 3, "detector": "D4", "href": "v1/x/planes/p.spx"}],
        }
        for index in range(20)
    ]
    manifest = validate(
        {
            "schema_version": 1,
            "snapshot_id": "test-1",
            "targets": [
                {
                    "id": "many",
                    "visits": visits,
                    "timeline": {"default_band": 3, "visits": 20},
                    "windows": [{"band": 3, "lo": 0.0, "hi": 1.0, "s": 0.1}],
                }
            ],
        }
    )

    body = frames_payload(manifest, "many", "timeline", 8, visits=8)

    assert body["visits"] == 8
    assert body["available_visits"] == 20
    assert body["degraded"] is True
    assert len(body["frames_omitted"]) == 12
    kept = {plane["visit_id"] for plane in body["planes"]}
    assert kept.isdisjoint(body["frames_omitted"])
    assert body["planes"][0]["mjd"] == 60000.0
    assert body["planes"][-1]["mjd"] == 60019.0


def test_a_detector_mismatch_is_refused_not_mixed() -> None:
    def visit(index: int, detector: str) -> dict[str, Any]:
        return {
            "visit_id": f"dr1-{index}-{detector}",
            "mjd": float(60000 + index),
            "date": "2026-01-01T00:00:00Z",
            "release": "dr1",
            "detector": detector,
            "frames": [{"band": 3, "detector": detector, "href": "v1/x/planes/p.spx"}],
        }

    manifest = validate(
        {
            "schema_version": 1,
            "snapshot_id": "test-2",
            "targets": [
                {
                    "id": "mixed",
                    "visits": [visit(1, "D4"), visit(2, "D4"), visit(3, "D5")],
                    "windows": [{"band": 3, "lo": 0.0, "hi": 1.0, "s": 0.1}],
                }
            ],
        }
    )

    body = frames_payload(manifest, "mixed", "timeline", 8, visits=8)

    assert body["detector"] == "D4"
    assert body["visits"] == 2
    assert body["refused_frames"] == ["dr1-3-D5"]


def test_one_visit_refuses_the_timeline_with_a_reason() -> None:
    manifest = validate(
        {
            "schema_version": 1,
            "snapshot_id": "test-3",
            "targets": [
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
                    "windows": [{"band": 3, "lo": 0.0, "hi": 1.0, "s": 0.1}],
                }
            ],
        }
    )

    with pytest.raises(Exception, match="needs two"):
        frames_payload(manifest, "single", "timeline", 8, visits=8)


# Band selection and per-band windows --------------------------------------


def test_each_band_stack_carries_its_own_window(client: TestClient) -> None:
    first = _frames(client, "barnard/frames?mode=timeline&band=1").json()
    second = _frames(client, "barnard/frames?mode=timeline&band=4").json()

    assert first["band"] == 1
    assert second["band"] == 4
    assert first["window"]["sha256"] != second["window"]["sha256"]
    assert {plane["window_sha256"] for plane in second["planes"]} == {
        second["window"]["sha256"]
    }


def test_default_band_is_the_one_with_the_most_visits(client: TestClient) -> None:
    detail = _frames(client, "barnard").json()
    frames = _frames(client, "barnard/frames?mode=timeline").json()

    assert detail["timeline"]["default_band"] == 1
    assert frames["band"] == detail["timeline"]["default_band"]
    assert frames["default_band"] == detail["timeline"]["default_band"]
    assert detail["timeline"]["visits"] == 8


# Mode eligibility ----------------------------------------------------------


def test_modes_report_timeline_and_survey_compare_separately(
    client: TestClient,
) -> None:
    targets = _frames(client, "").json()["targets"]
    by_id = {target["id"]: target for target in targets}

    assert by_id["barnard"]["modes"]["timeline"]["enabled"] is True
    assert by_id["barnard"]["modes"]["survey_compare"]["enabled"] is True
    assert by_id["wise0855"]["modes"]["survey_compare"]["enabled"] is False
    assert "one survey epoch" in (
        by_id["wise0855"]["modes"]["survey_compare"]["reason"].lower()
    )
    assert by_id["wise0855"]["modes"]["timeline"]["enabled"] is True


def test_visits_outside_the_visit_range_are_rejected(client: TestClient) -> None:
    assert _frames(client, "barnard/frames?mode=timeline&visits=1").status_code == 422
    assert _frames(client, "barnard/frames?mode=timeline&visits=99").status_code == 422
