from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from drift.archive import (
    ArchiveRequestError,
    ByteFetcher,
    CollectionDiscovery,
    CutoutFetcher,
    DiskCache,
    HttpResponse,
    RetryPolicy,
    Sia2Client,
    append_query,
    fits_hdu_names,
    parse_sia_csv,
    strip_fits_extension,
)
from drift.ingest import cli, targets
from drift.ingest import record as ingest_record
from drift.ingest.fixture import FixtureError, FixtureStore, FixtureTransport

FIXTURE = Path(__file__).parent / "fixtures" / "archive"

CSV_BODY = (
    "obs_publisher_did,access_url,t_min,obs_release_date\n"
    "ivo://irsa.ipac/spherex_dr1?obs/D1,https://example.test/a.fits,60000,2026-10-01\n"
)


class FakeTransport:
    def __init__(self, responses: Sequence[HttpResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url: str, timeout: float) -> HttpResponse:
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(status: int = 200, body: bytes = CSV_BODY.encode()) -> HttpResponse:
    return HttpResponse(url="", status=status, body=body)


def _fake_archive(root: Path, *, with_searches: bool = True) -> FakeTransport:
    """Serve a minimal complete recording plan without touching the network."""
    cube_path = root / "cube" / "lpv_cube_slice.fits"
    cube_path.parent.mkdir(parents=True, exist_ok=True)
    cube_path.write_bytes(
        _fits_hdu(
            _fits_card("SIMPLE", "T"),
            _fits_card("BITPIX", "8"),
            _fits_card("NAXIS", "0"),
        )
        + _fits_hdu(
            _fits_card("XTENSION", "'IMAGE   '"),
            _fits_card("BITPIX", "8"),
            _fits_card("NAXIS", "1"),
            _fits_card("NAXIS1", "4"),
            _fits_card("EXTNAME", "'IMAGE   '"),
            data=b"abcd",
        )
        + _fits_hdu(
            _fits_card("XTENSION", "'IMAGE   '"),
            _fits_card("BITPIX", "8"),
            _fits_card("NAXIS", "1"),
            _fits_card("NAXIS1", "4"),
            _fits_card("EXTNAME", "'SPECTRAL_CHANNELS'"),
            data=b"wxyz",
        )
    )
    cutout = _fits_hdu(
        _fits_card("SIMPLE", "T"),
        _fits_card("BITPIX", "8"),
        _fits_card("NAXIS", "0"),
    ) + _fits_hdu(
        _fits_card("XTENSION", "'IMAGE   '"),
        _fits_card("BITPIX", "8"),
        _fits_card("NAXIS", "1"),
        _fits_card("NAXIS1", "4"),
        _fits_card("EXTNAME", "'IMAGE   '"),
        data=b"abcd",
    )

    searches: list[bytes] = []
    for spec in targets.FIXTURE_SEARCHES:
        rows = [
            (cutout.filename, cutout.target_mjd, cutout.band)
            for cutout in targets.FIXTURE_CUTOUTS
            if cutout.target == spec.target and cutout.collection == spec.collection
        ]
        if not rows:
            band = "D1" if spec.target == "bamberga" else "D3"
            rows = [(f"{spec.target}_{spec.collection}", 61000.0, band)]
        body = (
            "obs_publisher_did,access_url,t_min,obs_release_date,"
            "obs_collection,energy_bandpassname\n"
            + "".join(
                f"ivo://example/{spec.target}/{filename},"
                f"https://example.test/{filename}.fits,"
                f"{mjd},2026-01-01,{spec.collection},SPHEREx-{band}\n"
                for filename, mjd, band in rows
            )
        )
        searches.append(body.encode())
    discovery_body = (
        "collection,release_date\n"
        + "".join(
            f"{name},{release}\n"
            for name, release in targets.SEED_COLLECTIONS
            if name.startswith("spherex_")
        )
    ).encode()
    search_responses = [response(body=body) for body in searches]
    return FakeTransport(
        (search_responses if with_searches else [])
        + [response(body=cutout) for _ in targets.FIXTURE_CUTOUTS]
        + [response(body=discovery_body)]
    )


def test_search_sends_collection_and_never_falls_back() -> None:
    transport = FakeTransport([response()])
    client = Sia2Client(transport=transport)

    rows = client.search("spherex_dr1", pos=(269.4, 4.69), size_deg=0.01)

    assert rows[0]["obs_publisher_did"].endswith("/D1")
    query = parse_qs(urlsplit(transport.calls[0]).query)
    assert query["COLLECTION"] == ["spherex_dr1"]
    assert query["POS"] == ["circle 269.4 4.69 0.01"]
    assert query["RESPONSEFORMAT"] == ["csv"]

    empty = FakeTransport([])
    with pytest.raises(ValueError, match="COLLECTION"):
        Sia2Client(transport=empty).search("")
    assert empty.calls == []


def test_retry_at_1_2_4_8_logs_every_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport(
        [response(503, b"Server busy") for _ in range(4)] + [response()]
    )
    sleeps: list[float] = []
    client = Sia2Client(transport=transport, sleeper=sleeps.append)

    with caplog.at_level(logging.INFO):
        rows = client.search("spherex_dr1", pos=(1.0, 2.0))

    assert rows
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert len(transport.calls) == 5
    lines = [record.message for record in caplog.records]
    assert len(lines) == 5
    for retry, line in enumerate(lines[:-1]):
        assert "status=503" in line
        assert f"retries={retry}" in line
        assert "latency_ms=" in line
    assert "status=200" in lines[-1]
    assert "retries=4" in lines[-1]


def test_exhaustion_fails_loudly_not_empty() -> None:
    transport = FakeTransport([response(503, b"Server busy") for _ in range(5)])
    sleeps: list[float] = []
    client = Sia2Client(transport=transport, sleeper=sleeps.append)

    with pytest.raises(ArchiveRequestError) as caught:
        client.search("spherex_dr1", pos=(1.0, 2.0))

    assert caught.value.attempts == 5
    assert caught.value.status == 503
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_timeout_is_retried() -> None:
    transport = FakeTransport([TimeoutError("slow"), response()])
    sleeps: list[float] = []
    rows = Sia2Client(transport=transport, sleeper=sleeps.append).search(
        "spherex_dr1", pos=(1.0, 2.0)
    )
    assert rows
    assert sleeps == [1.0]


def test_cache_hit_serves_bytes_without_another_request(tmp_path: Path) -> None:
    transport = FakeTransport([response(body=b"cutout-bytes"), response(body=b"other")])
    cache = DiskCache(tmp_path / "cache")
    fetcher = CutoutFetcher(transport=transport, cache=cache)
    access_url = "https://example.test/level2.fits"

    first = fetcher.fetch(access_url, center=(1.0, 2.0), size_deg=0.1)
    second = fetcher.fetch(access_url, center=(1.0, 2.0), size_deg=0.1)

    assert first == second == b"cutout-bytes"
    assert len(transport.calls) == 1

    fetcher.fetch(access_url, center=(1.0, 2.0), size_deg=0.2)
    assert len(transport.calls) == 2
    assert cache.key(access_url, {"center": "1.0,2.0", "size": "0.1"}) != cache.key(
        access_url, {"center": "1.0,2.0", "size": "0.2"}
    )
    assert cache.key(access_url, {"center": "1.0,2.0", "size": "0.1"}) != cache.key(
        "https://example.test/other.fits", {"center": "1.0,2.0", "size": "0.1"}
    )


def test_cutout_appends_center_and_size_to_access_url() -> None:
    transport = FakeTransport([response(body=b"fits")])
    fetcher = CutoutFetcher(transport=transport)
    access_url = "https://example.test/level2.fits?foo=bar"

    fetcher.fetch(access_url, center=(10.5, -2.25), size_deg=0.1)

    query = parse_qs(urlsplit(transport.calls[0]).query)
    assert query["foo"] == ["bar"]
    assert query["center"] == ["10.5,-2.25"]
    assert query["size"] == ["0.1"]
    assert append_query("https://x/y", {"a": "b"}) == "https://x/y?a=b"


def test_fixture_transport_serves_search_cutout_and_tap_urls() -> None:
    store = FixtureStore(FIXTURE)
    transport = FixtureTransport(store)
    search = store.index["searches"][0]
    cutout = store.index["cutouts"][0]

    assert transport.get(search["url"], 30).body == store.response(search["url"])
    assert transport.get(cutout["url"], 30).body == store.cutout(cutout["url"])
    assert transport.get("https://irsa.ipac.caltech.edu/TAP/sync?x=1", 30).body == (
        store.collections_bytes()
    )
    with pytest.raises(FixtureError, match="no recorded archive response"):
        transport.get("https://example.test/not-recorded", 30)


def test_collection_discovery_reads_recorded_listing_with_qr9() -> None:
    discovery = CollectionDiscovery(transport=FixtureTransport(FixtureStore(FIXTURE)))
    collections = discovery.list()
    by_name = {item.name: item for item in collections}

    assert by_name["spherex_qr9"].release_date == "2027-01-15"
    assert by_name["spherex_dr1"].release_date == "2026-11-12"
    assert "wise_allwise" not in by_name
    assert all(name.startswith("spherex_") for name in by_name)


def test_collection_discovery_live_path_uses_tap_query() -> None:
    body = b"collection,release_date\nspherex_qr9,2027-01-15\nwise_allwise,2013-11-14\n"
    transport = FakeTransport([response(body=body)])
    collections = CollectionDiscovery(transport=transport).list()

    assert [item.name for item in collections] == ["spherex_qr9"]
    query = parse_qs(urlsplit(transport.calls[0]).query)
    assert "spherex.obscore" in query["QUERY"][0]
    assert query["FORMAT"] == ["csv"]


def test_byte_fetcher_caches_and_retries(tmp_path: Path) -> None:
    transport = FakeTransport([response(503, b"Server busy"), response(body=b"cube")])
    fetcher = ByteFetcher(
        transport=transport,
        cache=DiskCache(tmp_path / "cache"),
        sleeper=lambda _: None,
    )

    assert fetcher.fetch("https://example.test/cube.fits") == b"cube"
    assert fetcher.fetch("https://example.test/cube.fits") == b"cube"
    assert len(transport.calls) == 2


def test_recorded_search_replays_with_zero_network_calls() -> None:
    store = FixtureStore(FIXTURE)
    client = Sia2Client(transport=FixtureTransport(store))
    entry = store.index["searches"][0]

    rows = client.search(
        entry["collection"], pos=tuple(entry["pos"]), size_deg=entry["size_deg"]
    )

    assert rows == parse_sia_csv(store.response(entry["url"]))
    assert len(rows) == entry["row_count"]
    assert rows[0]["obs_collection"] == entry["collection"]
    assert rows[0]["energy_bandpassname"].startswith("SPHEREx-")
    assert float(rows[0]["t_min"]) > 0
    assert rows[0]["access_url"].startswith("https://irsa.ipac.caltech.edu/")
    assert store.response(entry["url"])  # recorded bytes are present


def test_recorded_cutouts_have_no_epsf_and_replay_byte_identically() -> None:
    store = FixtureStore(FIXTURE)
    fetcher = CutoutFetcher(transport=FixtureTransport(store))

    assert store.index["cutouts"]
    for entry in store.index["cutouts"]:
        recorded = store.cutout(entry["url"])
        names = fits_hdu_names(recorded)
        assert "EPSF" not in names
        assert {"IMAGE", "FLAGS", "VARIANCE", "WCS-WAVE"} <= set(names)
        replay = fetcher.fetch(
            entry["access_url"],
            center=tuple(entry["center"]),
            size_deg=entry["size_deg"],
            strip_epsf=True,
        )
        assert replay == recorded
        assert strip_fits_extension(recorded, "EPSF") == recorded
        with pytest.raises(FixtureError, match="strip_epsf"):
            store.cutout(entry["url"], strip_epsf=False)


def test_fixture_holds_a_cube_slice() -> None:
    store = FixtureStore(FIXTURE)
    entry = store.index["cube_slice"]
    path = FIXTURE / entry["path"]
    names = fits_hdu_names(path.read_bytes())

    assert "IMAGE" in names
    assert "SPECTRAL_CHANNELS" in names


def test_fixture_is_within_budget() -> None:
    total = sum(path.stat().st_size for path in FIXTURE.rglob("*") if path.is_file())
    assert total <= 16 * 1024 * 1024


def _fits_card(key: str, value: str = "") -> bytes:
    return f"{key:<8}= {value:>20}".ljust(80).encode("ascii")


def _fits_hdu(*cards: bytes, data: bytes = b"") -> bytes:
    header = b"".join(cards + (b"END".ljust(80),))
    header += b" " * (-len(header) % 2880)
    data += b"\0" * (-len(data) % 2880)
    return header + data


def test_fits_parser_handles_empty_primary_and_strips_extension() -> None:
    primary = _fits_hdu(
        _fits_card("SIMPLE", "T"),
        _fits_card("BITPIX", "8"),
        _fits_card("NAXIS", "0"),
        _fits_card("EXTEND", "T"),
    )
    image = _fits_hdu(
        _fits_card("XTENSION", "'IMAGE   '"),
        _fits_card("BITPIX", "8"),
        _fits_card("NAXIS", "2"),
        _fits_card("NAXIS1", "2"),
        _fits_card("NAXIS2", "2"),
        _fits_card("EXTNAME", "'IMAGE   '"),
        data=b"abcd",
    )
    epsf = _fits_hdu(
        _fits_card("XTENSION", "'IMAGE   '"),
        _fits_card("BITPIX", "8"),
        _fits_card("NAXIS", "1"),
        _fits_card("NAXIS1", "4"),
        _fits_card("EXTNAME", "'EPSF    '"),
        data=b"wxyz",
    )
    encoded = primary + image + epsf

    assert fits_hdu_names(encoded) == ["PRIMARY", "IMAGE", "EPSF"]
    stripped = strip_fits_extension(encoded, "EPSF")
    assert fits_hdu_names(stripped) == ["PRIMARY", "IMAGE"]
    assert stripped == primary + image


def test_fits_parser_ignores_header_comments() -> None:
    primary = _fits_hdu(
        _fits_card("SIMPLE", "T / conforms to FITS"),
        _fits_card("BITPIX", "8 / array data type"),
        _fits_card("NAXIS", "0 / no data"),
        _fits_card("EXTEND", "T"),
    )
    image = _fits_hdu(
        _fits_card("XTENSION", "'IMAGE   ' / extension"),
        _fits_card("BITPIX", "8 / array data type"),
        _fits_card("NAXIS", "1 / one axis"),
        _fits_card("NAXIS1", "4 / width"),
        _fits_card("EXTNAME", "'IMAGE   '"),
        data=b"abcd",
    )

    assert fits_hdu_names(primary + image) == ["PRIMARY", "IMAGE"]


def test_retry_policy_is_configurable() -> None:
    policy = RetryPolicy()
    assert policy.max_attempts == 5
    assert policy.should_retry(429, b"")
    assert policy.should_retry(500, b"")
    assert policy.should_retry(502, b"")
    assert policy.should_retry(503, b"")
    assert policy.should_retry(400, b"Server busy")
    assert not policy.should_retry(404, b"not found")

    custom = RetryPolicy(delays=(0.5,), retry_statuses=frozenset({418}))
    assert custom.max_attempts == 2
    assert custom.should_retry(418, b"")
    assert not custom.should_retry(503, b"")


def test_record_is_idempotent_and_replay_is_offline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert ingest_record.record(FIXTURE) == 0
    record_output = capsys.readouterr().out
    assert "no network calls" in record_output

    assert ingest_record.replay(FIXTURE) == 0
    replay_output = capsys.readouterr().out
    assert "replay:" in replay_output


def test_sampling_preserves_endpoints_and_columns(tmp_path: Path) -> None:
    rows = [
        {"obs_publisher_did": f"did-{index}", "t_min": str(index)}
        for index in range(401)
    ]
    sampled = ingest_record.sample_rows(rows, 400)
    assert len(sampled) == 400
    assert sampled[0] == rows[0]
    assert sampled[-1] == rows[-1]

    path = tmp_path / "rows.csv"
    ingest_record.write_rows(path, rows[:3])
    parsed = parse_sia_csv(path.read_bytes())
    assert parsed == rows[:3]


def test_select_row_raises_on_malformed_rows() -> None:
    with pytest.raises(ingest_record.RecordingError):
        ingest_record.select_row(
            [{"obs_collection": "spherex_dr1", "energy_bandpassname": "SPHEREx-D3"}],
            "spherex_dr1",
            "D3",
            61000.0,
        )


def test_fixture_records_sampled_row_provenance() -> None:
    store = FixtureStore(FIXTURE)
    sampled = [entry for entry in store.index["searches"] if entry["sampled"]]
    assert sampled
    for entry in sampled:
        assert entry["row_count"] < entry["archive_row_count"]


def test_cli_configures_info_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)

    cli._configure_logging()

    assert root.level == logging.INFO
    assert root.handlers


def test_record_builds_a_fixture_from_a_fake_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = _fake_archive(tmp_path)

    assert ingest_record.record(tmp_path, transport=transport) == 0
    assert ingest_record.fixture_complete(tmp_path) is True
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert len(index["searches"]) == len(targets.FIXTURE_SEARCHES)
    assert len(index["cutouts"]) == len(targets.FIXTURE_CUTOUTS)
    calls = len(transport.calls)

    capsys.readouterr()
    assert ingest_record.record(tmp_path, transport=transport) == 0
    assert "no network calls" in capsys.readouterr().out
    assert len(transport.calls) == calls


def test_record_cutout_size_override_reaches_fetch_and_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest_record.time, "sleep", lambda _: None)
    transport = _fake_archive(tmp_path)

    assert ingest_record.record(tmp_path, transport=transport, cutout_size_deg=0.9) == 0

    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert {entry["size_deg"] for entry in index["cutouts"]} == {0.9}
    for entry in index["cutouts"]:
        assert parse_qs(urlsplit(entry["url"]).query)["size"] == ["0.9"]

    fetched = [url for url in transport.calls if "center=" in url]
    assert len(fetched) == len(targets.FIXTURE_CUTOUTS)
    for url in fetched:
        assert parse_qs(urlsplit(url).query)["size"] == ["0.9"]

    default_root = tmp_path / "default"
    default_transport = _fake_archive(default_root)
    assert ingest_record.record(default_root, transport=default_transport) == 0
    default_index = json.loads(
        (default_root / "index.json").read_text(encoding="utf-8")
    )
    expected = {spec.filename: spec.size_deg for spec in targets.FIXTURE_CUTOUTS}
    for entry in default_index["cutouts"]:
        filename = Path(entry["path"]).name
        assert entry["size_deg"] == expected[filename]
        assert parse_qs(urlsplit(entry["url"]).query)["size"] == [
            str(expected[filename])
        ]
    assert ingest_record.fixture_complete(default_root) is True

    # An override never reuses a cutout recorded at a different size, even
    # when the caller forgot --force.
    rerun_transport = _fake_archive(default_root, with_searches=False)
    assert (
        ingest_record.record(
            default_root,
            transport=rerun_transport,
            cutout_size_deg=0.9,
        )
        == 0
    )
    refetched = [url for url in rerun_transport.calls if "center=" in url]
    assert len(refetched) == len(targets.FIXTURE_CUTOUTS)
    for url in refetched:
        assert parse_qs(urlsplit(url).query)["size"] == ["0.9"]
    rerun_index = json.loads((default_root / "index.json").read_text(encoding="utf-8"))
    assert {entry["size_deg"] for entry in rerun_index["cutouts"]} == {0.9}


def test_override_resumes_missing_cutouts_at_file_granularity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest_record.time, "sleep", lambda _: None)
    first = _fake_archive(tmp_path)
    assert ingest_record.record(tmp_path, transport=first, cutout_size_deg=0.9) == 0

    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    missing_path = tmp_path / index["cutouts"][0]["path"]
    missing_path.unlink()

    full = _fake_archive(tmp_path, with_searches=False)
    partial = FakeTransport([full.responses[0], full.responses[-1]])
    assert ingest_record.record(tmp_path, transport=partial, cutout_size_deg=0.9) == 0

    fetched = [url for url in partial.calls if "center=" in url]
    assert len(fetched) == 1
    assert parse_qs(urlsplit(fetched[0]).query)["size"] == ["0.9"]
    resumed = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert {entry["size_deg"] for entry in resumed["cutouts"]} == {0.9}
    assert (tmp_path / ".cutout-progress.json").is_file()

    # A changed recorded visit invalidates that cutout's progress entry.
    progress_path = tmp_path / ".cutout-progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    stale = f"cutouts/{Path(resumed['cutouts'][1]['path']).name}"
    progress[stale]["url"] = "https://example.test/stale.fits"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    revisit = _fake_archive(tmp_path, with_searches=False)
    revisit_transport = FakeTransport([revisit.responses[0], revisit.responses[-1]])
    assert (
        ingest_record.record(tmp_path, transport=revisit_transport, cutout_size_deg=0.9)
        == 0
    )
    refetched_visits = [url for url in revisit_transport.calls if "center=" in url]
    assert len(refetched_visits) == 1

    # A different requested size invalidates every progress entry.
    changed = _fake_archive(tmp_path, with_searches=False)
    assert ingest_record.record(tmp_path, transport=changed, cutout_size_deg=0.5) == 0
    refetched = [url for url in changed.calls if "center=" in url]
    assert len(refetched) == len(targets.FIXTURE_CUTOUTS)
    for url in refetched:
        assert parse_qs(urlsplit(url).query)["size"] == ["0.5"]


def test_cutout_uses_only_the_targets_own_search_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest_record.time, "sleep", lambda _: None)
    _fake_archive(tmp_path)  # writes a reusable cube slice
    searches = (
        targets.SearchSpec("barnard", "spherex_dr1", "barnard.csv"),
        targets.SearchSpec("bamberga", "spherex_dr1", "bamberga.csv"),
    )
    cutouts = (
        targets.CutoutSpec(
            "barnard",
            "spherex_dr1",
            "D3",
            61100.0,
            "barnard.fits",
            center=(1.0, 2.0),
        ),
    )
    monkeypatch.setattr(ingest_record, "FIXTURE_SEARCHES", searches)
    monkeypatch.setattr(ingest_record, "FIXTURE_CUTOUTS", cutouts)

    own = (
        b"obs_publisher_did,access_url,t_min,obs_release_date,"
        b"obs_collection,energy_bandpassname\n"
        b"ivo://example/own,https://example.test/own.fits,61110,"
        b"2026-01-01,spherex_dr1,SPHEREx-D3\n"
    )
    other = (
        b"obs_publisher_did,access_url,t_min,obs_release_date,"
        b"obs_collection,energy_bandpassname\n"
        b"ivo://example/other,https://example.test/other.fits,61100,"
        b"2026-01-01,spherex_dr1,SPHEREx-D3\n"
    )
    discovery = (
        "collection,release_date\n"
        + "".join(
            f"{name},{release}\n"
            for name, release in targets.SEED_COLLECTIONS
            if name.startswith("spherex_")
        )
    ).encode()
    cutout_body = _fits_hdu(
        _fits_card("SIMPLE", "T"),
        _fits_card("BITPIX", "8"),
        _fits_card("NAXIS", "0"),
    ) + _fits_hdu(
        _fits_card("XTENSION", "'IMAGE   '"),
        _fits_card("BITPIX", "8"),
        _fits_card("NAXIS", "1"),
        _fits_card("NAXIS1", "4"),
        _fits_card("EXTNAME", "'IMAGE   '"),
        data=b"abcd",
    )
    transport = FakeTransport(
        [
            response(body=own),
            response(body=other),
            response(body=cutout_body),
            response(body=discovery),
        ]
    )

    assert ingest_record.record(tmp_path, transport=transport) == 0

    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert index["cutouts"][0]["access_url"] == "https://example.test/own.fits"
    fetched = [url for url in transport.calls if "center=" in url]
    assert len(fetched) == 1
    assert fetched[0].startswith("https://example.test/own.fits?")
    assert parse_qs(urlsplit(fetched[0]).query)["center"] == ["1.0,2.0"]


def test_fixture_rejects_unknown_schema_version(tmp_path: Path) -> None:
    copy = tmp_path / "archive"
    shutil.copytree(FIXTURE, copy)
    index = json.loads((copy / "index.json").read_text(encoding="utf-8"))
    index["schema_version"] = 2
    (copy / "index.json").write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(FixtureError, match="schema version"):
        FixtureStore(copy)


def test_completeness_rejects_a_truncated_recorded_file(tmp_path: Path) -> None:
    copy = tmp_path / "archive"
    shutil.copytree(FIXTURE, copy)
    entry = json.loads((copy / "index.json").read_text(encoding="utf-8"))["searches"][0]
    (copy / entry["path"]).write_bytes(b"obs_publisher_did\n")

    assert ingest_record.fixture_complete(copy) is False

    (copy / entry["path"]).write_bytes((FIXTURE / entry["path"]).read_bytes())
    assert ingest_record.fixture_complete(copy) is True


def test_cli_requires_an_explicit_fixture_path() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["record"])
    assert excinfo.value.code == 2

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["replay"])
    assert excinfo.value.code == 2


def test_archive_never_depends_on_ingest_or_the_fixture_tree() -> None:
    repository = Path(__file__).resolve().parents[1]
    archive_source = (repository / "drift" / "archive.py").read_text(encoding="utf-8")
    assert "drift.ingest" not in archive_source
    assert "tests/fixtures" not in archive_source

    for relative in ("drift/ingest/record.py", "drift/ingest/cli.py"):
        source = (repository / relative).read_text(encoding="utf-8")
        assert "tests/fixtures" not in source, relative
