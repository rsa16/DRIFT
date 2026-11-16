# Fixtures

DRIFT uses the same interface for live IRSA data and for data saved on disk. This is so tests can run without IRSA and so the demo runs fine. IRSA is not a reliable API and often fails.

There are two fixtures, each for a different purpose.

## The test fixture

`tests/fixtures/archive` is the small fixture checked into the repository. It is used by `make check` and the test suite. The files are recorded at the cutout sizes used by the tests (0.1° or 0.4°).

The fixture README describes the recorded files:

> The following test fixture was recorded from the SPHEREx archive on 2026-11-15. NASA/JPL-Caltech SPHEREx products served by IRSA.
>
> - `index.json` maps each SIA2 or cutout request URL to its recorded file.
> - `sia/*.csv` contains the SIA2 responses for our demo.
> - `cutouts/*.fits` contains IRSA cutouts with the `EPSF` extension removed. Barnard's Star has two DR1 and four QR3 visits in detector D5 at `size=0.4°`. 324 Bamberga in its D1 `size=0.1°` cutout.
> - `cube/lpv_cube_slice.fits` contains a 3x3x102 spectral slice from the DR1 all-sky cube around the NEP LPV. Includes the `IMAGE`, `NHITS`, `FLAGS`, `SPECTRAL_CHANNELS`, and `WCS-WAVE` extensions.
> - `collections.csv` is the collection list used by `CollectionDiscovery`. Pulled from TAP by `record`.

Replay the fixture without using the network:

```sh
uv run --frozen python -m drift.ingest replay --fixture tests/fixtures/archive
```

Re-record it when the archive changes. This needs a network connection and
replaces the committed data:

```sh
uv run --frozen python -m drift.ingest record --fixture tests/fixtures/archive --force
```

## The demo fixture

`build/demo-fixture` is generated locally and should not be checked into git. It records the same visits as the test fixture, but uses 0.9° cutouts so the demo can show more sky around each target. The published site still serves the same 512² `SPX1` planes, so only the recorded FITS files get larger.

Record the demo fixture and build the snapshot:

```sh
make demo-fixture
make demo-snapshot
```

`make demo-fixture` runs:

```sh
uv run --frozen python -m drift.ingest record \
  --cutout-size-deg 0.9 --fixture build/demo-fixture
```

If a cutout download fails, run the command again. Files that were already written are reused, and only missing, resized, or newly selected cutouts are downloaded. A small `.cutout-progress.json` file records each cutout's size and visit URL, so a resumed run does not reuse a cutout recorded with the wrong settings. Delete `build/demo-fixture` or pass `--force` to start over.

`make demo-snapshot` writes the demo snapshot to `build/demo-snapshots`. Neither target runs as part of `make check`. Recording the 0.9° fixture is a manual network step; the tests remain offline and use only `tests/fixtures/archive`.

## Snapshot layout

Both commands write snapshots with this layout:

```
<snapshot_dir>/
├── latest.json                       # names the current snapshot
└── v1/<snapshot_id>/
    ├── manifest.json                 # targets, visits, bands, windows, planes
    └── planes/*.spx                  # float16 image planes
```

At request time, the application reads `latest.json` and follows the pointer to the current snapshot. It does not read any other snapshot. Publishing finishes writing the new snapshot before updating the pointer, so a failed run does not publish a partial snapshot.

See [Architecture](architecture.md) for the snapshot contract and [Deployment](deployment.md) for instructions on publishing one to a server.
