from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import pytest
from astropy.io import fits  # type: ignore[import-untyped]
from fastapi.testclient import TestClient

from drift.api import create_app
from drift.archive import HttpResponse
from drift.config import Settings
from drift.ingest.index import (
    SpatialIndex,
    build_index,
    parse_image_header,
    read_header_entry,
)

FIXTURE = Path(__file__).parent / "fixtures" / "archive"
BUCKET = "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com"
PREFIX = "dr1/level2/2026W12_2A/"
BARNARD_RA = 269.4462386
BARNARD_DEC = 4.7686521
EMPTY_CSV = b"obs_publisher_did,s_ra,s_dec\n"


def _fits_body(
    *,
    ra: float,
    dec: float,
    detector: str = "1",
    mjd: float = 61118.512702,
    date_obs: str = "2026-03-19T12:17:19.890",
    naxis: int = 2040,
) -> bytes:
    """Synthesize a FITS file with a one-block primary and an IMAGE header."""
    primary = fits.Header()
    primary["SIMPLE"] = True
    primary["BITPIX"] = 8
    primary["NAXIS"] = 0
    primary["EXTEND"] = True

    image = fits.Header()
    image["XTENSION"] = "IMAGE"
    image["BITPIX"] = -32
    image["NAXIS"] = 2
    image["NAXIS1"] = naxis
    image["NAXIS2"] = naxis
    image["CTYPE1"] = "RA---TAN"
    image["CTYPE2"] = "DEC--TAN"
    image["CRVAL1"] = ra
    image["CRVAL2"] = dec
    image["CRPIX1"] = naxis / 2.0 + 0.5
    image["CRPIX2"] = naxis / 2.0 + 0.5
    image["CDELT1"] = -6.158 / 3600.0
    image["CDELT2"] = 6.158 / 3600.0
    image["PC1_1"] = 1.0
    image["PC1_2"] = 0.0
    image["PC2_1"] = 0.0
    image["PC2_2"] = 1.0
    image["DETECTOR"] = detector
    image["MJD-AVG"] = mjd
    image["DATE-OBS"] = date_obs
    return (primary.tostring() + image.tostring()).encode("ascii")


def _listing_xml(keys: list[str], *, truncated: bool, token: int) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>",
    ]
    if truncated:
        parts.append(f"<NextContinuationToken>{token}</NextContinuationToken>")
    for key in keys:
        parts.append(f"<Contents><Key>{escape(key)}</Key></Contents>")
    parts.append("</ListBucketResult>")
    return "".join(parts).encode("utf-8")


class FakeS3Transport:
    """A fake anonymous S3 transport: LIST pages and byte-range GETs."""

    def __init__(
        self,
        objects: dict[str, bytes],
        *,
        page_size: int = 100,
        fail_keys: set[str] | None = None,
    ) -> None:
        self.objects = dict(objects)
        self.page_size = page_size
        self.fail_keys = set(fail_keys or ())
        self.list_calls: list[str] = []
        self.range_calls: list[str] = []

    def get(self, url: str, timeout: float) -> HttpResponse:
        split = urlsplit(url)
        assert f"{split.scheme}://{split.netloc}" == BUCKET
        self.list_calls.append(url)
        query = parse_qs(split.query)
        prefix = query.get("prefix", [""])[0]
        token = int(query.get("continuation-token", ["0"])[0])
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        page = keys[token : token + self.page_size]
        truncated = token + self.page_size < len(keys)
        body = _listing_xml(page, truncated=truncated, token=token + self.page_size)
        return HttpResponse(url=url, status=200, body=body)

    def get_range(
        self, url: str, *, start: int, end: int, timeout: float
    ) -> HttpResponse:
        split = urlsplit(url)
        assert f"{split.scheme}://{split.netloc}" == BUCKET
        key = unquote(split.path.lstrip("/"))
        self.range_calls.append(key)
        if key in self.fail_keys:
            return HttpResponse(url=url, status=500, body=b"")
        return HttpResponse(
            url=url, status=206, body=self.objects[key][start : end + 1]
        )


class FakeCutoutTransport:
    """Serve cutout bytes for index-issued URLs and refuse every SIA request."""

    def __init__(self, cutout: bytes) -> None:
        self.cutout = cutout
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> HttpResponse:
        self.urls.append(url)
        if "/SIA?" in url:
            raise AssertionError("the index path must not call SIA")
        if "/ibe/data/" in url:
            return HttpResponse(url=url, status=200, body=self.cutout)
        raise AssertionError(f"unexpected upstream URL {url}")


class RecordingTransport:
    def __init__(self, body: bytes = EMPTY_CSV) -> None:
        self.body = body
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> HttpResponse:
        self.urls.append(url)
        return HttpResponse(url=url, status=200, body=self.body)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "DRIFT_SNAPSHOT_DIR": str(tmp_path / "snapshots"),
        "DRIFT_CACHE_DIR": str(tmp_path / "cache"),
        "DRIFT_LIVE_ENABLED": "true",
        "DRIFT_LIVE_TIMEOUT_S": "10",
    }
    values.update(overrides)
    return Settings.from_env({key: str(value) for key, value in values.items()})


def _objects() -> dict[str, bytes]:
    return {
        f"{PREFIX}v1/1/l2b-test-0000.fits": _fits_body(ra=10.0, dec=20.0, mjd=61100.1),
        f"{PREFIX}v1/1/l2b-test-0001.fits": _fits_body(ra=12.0, dec=20.0, mjd=61101.2),
        f"{PREFIX}v1/1/l2b-test-0002.fits": _fits_body(ra=14.0, dec=20.0, mjd=61102.3),
    }


# AC1 -----------------------------------------------------------------------


def test_image_header_parse_extracts_wcs_detector_mjd_date_and_key() -> None:
    objects = _objects()
    key = next(iter(objects))
    transport = FakeS3Transport(objects)

    entry = read_header_entry(transport, key)

    assert entry.key == key
    assert entry.release == "dr1"
    assert entry.detector == "1"
    assert entry.mjd == pytest.approx(61100.1)
    assert entry.date_obs == "2026-03-19T12:17:19.890"
    assert entry.ra == pytest.approx(10.0)
    assert entry.dec == pytest.approx(20.0)
    assert entry.naxis1 == 2040
    assert entry.contains(10.0, 20.0)
    assert not entry.contains(50.0, 20.0)
    assert transport.range_calls == [key]


def test_header_parse_rejects_a_truncated_range_read() -> None:
    with pytest.raises(ValueError, match="END"):
        parse_image_header(b" " * 2880)


def test_index_build_writes_one_row_per_exposure_with_wcs_columns(
    tmp_path: Path,
) -> None:
    objects = _objects()
    index_path = tmp_path / "index.sqlite"

    report = build_index(
        index_path,
        release="dr1",
        prefix=PREFIX,
        transport=FakeS3Transport(objects),
    )

    assert report.discovered == 3
    assert report.read == 3
    assert report.indexed == 3
    assert report.skipped == 0
    assert report.failed == 0
    assert index_path.is_file()

    connection = sqlite3.connect(index_path)
    try:
        rows = connection.execute(
            "SELECT key, crval1, crpix1, cdelt1, pc1_1, ctype1, detector, "
            "mjd, date_obs FROM exposures ORDER BY key"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 3
    first = rows[0]
    assert first[0].endswith("l2b-test-0000.fits")
    assert first[1] == pytest.approx(10.0)
    assert first[2] == pytest.approx(1020.5)
    assert first[3] == pytest.approx(-6.158 / 3600.0)
    assert first[4] == pytest.approx(1.0)
    assert first[5] == "RA---TAN"
    assert first[6] == "1"
    assert first[7] == pytest.approx(61100.1)
    assert first[8] == "2026-03-19T12:17:19.890"


def test_rerunning_the_index_skips_already_indexed_keys(tmp_path: Path) -> None:
    objects = _objects()
    index_path = tmp_path / "index.sqlite"
    first_transport = FakeS3Transport(objects)
    build_index(
        index_path,
        release="dr1",
        prefix=PREFIX,
        transport=first_transport,
    )
    reads_after_first = len(first_transport.range_calls)

    second_transport = FakeS3Transport(objects)
    report = build_index(
        index_path,
        release="dr1",
        prefix=PREFIX,
        transport=second_transport,
    )

    assert report.discovered == 3
    assert report.read == 0
    assert report.indexed == 0
    assert report.skipped == 3
    assert second_transport.range_calls == []
    assert reads_after_first == 3
    connection = sqlite3.connect(index_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM exposures").fetchone()[0] == 3
    finally:
        connection.close()


def test_failed_range_reads_are_counted_and_do_not_block_the_batch(
    tmp_path: Path,
) -> None:
    objects = _objects()
    failed_key = f"{PREFIX}v1/1/l2b-test-0001.fits"

    report = build_index(
        tmp_path / "index.sqlite",
        release="dr1",
        prefix=PREFIX,
        transport=FakeS3Transport(objects, fail_keys={failed_key}),
    )

    assert report.discovered == 3
    assert report.read == 2
    assert report.indexed == 2
    assert report.failed == 1


def test_index_rejects_a_prefix_outside_the_release_or_fixed_bucket(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="prefix"):
        build_index(
            tmp_path / "index.sqlite",
            release="dr1",
            prefix="qr3/level2/2026W12_2A/",
            transport=FakeS3Transport({}),
        )
    with pytest.raises(ValueError, match="prefix"):
        build_index(
            tmp_path / "index.sqlite",
            release="dr1",
            prefix="https://evil.invalid/dr1/level2/",
            transport=FakeS3Transport({}),
        )


# AC2 -----------------------------------------------------------------------


def test_point_query_returns_containing_exposures_quickly(tmp_path: Path) -> None:
    objects = _objects()
    index_path = tmp_path / "index.sqlite"
    build_index(
        index_path,
        release="dr1",
        prefix=PREFIX,
        transport=FakeS3Transport(objects),
    )
    index = SpatialIndex(index_path)

    started = time.perf_counter()
    hits = index.query(10.0, 20.0)
    elapsed = time.perf_counter() - started

    assert [entry.key for entry in hits] == [next(iter(objects))]
    assert index.query(100.0, -40.0) == []
    assert elapsed < 0.1
    index.close()


def test_point_query_applies_a_small_margin_for_corner_only_bboxes(
    tmp_path: Path,
) -> None:
    objects = {
        f"{PREFIX}v1/1/l2b-test-0000.fits": _fits_body(ra=10.0, dec=20.0),
    }
    index_path = tmp_path / "index.sqlite"
    build_index(
        index_path,
        release="dr1",
        prefix=PREFIX,
        transport=FakeS3Transport(objects),
    )
    index = SpatialIndex(index_path)
    entry = index.query(10.0, 20.0)[0]

    assert entry.contains(entry.ra_center + entry.ra_hi + 0.01, 20.0, margin=0.05)
    assert not entry.contains(entry.ra_center + entry.ra_hi + 0.01, 20.0, margin=0.0)
    index.close()


# AC3 -----------------------------------------------------------------------


def _barnard_objects() -> dict[str, bytes]:
    return {
        f"{PREFIX}v1/1/l2b-test-0000.fits": _fits_body(
            ra=BARNARD_RA, dec=BARNARD_DEC, mjd=61100.1
        ),
        f"{PREFIX}v1/1/l2b-test-0001.fits": _fits_body(
            ra=BARNARD_RA + 0.1, dec=BARNARD_DEC, mjd=61101.2
        ),
    }


def test_resolve_with_a_spatial_index_makes_no_sia_request(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    build_index(
        index_path,
        release="dr1",
        prefix=PREFIX,
        transport=FakeS3Transport(_barnard_objects()),
    )
    cutout = (FIXTURE / "cutouts" / "barnard_dr1_0371_2.fits").read_bytes()
    transport = FakeCutoutTransport(cutout)
    client = TestClient(
        create_app(
            _settings(tmp_path, DRIFT_SPATIAL_INDEX=str(index_path)),
            live_transport=transport,
        )
    )

    response = client.get(
        f"/api/v1/resolve?ra={BARNARD_RA}&dec={BARNARD_DEC}&fov=0.4&bins=2"
        "&url=http%3A%2F%2Fevil.invalid&host=evil.invalid&key=evil.fits"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "ready", body["reason"]
    assert body["timeline"]["enabled"] is True
    assert body["frames"] is not None
    assert len(body["frames"]["planes"]) == 2
    assert all("/SIA?" not in url for url in transport.urls)
    for url in transport.urls:
        assert url.startswith("https://irsa.ipac.caltech.edu/ibe/data/spherex/")
        assert "evil" not in url


def test_index_backed_resolve_returns_no_visits_for_an_uncovered_position(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    build_index(
        index_path,
        release="dr1",
        prefix=PREFIX,
        transport=FakeS3Transport(_barnard_objects()),
    )
    transport = FakeCutoutTransport(b"")
    client = TestClient(
        create_app(
            _settings(tmp_path, DRIFT_SPATIAL_INDEX=str(index_path)),
            live_transport=transport,
        )
    )

    response = client.get("/api/v1/resolve?ra=10&dec=-40&fov=0.4&bins=2")

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "unavailable"
    assert transport.urls == []


# AC4 -----------------------------------------------------------------------


def test_cutout_bytes_are_cached_so_a_second_resolve_fetches_nothing(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    build_index(
        index_path,
        release="dr1",
        prefix=PREFIX,
        transport=FakeS3Transport(_barnard_objects()),
    )
    cutout = (FIXTURE / "cutouts" / "barnard_dr1_0371_2.fits").read_bytes()
    transport = FakeCutoutTransport(cutout)
    client = TestClient(
        create_app(
            _settings(tmp_path, DRIFT_SPATIAL_INDEX=str(index_path)),
            live_transport=transport,
        )
    )

    first = client.get(
        f"/api/v1/resolve?ra={BARNARD_RA}&dec={BARNARD_DEC}&fov=0.4&bins=2"
    )
    assert first.status_code == 200, first.text
    cutout_requests = [url for url in transport.urls if "/ibe/data/" in url]
    assert len(cutout_requests) == 2

    # A different bins value changes the resolve cache key but not the cutout key.
    second = client.get(
        f"/api/v1/resolve?ra={BARNARD_RA}&dec={BARNARD_DEC}&fov=0.4&bins=4"
    )
    assert second.status_code == 200, second.text
    assert second.json()["cached"] is False
    assert [url for url in transport.urls if "/ibe/data/" in url] == cutout_requests
    cache_files = list((tmp_path / "cache" / "live").glob("*.bin"))
    assert len(cache_files) == 2


# AC5 -----------------------------------------------------------------------


def test_without_an_index_the_existing_sia_path_is_used(tmp_path: Path) -> None:
    transport = RecordingTransport()
    client = TestClient(create_app(_settings(tmp_path), live_transport=transport))

    response = client.get(
        f"/api/v1/resolve?ra={BARNARD_RA}&dec={BARNARD_DEC}&fov=0.4&bins=2"
    )

    assert response.status_code == 200, response.text
    assert any("/SIA?" in url for url in transport.urls)


def test_index_resolve_keeps_validation_and_rate_limit(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    build_index(
        index_path,
        release="dr1",
        prefix=PREFIX,
        transport=FakeS3Transport(_barnard_objects()),
    )
    cutout = (FIXTURE / "cutouts" / "barnard_dr1_0371_2.fits").read_bytes()
    transport = FakeCutoutTransport(cutout)
    client = TestClient(
        create_app(
            _settings(tmp_path, DRIFT_SPATIAL_INDEX=str(index_path)),
            live_transport=transport,
        )
    )

    invalid = client.get("/api/v1/resolve?ra=400&dec=0&fov=0.4&bins=2")
    assert invalid.status_code == 400
    assert transport.urls == []

    for index in range(6):
        response = client.get(
            f"/api/v1/resolve?ra={BARNARD_RA + index * 0.01}&dec={BARNARD_DEC}"
            "&fov=0.4&bins=2"
        )
        assert response.status_code == 200, response.text
    limited = client.get(
        f"/api/v1/resolve?ra={BARNARD_RA + 0.5}&dec={BARNARD_DEC}&fov=0.4&bins=2"
    )
    assert limited.status_code == 429
