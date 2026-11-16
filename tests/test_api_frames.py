"""Contract tests for the synthetic fixture snapshot and the frames API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from drift.api import create_app
from drift.config import Settings
from drift.ingest import cli
from drift.ingest.real import build_real_snapshot
from drift.ingest.synthetic import build_synthetic_snapshot
from drift.manifest import (
    FixtureSnapshotError,
    ManifestError,
    load_latest,
    publish_snapshot,
    validate,
)
from drift.planes import decode_plane

FIXTURE = Path(__file__).parent / "fixtures" / "archive"


@pytest.fixture(scope="session")
def snapshot_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("snapshots")
    build_synthetic_snapshot(root)
    return root


@pytest.fixture(scope="session")
def real_snapshot_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("real-snapshots")
    build_real_snapshot(
        root,
        fixture=FIXTURE,
        generated_at="2026-11-15T23:59:00Z",
    )
    return root


def _settings(root: Path, *, allow_fixture: bool = True) -> Settings:
    return Settings.from_env(
        {
            "DRIFT_SNAPSHOT_DIR": str(root),
            "DRIFT_CACHE_DIR": str(root / "cache"),
            "DRIFT_ALLOW_FIXTURE": "true" if allow_fixture else "false",
        }
    )


@pytest.fixture
def client(snapshot_root: Path) -> TestClient:
    return TestClient(create_app(_settings(snapshot_root)))


@pytest.fixture
def real_client(real_snapshot_root: Path) -> TestClient:
    return TestClient(create_app(_settings(real_snapshot_root, allow_fixture=False)))


def _count_key(value: Any, wanted: str) -> int:
    if isinstance(value, dict):
        return sum(
            (1 if key == wanted else 0) + _count_key(item, wanted)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_count_key(item, wanted) for item in value)
    return 0


def _staging_manifest(staging: Path, snapshot_id: str) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    manifest = validate(
        {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "generated_at": "2026-11-15T00:00:00Z",
            "status": "fixture" if snapshot_id.startswith("fixture-") else "published",
            "fixture": snapshot_id.startswith("fixture-"),
            "targets": [],
            "files": [],
            "alerts": [],
            "window": {"lo": 0.0, "hi": 1.0, "s": 0.1, "sha256": "a" * 64},
        }
    )
    (staging / "manifest.json").write_text(manifest.model_dump_json())


# AC1 -----------------------------------------------------------------------


def test_fixture_snapshot_is_schema_v1_and_marked_as_fixture(
    snapshot_root: Path,
) -> None:
    pointer = load_latest(snapshot_root)

    assert pointer is not None
    assert pointer.snapshot_id.startswith("fixture-")
    manifest = pointer.load_manifest(snapshot_root)
    assert manifest.schema_version == 1
    assert manifest.fixture is True
    assert manifest.status == "fixture"
    assert manifest.window is not None
    assert manifest.window.sha256
    assert manifest.targets
    assert manifest.files
    for entry in manifest.files:
        path = snapshot_root / entry.path
        assert path.is_file(), entry.path
        assert entry.bytes == path.stat().st_size
        assert entry.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_production_publish_rejects_a_fixture_without_an_explicit_flag(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    _staging_manifest(staging, "fixture-20261115T000000Z-deadbeef")
    snapshots = tmp_path / "snapshots"

    with pytest.raises(FixtureSnapshotError, match="allow_fixture"):
        publish_snapshot(staging, snapshots)

    assert load_latest(snapshots) is None
    assert not (snapshots / "v1" / "fixture-20261115T000000Z-deadbeef").exists()

    pointer = publish_snapshot(staging, snapshots, allow_fixture=True)
    assert pointer.snapshot_id == "fixture-20261115T000000Z-deadbeef"
    assert load_latest(snapshots) is not None


def test_publish_rejects_a_snapshot_id_that_escapes_the_version_root(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    _staging_manifest(staging, "../../escape")

    with pytest.raises(ManifestError, match="snapshot_id"):
        publish_snapshot(staging, tmp_path / "snapshots", allow_fixture=True)

    assert not (tmp_path / "escape").exists()


def test_api_refuses_a_fixture_snapshot_without_an_explicit_flag(
    snapshot_root: Path,
) -> None:
    client = TestClient(create_app(_settings(snapshot_root, allow_fixture=False)))

    response = client.get("/api/v1/manifest")

    assert response.status_code == 503
    assert "DRIFT_ALLOW_FIXTURE" in response.json()["detail"]


def test_cli_fixture_synthetic_writes_a_valid_snapshot(tmp_path: Path) -> None:
    assert cli.main(["fixture", "--synthetic", "--snapshot-dir", str(tmp_path)]) == 0

    pointer = load_latest(tmp_path)
    assert pointer is not None
    assert pointer.snapshot_id.startswith("fixture-")
    manifest = pointer.load_manifest(tmp_path)
    assert manifest.fixture is True
    assert manifest.schema_version == 1
    assert any(target.id == "barnard" for target in manifest.targets)


# AC2 -----------------------------------------------------------------------


def test_manifest_route_returns_the_schema_v1_manifest(client: TestClient) -> None:
    response = client.get("/api/v1/manifest")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["snapshot_id"].startswith("fixture-")
    assert body["fixture"] is True
    assert body["window"]["sha256"]
    assert validate(body).snapshot_id == body["snapshot_id"]


def test_targets_route_reports_mode_eligibility(client: TestClient) -> None:
    response = client.get("/api/v1/targets")

    assert response.status_code == 200
    body = response.json()
    by_id = {target["id"]: target for target in body["targets"]}
    assert by_id["barnard"]["modes"]["survey_compare"]["enabled"] is True
    assert by_id["wise0855"]["modes"]["survey_compare"]["enabled"] is False
    assert (
        "one survey epoch"
        in by_id["wise0855"]["modes"]["survey_compare"]["reason"].lower()
    )
    assert body["fixture"] is True


def test_target_detail_reports_epochs_pixel_scale_and_modes(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/targets/barnard")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "barnard"
    assert len(body["epochs"]) == 2
    assert body["epochs"][0]["release"] in {"dr1", "qr3"}
    assert body["pixel_scale_arcsec"] == pytest.approx(6.158)
    assert body["units"] == "MJy/sr"
    assert body["modes"]["survey_compare"]["enabled"] is True
    assert body["modes"]["timeline"]["enabled"] is True


def test_frames_returns_eight_by_two_planes_and_exactly_one_window(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/targets/barnard/frames?mode=epoch&bins=8")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "epoch"
    assert body["bins"] == 8
    assert len(body["planes"]) == 8 * 2
    assert len(body["epochs"]) == 2
    assert len({plane["band"] for plane in body["planes"]}) == 8
    assert len({plane["epoch_id"] for plane in body["planes"]}) == 2
    assert _count_key(body, "window") == 1

    window = body["window"]
    assert len(window["sha256"]) == 64
    assert window["lo"] < window["hi"]
    assert window["s"] > 0
    assert {plane["window_sha256"] for plane in body["planes"]} == {window["sha256"]}
    for plane in body["planes"]:
        assert plane["href"].startswith("/api/v1/snapshots/")
        assert plane["static_href"].startswith("/v1/")
        assert plane["width"] == plane["height"] == 512
        assert plane["dtype"] == "float16"
        assert plane["lambda_um"] > 0
        assert plane["lambda_lo_um"] <= plane["lambda_um"] <= plane["lambda_hi_um"]
        assert plane["dlambda_um"] > 0
        assert plane["release"] in {"dr1", "qr3"}
        assert plane["sha256"]
        assert plane["bytes"] > 0


def test_frames_thins_to_the_requested_bin_count(client: TestClient) -> None:
    body = client.get("/api/v1/targets/barnard/frames?mode=epoch&bins=4").json()

    assert body["bins"] == 4
    assert len(body["planes"]) == 4 * 2
    assert len({plane["band"] for plane in body["planes"]}) == 4


def test_frames_never_exceeds_the_sixteen_plane_blink_budget(
    client: TestClient,
) -> None:
    body = client.get("/api/v1/targets/barnard/frames?mode=epoch&bins=16").json()

    assert body["requested_bins"] == 16
    assert body["bins"] == 8
    assert len(body["planes"]) == 16


def test_frames_refuses_a_target_with_only_one_epoch(client: TestClient) -> None:
    response = client.get("/api/v1/targets/wise0855/frames?mode=epoch&bins=8")

    assert response.status_code == 409
    assert "one survey epoch" in response.json()["detail"].lower()


@pytest.mark.parametrize("query", ["mode=spectrum&bins=8", "mode=nope&bins=8"])
def test_frames_rejects_unavailable_or_unknown_modes(
    client: TestClient, query: str
) -> None:
    response = client.get(f"/api/v1/targets/barnard/frames?{query}")

    assert response.status_code in {400, 409}
    assert response.json()["detail"]


def test_frames_rejects_a_bin_count_outside_the_budget(client: TestClient) -> None:
    assert (
        client.get("/api/v1/targets/barnard/frames?mode=epoch&bins=0").status_code
        == 422
    )
    assert (
        client.get("/api/v1/targets/barnard/frames?mode=epoch&bins=99").status_code
        == 422
    )


# AC3 -----------------------------------------------------------------------


def test_plane_route_serves_float16_spx1_bytes(client: TestClient) -> None:
    body = client.get("/api/v1/targets/barnard/frames?mode=epoch&bins=8").json()
    plane = body["planes"][0]

    response = client.get(plane["href"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["cache-control"].startswith("public")
    decoded = decode_plane(response.content)
    assert decoded.header.dtype == 1
    assert decoded.header.height == decoded.header.width == 512
    assert decoded.pixels.dtype.name == "float16"
    assert len(response.content) == plane["bytes"]


def test_plane_route_refuses_path_traversal(client: TestClient) -> None:
    body = client.get("/api/v1/manifest").json()
    snapshot_id = body["snapshot_id"]

    response = client.get(
        f"/api/v1/snapshots/{snapshot_id}/planes/%2E%2E%2Fmanifest.json"
    )

    assert response.status_code == 404


# AC5/AC6 helpers -----------------------------------------------------------


def test_target_and_snapshot_ids_are_fixture_marked_in_every_response(
    client: TestClient,
) -> None:
    manifest = client.get("/api/v1/manifest").json()
    targets = client.get("/api/v1/targets").json()
    detail = client.get("/api/v1/targets/barnard").json()

    assert manifest["fixture"] is True
    assert targets["fixture"] is True
    assert detail["fixture"] is True
    assert all(target["fixture"] is True for target in targets["targets"])
    assert json.dumps(detail["snapshot_id"]).startswith('"fixture-')


# Real fixture wiring -------------------------------------------------------


def test_deep_collections_map_to_their_processing_release() -> None:
    assert cli.release_from_collection("spherex_dr1_deep") == "dr1"
    assert cli.release_from_collection("spherex_qr3_deep") == "qr3"
    assert cli.release_from_collection("spherex_dr1") == "dr1"


def test_build_cli_writes_a_real_snapshot_with_egress_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args, **kwargs):
        raise AssertionError("build command attempted network access")

    monkeypatch.setattr("drift.archive.UrllibTransport.get", blocked)

    exit_code = cli.main(
        [
            "build",
            "--fixture",
            str(FIXTURE),
            "--snapshot-dir",
            str(tmp_path),
            "--generated-at",
            "2026-11-15T23:59:00Z",
        ]
    )

    assert exit_code == 0
    pointer = load_latest(tmp_path)
    assert pointer is not None
    assert not pointer.snapshot_id.startswith("fixture-")
    manifest = pointer.load_manifest(tmp_path)
    assert manifest.fixture is False
    assert {target.id for target in manifest.targets} == {
        "bamberga",
        "lpv",
        "barnard",
    }


def test_real_snapshot_is_published_and_every_file_matches_its_hash(
    real_snapshot_root: Path,
) -> None:
    pointer = load_latest(real_snapshot_root)

    assert pointer is not None
    assert not pointer.snapshot_id.startswith("fixture-")
    manifest = pointer.load_manifest(real_snapshot_root)
    assert manifest.schema_version == 1
    assert manifest.fixture is False
    assert manifest.status == "published"
    assert manifest.files
    files_by_path = {entry.path: entry for entry in manifest.files}
    for entry in manifest.files:
        path = real_snapshot_root / entry.path
        assert path.is_file(), entry.path
        assert entry.bytes == path.stat().st_size
        assert entry.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest.watermark
    assert len(str(manifest.watermark.get("rows_sha256"))) == 64
    assert manifest.source
    assert manifest.source["row_counts"]
    labels_by_target = {target.id: set(target.labels) for target in manifest.targets}
    assert "saturated" in labels_by_target["barnard"]
    assert "saturated" not in labels_by_target["bamberga"]
    for target in manifest.targets:
        for visit in target.visits:
            assert visit.release in {"dr1", "qr3"}
            for frame in visit.frames:
                assert frame.source_url
                assert frame.source_url.startswith("https://irsa.ipac.caltech.edu/")
                assert frame.lambda_um and frame.dlambda_um
                assert frame.sha256
                assert frame.saturated is not None
                assert frame.href
                entry = files_by_path[frame.href]
                assert entry.sha256 == frame.sha256
                assert entry.bytes == frame.bytes
        for epoch in target.epochs:
            for frame in epoch.frames:
                assert frame.href
                entry = files_by_path[frame.href]
                assert entry.sha256 == frame.sha256


@pytest.mark.parametrize("target_id", ["bamberga", "lpv", "barnard"])
def test_matched_wavelength_tolerance_and_single_window(
    real_client: TestClient, target_id: str
) -> None:
    response = real_client.get(
        f"/api/v1/targets/{target_id}/frames?mode=timeline&visits=16"
    )

    assert response.status_code == 200
    body = response.json()
    planes = body["planes"]
    assert body["mode"] == "timeline"
    assert len(planes) >= 2
    assert len({plane["window_sha256"] for plane in planes}) == 1
    assert body["window"]["sha256"] == planes[0]["window_sha256"]
    for plane in planes:
        assert plane["visit_id"]
        assert plane["date"].endswith("Z")
        assert isinstance(plane["mjd"], float)
        assert plane["release"] in {"dr1", "qr3"}
        assert plane["detector"] == body["detector"]
        assert plane["lambda_lo_um"] <= plane["lambda_um"] <= plane["lambda_hi_um"]
        assert plane["source_url"].startswith("https://irsa.ipac.caltech.edu/")
    for left in planes:
        for right in planes:
            assert abs(left["lambda_um"] - right["lambda_um"]) <= (
                0.5 * left["dlambda_um"] + 1e-9
            )


def test_real_bamberga_timeline_holds_the_two_dr1_visits(
    real_client: TestClient,
) -> None:
    body = real_client.get(
        "/api/v1/targets/bamberga/frames?mode=timeline&visits=8"
    ).json()

    assert body["visits"] == 2
    assert body["degraded"] is False
    assert {plane["release"] for plane in body["planes"]} == {"dr1"}
    gap = body["planes"][1]["mjd"] - body["planes"][0]["mjd"]
    assert gap == pytest.approx(1.288, abs=0.001)
    assert body["span_days"] == pytest.approx(gap)


def test_real_snapshot_exposes_source_links_in_both_modes(
    real_client: TestClient,
) -> None:
    timeline = real_client.get(
        "/api/v1/targets/lpv/frames?mode=timeline&visits=16"
    ).json()
    compare = real_client.get("/api/v1/targets/barnard/frames?mode=epoch&bins=8").json()

    assert timeline["planes"][0]["source_url"]
    assert len({plane["source_url"] for plane in timeline["planes"]}) > 1
    assert compare["planes"]
    detail = real_client.get("/api/v1/targets/barnard").json()
    assert compare["window"]["sha256"] == detail["window"]["sha256"]
    for plane in compare["planes"]:
        assert plane["source_url"]
        assert plane["release"] in {"dr1", "qr3"}
        assert plane["date"].endswith("Z")
        assert isinstance(plane["mjd"], float)
        assert plane["window_sha256"] == compare["window"]["sha256"]
