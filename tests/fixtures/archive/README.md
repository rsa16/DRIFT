# Archive fixture

The following test fixture was recorded from the SPHEREx archive on 2026-11-15. NASA/JPL-Caltech SPHEREx products served by IRSA.

- `index.json` maps each SIA2 or cutout request URL to its recorded file.
- `sia/*.csv` contains the SIA2 responses for our demo
- `cutouts/*.fits` contains IRSA cutouts with the `EPSF` extension removed. Barnard's Star has two DR1 and four QR3 visits in detector D5 at `size=0.4°`. 324 Bamberga in its D1 `size=0.1°` cutout.
- `cube/lpv_cube_slice.fits` contains a 3x3x102 spectral slice from the DR1 all-sky cube around the NEP LPV. Includes the `IMAGE`, `NHITS`, `FLAGS`, `SPECTRAL_CHANNELS`, and `WCS-WAVE` extensions.
- `collections.csv` is the collection list used by `CollectionDiscovery`. Pulled from TAP by `record`.

Replay:

```sh
python -m drift.ingest replay --fixture tests/fixtures/archive
```

You will need `astropy` at build time to re-record:

```sh
uv run python -m drift.ingest record --fixture tests/fixtures/archive --force
```
