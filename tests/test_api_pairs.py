"""Every pair of a multi-epoch target is requestable.

The fixture's LPV entry carries three derived survey groups across DR1 and
QR3. Each pair must return a bounded compare stack with a release on every
plane.
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from drift.api import create_app
from drift.config import Settings
from drift.ingest.synthetic import build_synthetic_snapshot


@pytest.fixture(scope="session")
def snapshot_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pair-snapshots")
    build_synthetic_snapshot(root)
    return root


@pytest.fixture
def client(snapshot_root: Path) -> TestClient:
    settings = Settings.from_env(
        {
            "DRIFT_SNAPSHOT_DIR": str(snapshot_root),
            "DRIFT_CACHE_DIR": str(snapshot_root / "cache"),
            "DRIFT_ALLOW_FIXTURE": "true",
        }
    )
    return TestClient(create_app(settings))


def test_the_lpv_fixture_has_three_survey_groups(client: TestClient) -> None:
    detail = client.get("/api/v1/targets/lpv").json()

    assert len(detail["epochs"]) == 3
    assert {epoch["release"] for epoch in detail["epochs"]} <= {"dr1", "qr3"}


@pytest.mark.parametrize("pair", list(combinations(range(3), 2)))
def test_every_lpv_epoch_pair_returns_a_bounded_stack(
    client: TestClient, pair: tuple[int, int]
) -> None:
    response = client.get(
        f"/api/v1/targets/lpv/frames?mode=epoch&bins=8&epochs={pair[0]},{pair[1]}"
    )

    assert response.status_code == 200
    body = response.json()
    assert [epoch["epoch_id"] for epoch in body["epochs"]] == [
        client.get("/api/v1/targets/lpv").json()["epochs"][index]["epoch_id"]
        for index in pair
    ]
    assert body["plane_count"] == len(body["planes"]) == 16
    assert body["plane_count"] <= 16
    assert body["degraded"] is False
    assert {plane["release"] for plane in body["planes"]} <= {"dr1", "qr3"}
    assert {plane["window_sha256"] for plane in body["planes"]} == {
        body["window"]["sha256"]
    }
    for plane in body["planes"]:
        assert plane["detector"]
        assert plane["mjd"] is not None
        assert plane["date"].endswith("Z")
        assert plane["dtype"] == "float16"


def test_the_lpv_compare_defaults_to_the_first_and_last_group(
    client: TestClient,
) -> None:
    default = client.get("/api/v1/targets/lpv/frames?mode=epoch&bins=8").json()
    assert len(default["epochs"]) == 2
    assert default["plane_count"] == 16

    body = client.get("/api/v1/targets/lpv/frames?mode=epoch&bins=8&epochs=all").json()

    assert len(body["epochs"]) == 3
    assert body["bins"] == 5
    assert body["plane_count"] == 15
    assert {plane["release"] for plane in body["planes"]} <= {"dr1", "qr3"}
