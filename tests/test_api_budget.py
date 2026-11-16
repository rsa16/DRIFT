"""Difference, temporal RGB, and the plane/byte budget.

The seams are the public frames response and the small ``Budget`` planner.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from drift.api import Budget, create_app
from drift.config import Settings
from drift.cubes import DifferenceStack
from drift.ingest.synthetic import build_synthetic_snapshot
from drift.planes import decode_plane, encode_plane


@pytest.fixture(scope="session")
def snapshot_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("budget-snapshots")
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


def _frames(client: TestClient, query: str):
    return client.get(f"/api/v1/targets/{query}")


# Difference ----------------------------------------------------------------


def test_diff_returns_float16_difference_planes_on_the_one_window(
    client: TestClient,
) -> None:
    timeline = _frames(client, "barnard/frames?mode=timeline&visits=8").json()
    reference = timeline["planes"][0]
    current = timeline["planes"][1]

    response = _frames(
        client,
        f"barnard/frames?mode=diff&band={timeline['band']}&reference=0",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "diff"
    assert body["reference"]["visit_id"] == reference["visit_id"]
    assert body["plane_count"] == len(body["planes"])
    assert len(body["planes"]) == len(timeline["planes"]) - 1
    assert {plane["window_sha256"] for plane in body["planes"]} == {
        body["window"]["sha256"]
    }
    assert body["window"]["sha256"] == timeline["window"]["sha256"]

    expected = reference["sigma"] ** 2 + current["sigma"] ** 2
    for plane in body["planes"]:
        assert plane["difference"] is True
        assert plane["reference_visit_id"] == reference["visit_id"]
        assert plane["dtype"] == "float16"
        assert plane["width"] == plane["height"] == 512
        assert 0.0 <= plane["bandpass_fraction"] <= 0.5
        assert plane["reference_lambda_um"] is not None
        assert plane["sigma_squared"] == pytest.approx(
            plane["source_sigma"] ** 2 + reference["sigma"] ** 2
        )
        assert body["planes"][0]["sigma_squared"] == pytest.approx(expected)

        decoded = decode_plane(client.get(plane["href"]).content)
        assert decoded.header.dtype == 1
        assert decoded.pixels.dtype.name == "float16"
        assert decoded.header.width == decoded.header.height == 512


def test_diff_cancels_a_stationary_star_to_noise(client: TestClient) -> None:
    timeline = _frames(client, "barnard/frames?mode=timeline&visits=8").json()
    band = timeline["band"]
    first = decode_plane(client.get(timeline["planes"][0]["href"]).content)
    second = decode_plane(client.get(timeline["planes"][1]["href"]).content)

    body = _frames(
        client,
        f"barnard/frames?mode=diff&band={band}&reference=0&visit=1",
    ).json()
    assert body["degraded"] is False
    assert body["frames_omitted"] == []
    difference = decode_plane(client.get(body["planes"][0]["href"]).content)

    # The synthetic field's static star at (x=330, y=180) is identical in both
    # visits; only the moving synthetic companion may remain.
    static_box = difference.pixels[175:186, 325:336].astype(np.float64)
    assert np.nanmax(np.abs(static_box)) < 0.25

    # The difference is the current minus the reference, not a shader trick.
    expected = second.pixels.astype(np.float32) - first.pixels.astype(np.float32)
    observed = difference.pixels.astype(np.float32)
    finite = np.isfinite(expected) & np.isfinite(observed)
    assert np.max(np.abs(observed[finite] - expected[finite])) < 0.01


def test_diff_reference_must_name_a_visit_in_the_stack(client: TestClient) -> None:
    response = _frames(
        client,
        "barnard/frames?mode=diff&band=1&reference=not-a-visit",
    )

    assert response.status_code in {400, 409}
    assert "reference" in response.json()["detail"].lower()


def test_difference_adds_per_pixel_variance_maps() -> None:
    reference = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
    current = np.array([[1.5, 2.0], [2.0, 4.5]], dtype=np.float32)
    reference_variance = np.full((2, 2), 0.04, dtype=np.float32)
    current_variance = np.full((2, 2), 0.09, dtype=np.float32)

    result = DifferenceStack(reference_sigma=0.2, current_sigma=0.3).difference(
        reference,
        current,
        reference_variance=reference_variance,
        current_variance=current_variance,
    )

    assert result.sigma_squared == pytest.approx(0.2**2 + 0.3**2)
    assert result.variance is not None
    assert np.allclose(result.variance, 0.13)
    assert np.allclose(result.image, current - reference)

    masked = reference.copy()
    masked[0, 0] = np.nan
    derivative = DifferenceStack(reference_sigma=0.2, current_sigma=0.3).difference(
        masked,
        current,
        reference_variance=reference_variance,
        current_variance=current_variance,
    )
    assert derivative.variance is not None
    assert np.isnan(derivative.image[0, 0])
    assert np.isnan(derivative.variance[0, 0])


def test_diff_resolves_visit_ids_after_timeline_thinning(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    _publish(root, _many_visit_manifest(root))
    client = _client_for(root)

    body = client.get(
        "/api/v1/targets/many/frames?mode=diff"
        "&band=1&reference=dr1-0-D4&visit=dr1-19-D4"
    ).json()

    assert body["reference"]["visit_id"] == "dr1-0-D4"
    assert body["planes"][0]["visit_id"] == "dr1-19-D4"
    assert body["degraded"] is False

    default = client.get("/api/v1/targets/many/frames?mode=diff&band=1").json()
    assert default["plane_count"] == 15
    assert default["reference"]
    assert default["plane_count"] + 1 <= 16
    assert default["degraded"] is True
    assert default["frames_omitted"]


def test_diff_annotates_cross_release_and_combined_quality(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    snapshot_id = "fixture-quality"
    frame = _plane_file(root, snapshot_id, "noise.spx", noise=0.1)
    frame.pop("sigma")
    frame.update({"saturated": True, "flag_bad_frac": 0.25})
    payload = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "fixture": True,
        "status": "fixture",
        "targets": [
            {
                "id": "quality",
                "visits": [
                    {
                        "visit_id": f"{release}-1-D4",
                        "mjd": 61000.0 + index,
                        "date": f"2026-01-0{index + 1}T00:00:00Z",
                        "release": release,
                        "detector": "D4",
                        "frames": [frame],
                    }
                    for index, release in enumerate(("dr1", "qr3"))
                ],
                "timeline": {"default_band": 1, "visits": 2},
                "windows": [{"band": 1, "lo": 0.0, "hi": 2.0, "s": 0.2}],
            }
        ],
        "window": {"lo": 0.0, "hi": 2.0, "s": 0.2},
    }
    _publish(root, payload)
    client = _client_for(root)

    body = client.get(
        "/api/v1/targets/quality/frames?mode=diff&band=1"
        "&reference=dr1-1-D4&visit=qr3-1-D4"
    ).json()

    plane = body["planes"][0]
    assert plane["cross_release"] is True
    assert body["cross_release"] is True
    assert plane["saturated"] is True
    assert plane["reference_saturated"] is True
    assert plane["flag_bad_frac"] == pytest.approx(0.25)
    assert plane["sigma_squared"] > 0
    assert plane["sigma"] > 0


def test_difference_sigma_falls_back_to_the_planes_when_missing() -> None:
    rng = np.random.default_rng(1)
    reference = rng.normal(1.0, 0.1, size=(16, 16)).astype(np.float32)
    current = rng.normal(1.0, 0.1, size=(16, 16)).astype(np.float32)

    result = DifferenceStack().difference(reference, current)

    assert result.sigma_squared > 0
    assert result.sigma > 0


# Temporal RGB --------------------------------------------------------------


def test_rgb_returns_three_visits_from_the_same_band_and_window(
    client: TestClient,
) -> None:
    timeline = _frames(client, "barnard/frames?mode=timeline&visits=8").json()

    response = _frames(
        client,
        f"barnard/frames?mode=rgb&band={timeline['band']}",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "rgb"
    assert body["plane_count"] == len(body["planes"]) == 3
    assert [plane["channel"] for plane in body["planes"]] == ["r", "g", "b"]
    assert len({plane["visit_id"] for plane in body["planes"]}) == 3
    assert {plane["band"] for plane in body["planes"]} == {timeline["band"]}
    assert {plane["window_sha256"] for plane in body["planes"]} == {
        body["window"]["sha256"]
    }
    assert body["window"]["sha256"] == timeline["window"]["sha256"]
    assert body["degraded"] is True
    assert len(body["frames_omitted"]) == 5
    assert response.headers["x-spx-degraded"] == "1"
    for plane in body["planes"]:
        assert plane["dtype"] == "float16"
        assert plane["release"] in {"dr1", "qr3"}
        assert plane["detector"] == timeline["detector"]
        assert plane["date"].endswith("Z")


def test_rgb_needs_three_visits_and_says_why(client: TestClient) -> None:
    response = _frames(client, "bamberga/frames?mode=rgb")

    assert response.status_code == 409
    assert "three" in response.json()["detail"].lower()


# Budget --------------------------------------------------------------------


def _plane_file(
    root: Path,
    snapshot_id: str,
    name: str,
    *,
    value: float = 1.0,
    noise: float = 0.0,
) -> dict[str, object]:
    rng = np.random.default_rng(0)
    pixels = np.full((8, 8), value, dtype=np.float32)
    if noise:
        pixels = pixels + rng.normal(0.0, noise, size=pixels.shape).astype(np.float32)
    body = encode_plane(pixels)
    path = root / "v1" / snapshot_id / "planes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return {
        "band": 1,
        "detector": "D4",
        "href": f"v1/{snapshot_id}/planes/{name}",
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "sigma": 0.02,
    }


def _publish(root: Path, payload: dict) -> None:
    snapshot_id = str(payload["snapshot_id"])
    directory = root / "v1" / snapshot_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(payload))
    (root / "latest.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "manifest": f"v1/{snapshot_id}/manifest.json",
                "published_at": "2026-11-15T00:00:00Z",
            }
        )
    )


def _client_for(root: Path) -> TestClient:
    settings = Settings.from_env(
        {
            "DRIFT_SNAPSHOT_DIR": str(root),
            "DRIFT_CACHE_DIR": str(root / "cache"),
            "DRIFT_ALLOW_FIXTURE": "true",
        }
    )
    return TestClient(create_app(settings))


def _many_visit_manifest(root: Path, count: int = 20) -> dict:
    snapshot_id = "fixture-many"
    frames = [_plane_file(root, snapshot_id, "many.spx")]
    visits = [
        {
            "visit_id": f"dr1-{index}-D4",
            "mjd": 60000.0 + float(index * index),
            "date": f"2026-01-01T00:{index:02d}:00Z",
            "release": "dr1",
            "survey": 1,
            "detector": "D4",
            "frames": frames,
        }
        for index in range(count)
    ]
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "fixture": True,
        "status": "fixture",
        "targets": [
            {
                "id": "many",
                "visits": visits,
                "timeline": {"default_band": 1, "visits": count},
                "windows": [{"band": 1, "lo": 0.0, "hi": 2.0, "s": 0.2}],
            }
        ],
        "window": {"lo": 0.0, "hi": 2.0, "s": 0.2},
    }


def _epoch_manifest(root: Path, epoch_count: int, band_count: int = 8) -> dict:
    snapshot_id = f"fixture-{epoch_count}epochs"
    epochs = []
    for epoch_index in range(epoch_count):
        frames = [
            _plane_file(
                root,
                snapshot_id,
                f"e{epoch_index}_b{band_index}.spx",
                value=float(epoch_index + band_index),
            )
            for band_index in range(1, band_count + 1)
        ]
        for band_index, frame in enumerate(frames, start=1):
            frame.update({"band": band_index, "lambda_um": 1.0 + band_index * 0.01})
        epochs.append(
            {
                "epoch_id": f"e{epoch_index}",
                "release": "dr1" if epoch_index < 2 else "qr3",
                "survey": 1 if epoch_index < 2 else 3,
                "mjd_min": 61000.0 + epoch_index * 100.0,
                "mjd_max": 61010.0 + epoch_index * 100.0,
                "frames": frames,
            }
        )
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "fixture": True,
        "status": "fixture",
        "targets": [
            {
                "id": "multi",
                "epochs": epochs,
                "windows": [{"band": 1, "lo": 0.0, "hi": 2.0, "s": 0.2}],
            }
        ],
        "window": {"lo": 0.0, "hi": 2.0, "s": 0.2},
    }


def test_a_sixteen_plane_timeline_stays_within_the_compressed_budget(
    client: TestClient,
) -> None:
    response = _frames(client, "lpv/frames?mode=timeline&visits=16")

    body = response.json()
    assert body["plane_count"] == len(body["planes"]) <= 16
    assert body["compressed_bytes"] <= 8 * 1024 * 1024


def test_plane_bytes_are_gzip_encoded_when_the_browser_accepts_it(
    client: TestClient,
) -> None:
    body = _frames(client, "barnard/frames?mode=timeline&visits=8").json()
    plane = body["planes"][0]

    response = client.get(plane["href"], headers={"accept-encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert decode_plane(response.content).header.dtype == 1


def test_an_over_budget_visit_stack_thins_uniformly_over_mjd_and_says_so(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshots"
    _publish(root, _many_visit_manifest(root))
    client = _client_for(root)

    response = client.get("/api/v1/targets/many/frames?mode=timeline&visits=16")

    assert response.status_code == 200
    body = response.json()
    assert body["plane_count"] == body["visits"] == 16
    assert body["available_visits"] == 20
    assert body["degraded"] is True
    assert len(body["frames_omitted"]) == 4
    assert response.headers["x-spx-degraded"] == "1"

    # Index thinning would stop at visit 15 and never reach the span's end;
    # thinning over MJD reaches the last visit and samples the time axis.
    assert body["planes"][0]["visit_id"] == "dr1-0-D4"
    assert body["planes"][-1]["visit_id"] == "dr1-19-D4"
    mjds = [plane["mjd"] for plane in body["planes"]]
    assert mjds == sorted(mjds)
    assert len(set(mjds)) == 16


@pytest.mark.parametrize(
    ("epoch_count", "bins", "planes"),
    [(2, 8, 16), (3, 5, 15), (4, 4, 16)],
)
def test_survey_compare_adapts_bins_to_the_epoch_count(
    tmp_path: Path, epoch_count: int, bins: int, planes: int
) -> None:
    root = tmp_path / "snapshots"
    _publish(root, _epoch_manifest(root, epoch_count))
    client = _client_for(root)

    response = client.get("/api/v1/targets/multi/frames?mode=epoch&bins=16&epochs=all")

    assert response.status_code == 200
    body = response.json()
    assert body["bins"] == bins
    assert body["plane_count"] == planes <= 16
    assert body["compressed_bytes"] <= 8 * 1024 * 1024


def test_requesting_fewer_bins_is_not_degraded(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    _publish(root, _epoch_manifest(root, 2))
    client = _client_for(root)

    response = client.get("/api/v1/targets/multi/frames?mode=epoch&bins=4")

    body = response.json()
    assert body["plane_count"] == 8
    assert body["degraded"] is False
    assert body["frames_omitted"] == []
    assert response.headers["x-spx-degraded"] == "0"


def test_more_than_sixteen_epochs_are_capped_and_reported(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    _publish(root, _epoch_manifest(root, 17, band_count=1))
    client = _client_for(root)

    response = client.get("/api/v1/targets/multi/frames?mode=epoch&bins=16&epochs=all")

    body = response.json()
    assert body["plane_count"] == 16
    assert body["degraded"] is True
    assert "epoch e16" in body["frames_omitted"]
    assert response.headers["x-spx-degraded"] == "1"


def test_thinning_happens_before_any_resolution_drop() -> None:
    budget = Budget(max_compressed_bytes=1_000)

    planned = budget.plan(
        available=20,
        requested=16,
        compressed_at_full_resolution=10_000_000,
    )

    assert planned.count == 16
    assert planned.thinned is True
    assert planned.resolution_reduced is True
    assert planned.resolution < 512
    # Resolution alone must not reduce the thinned plane count.
    assert planned.count == min(16, budget.max_planes)


def test_a_four_epoch_stack_never_exceeds_the_plane_budget(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    _publish(root, _epoch_manifest(root, 4))
    client = _client_for(root)

    response = client.get("/api/v1/targets/multi/frames?mode=epoch&bins=16&epochs=all")

    body = response.json()
    assert body["plane_count"] <= 16
    assert body["bins"] * len(body["epochs"]) == body["plane_count"]
    assert body["compressed_bytes"] <= 8 * 1024 * 1024
    assert body["degraded"] is True
    assert body["frames_omitted"]
    assert response.headers["x-spx-degraded"] == "1"
