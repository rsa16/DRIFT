from __future__ import annotations

import http.server
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from drift.api import create_app
from drift.archive import HttpResponse, NoRedirectTransport
from drift.config import Settings
from drift.ingest.synthetic import build_synthetic_snapshot
from drift.live import LiveError, _stretch_window
from drift.planes import decode_plane

FIXTURE = Path(__file__).parent / "fixtures" / "archive"
OPERATOR_BASE = "https://operator.example.invalid"
EMPTY_CSV = b"obs_publisher_did,s_ra,s_dec\n"
BARNARD_RA = 269.4462386
BARNARD_DEC = 4.7686521


class FakeTransport:
    """Count every upstream request and serve a canned body per URL."""

    def __init__(self, responder: Callable[[str], bytes]) -> None:
        self.responder = responder
        self.urls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def get(self, url: str, timeout: float) -> HttpResponse:
        with self.lock:
            self.urls.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            body = self.responder(url)
        finally:
            with self.lock:
                self.active -= 1
        return HttpResponse(url=url, status=200, body=body)


class SlowTransport(FakeTransport):
    def __init__(self, delay_s: float) -> None:
        super().__init__(lambda url: EMPTY_CSV)
        self.delay_s = delay_s

    def get(self, url: str, timeout: float) -> HttpResponse:
        time.sleep(self.delay_s)
        return super().get(url, timeout)


class BrokenTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__(lambda url: EMPTY_CSV)

    def get(self, url: str, timeout: float) -> HttpResponse:
        with self.lock:
            self.urls.append(url)
        raise RuntimeError("secret upstream failure")


class PartialTransport(FakeTransport):
    """Recorded DR1 data, but both QR3 collections fail."""

    def __init__(self) -> None:
        recorded = _recorded_transport()
        super().__init__(recorded.responder)

    def get(self, url: str, timeout: float) -> HttpResponse:
        if "/SIA?" in url and _collection(url).startswith("spherex_qr3"):
            with self.lock:
                self.urls.append(url)
            raise OSError("qr3 unavailable")
        return super().get(url, timeout)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "DRIFT_SNAPSHOT_DIR": str(tmp_path / "snapshots"),
        "DRIFT_CACHE_DIR": str(tmp_path / "cache"),
        "DRIFT_LIVE_ENABLED": "true",
        "DRIFT_IRSA_BASE": OPERATOR_BASE,
        "DRIFT_LIVE_TIMEOUT_S": "5",
    }
    values.update(overrides)
    return Settings.from_env({key: str(value) for key, value in values.items()})


def _client(
    tmp_path: Path,
    transport: FakeTransport | None = None,
    **overrides: object,
) -> TestClient:
    return TestClient(
        create_app(_settings(tmp_path, **overrides), live_transport=transport)
    )


def _fixture_csv(collection: str) -> bytes:
    name = (
        "barnard__spherex_qr3.csv"
        if collection == "spherex_qr3"
        else "barnard__spherex_dr1.csv"
    )
    return (FIXTURE / "sia" / name).read_bytes()


def _collection(url: str) -> str:
    query = url.split("?", 1)[1]
    for item in query.split("&"):
        key, _, value = item.partition("=")
        if key == "COLLECTION":
            return value
    return ""


def _recorded_transport() -> FakeTransport:
    cutout = (FIXTURE / "cutouts" / "barnard_dr1_0371_2.fits").read_bytes()

    def respond(url: str) -> bytes:
        if "/SIA?" in url:
            return _fixture_csv(_collection(url))
        if "/ibe/data/" in url:
            return cutout
        raise AssertionError(f"unexpected upstream URL {url}")

    return FakeTransport(respond)


# AC1 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "ra=400&dec=0&fov=0.4",
        "ra=-1&dec=0&fov=0.4",
        "ra=10&dec=-91&fov=0.4",
        "ra=10&dec=91&fov=0.4",
        "ra=10&dec=0&fov=0",
        "ra=10&dec=0&fov=999",
        "ra=10&dec=0&fov=0.4&bins=5",
        "ra=10&dec=0&fov=0.4&bins=0",
        "ra=10&dec=0&fov=0.4&bins=17",
        "ra=10&dec=0&fov=0.4&target=not-a-target",
        "ra=10&dec=0&fov=999&target=barnard",
        "ra=400&dec=0&fov=0.4&target=barnard",
        "ra=not-a-number&dec=0&fov=0.4",
        "dec=0&fov=0.4",
    ],
)
def test_resolve_rejections_return_400_with_zero_upstream_requests(
    tmp_path: Path, query: str
) -> None:
    transport = FakeTransport(lambda url: EMPTY_CSV)
    client = _client(tmp_path, transport)

    response = client.get(f"/api/v1/resolve?{query}")

    assert response.status_code == 400, response.text
    assert "Traceback" not in response.text
    assert transport.urls == []


# AC2 -----------------------------------------------------------------------


def test_resolve_forwards_only_validated_floats_to_the_configured_base(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(lambda url: EMPTY_CSV)
    client = _client(tmp_path, transport)

    response = client.get(
        "/api/v1/resolve?ra=269.446&dec=4.769&fov=0.4&bins=2"
        "&url=http%3A%2F%2Fevil.invalid&host=evil.invalid&path=../../etc/passwd"
    )

    assert response.status_code == 200, response.text
    assert transport.urls
    for url in transport.urls:
        assert url.startswith(f"{OPERATOR_BASE}/SIA?")
        assert "evil" not in url
        assert "passwd" not in url
        assert "269.446" in url or "269.4460" in url
    assert {_collection(url) for url in transport.urls} <= {
        "spherex_dr1",
        "spherex_dr1_deep",
        "spherex_qr3",
        "spherex_qr3_deep",
    }


def test_operator_base_is_the_only_configured_host(tmp_path: Path) -> None:
    transport = FakeTransport(lambda url: EMPTY_CSV)
    client = _client(
        tmp_path,
        transport,
        DRIFT_IRSA_BASE="https://archive.example.invalid/prefix",
    )

    client.get("/api/v1/resolve?ra=269.446&dec=4.769&fov=0.4")

    assert transport.urls
    assert all(
        url.startswith("https://archive.example.invalid/prefix/SIA?")
        for url in transport.urls
    )


def test_forwarded_clients_get_independent_rate_budgets_when_configured(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(lambda url: EMPTY_CSV)
    app = create_app(
        _settings(tmp_path, DRIFT_TRUSTED_PROXY_HOPS="1"),
        live_transport=transport,
    )
    client = TestClient(app, client=("127.0.0.1", 50000))
    first = {"x-forwarded-for": "203.0.113.1"}
    second = {"x-forwarded-for": "203.0.113.2"}

    for index in range(6):
        response = client.get(
            f"/api/v1/resolve?ra={10 + index}&dec=0&fov=0.4&bins=2",
            headers=first,
        )
        assert response.status_code == 200, response.text
    assert (
        client.get(
            "/api/v1/resolve?ra=99&dec=0&fov=0.4&bins=2", headers=first
        ).status_code
        == 429
    )
    assert (
        client.get(
            "/api/v1/resolve?ra=100&dec=0&fov=0.4&bins=2", headers=second
        ).status_code
        == 200
    )


def test_forwarded_headers_are_ignored_by_default(tmp_path: Path) -> None:
    transport = FakeTransport(lambda url: EMPTY_CSV)
    app = create_app(_settings(tmp_path), live_transport=transport)
    client = TestClient(app, client=("127.0.0.1", 50000))

    for index in range(6):
        response = client.get(
            f"/api/v1/resolve?ra={10 + index}&dec=0&fov=0.4&bins=2",
            headers={"x-forwarded-for": f"203.0.113.{index + 1}"},
        )
        assert response.status_code == 200, response.text

    limited = client.get(
        "/api/v1/resolve?ra=99&dec=0&fov=0.4&bins=2",
        headers={"x-forwarded-for": "203.0.113.99"},
    )

    assert limited.status_code == 429


def test_partial_collection_failure_is_reported_as_degraded(tmp_path: Path) -> None:
    client = _client(tmp_path, PartialTransport())

    response = client.get(
        f"/api/v1/resolve?ra={BARNARD_RA}&dec={BARNARD_DEC}&fov=0.4&bins=2"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "ready", body["reason"]
    assert body["degraded"] is True
    assert "did not answer" in body["reason"]
    assert body["survey_compare"]["enabled"] is False
    assert "unverified" in body["survey_compare"]["reason"]
    assert body["frames"]["degraded"] is True


def test_bins_of_one_is_an_unavailable_timeline_not_a_contradiction(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, _recorded_transport())

    response = client.get(
        f"/api/v1/resolve?ra={BARNARD_RA}&dec={BARNARD_DEC}&fov=0.4&bins=1"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "unavailable"
    assert body["timeline"]["enabled"] is False
    assert body["timeline"]["estimated_frames"] == 0
    assert body["timeline"]["default_band"] is None


def test_a_stack_with_no_finite_pixels_raises_a_live_error() -> None:
    with pytest.raises(LiveError, match="no finite pixels"):
        _stretch_window([np.full((2, 2), np.nan)])


# AC3 -----------------------------------------------------------------------


def test_live_path_is_limited_to_six_requests_per_minute(tmp_path: Path) -> None:
    transport = FakeTransport(lambda url: EMPTY_CSV)
    client = _client(tmp_path, transport)

    for index in range(6):
        response = client.get(f"/api/v1/resolve?ra={10 + index}&dec=0&fov=0.4&bins=2")
        assert response.status_code == 200, response.text
    upstream_after_six = len(transport.urls)
    assert upstream_after_six > 0

    limited = client.get("/api/v1/resolve?ra=99&dec=0&fov=0.4&bins=2")

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert len(transport.urls) == upstream_after_six


def test_cached_and_pinned_responses_are_not_rate_limited(tmp_path: Path) -> None:
    transport = _recorded_transport()
    client = _client(tmp_path, transport)
    query = f"ra={BARNARD_RA}&dec={BARNARD_DEC}&fov=0.4&bins=2"

    first = client.get(f"/api/v1/resolve?{query}")
    assert first.status_code == 200, first.text
    upstream = len(transport.urls)

    for _ in range(8):
        cached = client.get(f"/api/v1/resolve?{query}")
        assert cached.status_code == 200, cached.text
        assert cached.json()["cached"] is True
    assert len(transport.urls) == upstream


def test_pinned_targets_are_unlimited_and_make_no_upstream_call(
    tmp_path: Path,
) -> None:
    build_synthetic_snapshot(tmp_path / "snapshots")
    transport = FakeTransport(lambda url: EMPTY_CSV)
    client = _client(
        tmp_path,
        transport,
        DRIFT_ALLOW_FIXTURE="true",
    )

    for _ in range(8):
        response = client.get("/api/v1/resolve?target=barnard&bins=8")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pinned"] is True
        assert body["target_id"] == "barnard"
        assert body["frames"] is None
    assert transport.urls == []


def test_upstream_concurrency_is_capped(tmp_path: Path) -> None:
    transport = SlowTransport(delay_s=0.05)
    client = _client(
        tmp_path,
        transport,
        DRIFT_LIVE_MAX_CONCURRENCY="2",
        DRIFT_LIVE_TIMEOUT_S="5",
    )
    errors: list[BaseException] = []

    def call(index: int) -> None:
        try:
            client.get(f"/api/v1/resolve?ra={20 + index}&dec=0&fov=0.4&bins=2")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert transport.max_active <= 2


# AC4 -----------------------------------------------------------------------


def test_slow_upstream_degrades_before_the_hundred_second_ceiling(
    tmp_path: Path,
) -> None:
    transport = SlowTransport(delay_s=1.0)
    client = _client(
        tmp_path,
        transport,
        DRIFT_LIVE_TIMEOUT_S="0.1",
    )

    started = time.monotonic()
    response = client.get("/api/v1/resolve?ra=10&dec=0&fov=0.4&bins=2")
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "degraded"
    assert body["degraded"] is True
    assert body["frames"] is None
    assert elapsed < 5


def test_unavailable_upstream_is_reported_without_exception_detail(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, BrokenTransport())

    response = client.get("/api/v1/resolve?ra=10&dec=0&fov=0.4&bins=2")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "unavailable"
    assert body["unavailable"] is True
    assert "secret upstream failure" not in response.text
    assert "Traceback" not in response.text


# AC5 -----------------------------------------------------------------------


def test_resolve_is_gated_by_live_enabled(tmp_path: Path) -> None:
    transport = FakeTransport(lambda url: EMPTY_CSV)
    client = _client(tmp_path, transport, DRIFT_LIVE_ENABLED="false")

    response = client.get("/api/v1/resolve?ra=10&dec=0&fov=0.4&bins=2")

    assert response.status_code == 404
    assert transport.urls == []


def test_live_transport_refuses_to_follow_a_redirect() -> None:
    hits: list[str] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"internal bytes")

        def log_message(self, *args: object) -> None:
            pass

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_port}/secret"
            )
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
    redirect = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        response = NoRedirectTransport().get(
            f"http://127.0.0.1:{redirect.server_port}/go", 2.0
        )
    finally:
        for server in (target, redirect):
            server.shutdown()
            server.server_close()

    assert response.status == 302
    assert hits == []


# AC6 and exit behaviour ----------------------------------------------------


def test_resolve_reports_timeline_and_compare_eligibility(tmp_path: Path) -> None:
    transport = FakeTransport(lambda url: EMPTY_CSV)
    client = _client(tmp_path, transport)

    body = client.get("/api/v1/resolve?ra=10&dec=0&fov=0.4&bins=2").json()

    assert body["state"] == "unavailable"
    assert body["timeline"]["enabled"] is False
    assert body["timeline"]["visits"] == 0
    assert body["timeline"]["estimated_frames"] == 0
    assert body["survey_compare"]["enabled"] is False
    assert "cover" in body["reason"].lower()


def test_live_timeline_returns_playable_planes(tmp_path: Path) -> None:
    transport = _recorded_transport()
    client = _client(tmp_path, transport)

    response = client.get(
        f"/api/v1/resolve?ra={BARNARD_RA}&dec={BARNARD_DEC}&fov=0.4&bins=2"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "ready", body["reason"]
    assert body["degraded"] is False
    assert body["timeline"]["enabled"] is True
    assert body["timeline"]["visits"] >= 2
    assert body["timeline"]["default_band"] == 1
    assert body["timeline"]["estimated_frames"] <= 2
    assert body["survey_compare"]["enabled"] is True
    frames = body["frames"]
    assert frames is not None
    assert 2 <= len(frames["planes"]) <= 2
    assert frames["mode"] == "timeline"
    assert frames["window"]["sha256"]
    assert {plane["window_sha256"] for plane in frames["planes"]} == {
        frames["window"]["sha256"]
    }
    assert frames["visits"] == len(frames["planes"])

    plane = client.get(frames["planes"][0]["href"])
    assert plane.status_code == 200
    decoded = decode_plane(plane.content)
    assert decoded.header.width == decoded.header.height == 512


def test_route_scan_finds_no_exception_detail_in_responses(tmp_path: Path) -> None:
    transport = BrokenTransport()
    app = create_app(_settings(tmp_path), live_transport=transport)
    client = TestClient(app, raise_server_exceptions=False)

    @app.get("/api/v1/boom-live")
    def boom() -> None:
        raise RuntimeError("secret internal failure")

    responses = [
        client.get("/api/v1/resolve?ra=10&dec=0&fov=0.4"),
        client.get("/api/v1/resolve?ra=400&dec=0&fov=0.4"),
        client.get("/api/v1/boom-live"),
    ]

    for response in responses:
        assert "Traceback" not in response.text
        assert "secret internal failure" not in response.text
        assert "secret upstream failure" not in response.text


def test_live_manifest_advertises_live_enabled(tmp_path: Path) -> None:
    build_synthetic_snapshot(tmp_path / "snapshots")
    client = _client(
        tmp_path,
        FakeTransport(lambda url: EMPTY_CSV),
        DRIFT_ALLOW_FIXTURE="true",
        DRIFT_LIVE_MAX_FOV_DEG="2.0",
    )
    body = client.get("/api/v1/manifest").json()

    assert body["live_enabled"] is True
    assert body["live_max_fov_deg"] == 2.0


def test_spatial_index_setting_is_optional_and_expands_a_user_path() -> None:
    settings = Settings.from_env({"DRIFT_SPATIAL_INDEX": "~/indexes/s3.sqlite"})

    assert settings.spatial_index == Path("~/indexes/s3.sqlite").expanduser()
    assert Settings.from_env({}).spatial_index is None


def test_unreadable_spatial_index_falls_back_to_the_sia_path(tmp_path: Path) -> None:
    transport = FakeTransport(lambda url: EMPTY_CSV)
    client = _client(
        tmp_path,
        transport,
        DRIFT_SPATIAL_INDEX=str(tmp_path / "missing.sqlite"),
    )

    response = client.get("/api/v1/resolve?ra=10&dec=0&fov=0.4&bins=2")

    assert response.status_code == 200, response.text
    assert transport.urls
    assert all("/SIA?" in url for url in transport.urls)
