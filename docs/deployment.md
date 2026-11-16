# Deployment

DRIFT runs with a compose setup. There is no database, and the containers do not hold any application data themselves. A deployment consists of the two images and the snapshot directory.

## Containers

The [Architecture](architecture.md) page explains the system's components and data flow. The compose runs the following:

| Service | Built from | Listens | Mounts | Notes |
| --- | --- | --- | --- | --- |
| `api` | root `Dockerfile` | 8000, internal | `drift-data:/data` | healthcheck on `/api/v1/health` every 5 s |
| `web` | `web/Dockerfile` (Caddy + static build) | 8080, published | `Caddyfile` read-only, `drift-data:/drift` read-only | waits for `api` to be healthy |
| `drift-data` | volume | — | — | snapshots and cache |

Caddy serves the web app, sends `/api/*` requests to `api:8000`, and serves published snapshot files under `/v1/*` directly from the volume. Those files use immutable cache headers, so plane downloads do not go through Python and the browser only needs one origin.

## First deployment

```sh
git clone https://github.com/rsa16/DRIFT.git
cd DRIFT
docker compose up -d --build
curl -fsS localhost:8080/api/v1/health
```

Compose exposes the `web` service on port `8080`.The `api` container checks `/api/v1/health` every five seconds. The `web` container waits for that check to pass before it starts.

### Initial snapshot

If the volume does not contain `latest.json`, the API creates a fake/test snapshot for demo-ing. Please ensure this is disabled in production:

```yaml
environment:
  DRIFT_ALLOW_FIXTURE: "false"
```


## Publish a snapshot

```sh
docker compose run --rm \
  -v "$PWD/build/demo-fixture:/fixture:ro" \
  api python -m drift.ingest build \
    --fixture /fixture \
    --snapshot-dir /data/snapshots
```

Use `--targets` to select targets. Use `--generated-at` to set the timestamp, which will be used as the snapshot's ID. If the machine can access the archive (be warned that it often cannot), `record` can fetch fresh data instead of replaying a fixture; see [Fixtures](fixtures.md).

The command writes the complete snapshot to `v1/<snapshot_id>/`, then updates `latest.json` through a temporary file and rename. If the command stops halfway through, the previous snapshot keeps serving and the incomplete snapshot is not published.

## Configuration reference

All settings use `DRIFT_*` environment variables. Empty values are ignored. An env file is used by default.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DRIFT_SNAPSHOT_DIR` | `/data/snapshots` | Where `latest.json` and snapshots live |
| `DRIFT_CACHE_DIR` | `/data/cache` | Derived planes and live-resolve cache |
| `DRIFT_LIVE_ENABLED` | `true` | Turn on the live coordinate path |
| `DRIFT_ALLOW_FIXTURE` | `false` | Allow serving synthetic/fixture snapshots (demo only) |
| `DRIFT_IRSA_BASE` | `https://irsa.ipac.caltech.edu` | Archive base URL |
| `DRIFT_TRUSTED_PROXY_HOPS` | `0` | Proxy hops to trust for `X-Forwarded-For`; `0` uses the socket address |
| `DRIFT_LIVE_TIMEOUT_S` | `28` | Deadline for one live resolve |
| `DRIFT_LIVE_RATE_LIMIT` | `6` | Live resolves allowed per client per window |
| `DRIFT_LIVE_RATE_WINDOW_S` | `60` | Window length for that limit |
| `DRIFT_LIVE_MAX_CONCURRENCY` | `2` | Concurrent archive operations |
| `DRIFT_LIVE_MAX_FOV_DEG` | `1.0` | Largest field of view a live request may ask for |
| `DRIFT_LIVE_CACHE_TTL_S` | `60` | How long a resolved live timeline is reused |
| `DRIFT_SPATIAL_INDEX` | unset | Optional SQLite S3 header index for faster live resolution |
| `DRIFT_TLS_CERT` | unset | Reserved; the service does not terminate TLS |
| `DRIFT_TLS_KEY` | unset | Reserved; the service does not terminate TLS |
| `DRIFT_DOMAIN` | unset | Reserved for deployment tooling; not read by the service |
| `DRIFT_ENV_FILE` | `.env` | Which env file to load; empty forbids loading |

## Monitoring

`GET /api/v1/health`

```json
{"snapshot_id": "20261116T025759Z-b9b213ba", "snapshot_age_s": 412,
 "plane_count": 144, "disk_free_bytes": 81234567168}
```

- `snapshot_id: null` means that no snapshot has been published.
- `snapshot_age_s` is the snapshot's age.
- `disk_free_bytes` is the free space on the snapshot filesystem.

## Upgrades

Images are versioned by tag in `docker-compose.yml` (`drift-api:0.10`,`drift-web:0.10`). Bump both tags together for a release, then:

```sh
docker compose pull   # or: docker compose build
docker compose up -d
```

Snapshots are stored as data, so upgrading the images does not change them. The manifest reader ignores unknown fields but refuses an unknown major schema version. This prevents an older image from partially reading a newer manifest. Start the new image before publishing a snapshot that requires it.

## Backups

Back up the `drift-data` volume. The `snapshots/` directory is the important part; `cache/` contains derived data and can be deleted without affecting snapshots. To restore a deployment, copy the volume back and keep the `latest.json` you want to serve.
