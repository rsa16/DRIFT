<div align="center">

# DRIFT

### Differential Infrared Frame Tracker

Compare images of the sky across NASA's SPHEREx visits. Created for the NASA SpaceApps Challenge 2026.

[![CI](https://github.com/rsa16/DRIFT/actions/workflows/ci.yml/badge.svg)](https://github.com/rsa16/DRIFT/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.14-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![Web](https://img.shields.io/badge/web-SvelteKit%20static%20SPA-ff3e00?logo=svelte&logoColor=white)](https://kit.svelte.dev/)
[![License](https://img.shields.io/badge/license-AGPL--3.0--only-blue)](LICENSE)

</div>

DRIFT compares SPHEREx observations taken at different times and highlights changes between them.

## What DRIFT does

SPHEREx maps the  sky every six months in 102 near-infrared bands. Comparing two visits can reveal a brightening star, a moving asteroid, or a new transient. But two visits are generally not comparable directly, since the measured wavelength depends on where a location falls on the detector.

Features:
- Visit timeline - Browse visits by date, MJD, release, and visit ID, and play through them.
- Matched comparisons - Ensures you blink or subtract images only when their measured wavelengths are close enough and they come from the same detector.
- Difference images - Subtract one visit from another so unchanged parts of the sky disappear.
- Colour composites - Combine visits into colour channels when that makes a change easier to see.
- Shareable views - Store `ra`, `dec`, and `fov` in the URL.
- Live lookups - Query arbitrary coordinates on the archive server, subject to field-of-view, rate, redirect, and timeout limits.

## Quickstart

You need Docker and Docker Compose:

```sh
git clone https://github.com/rsa16/DRIFT.git
cd DRIFT
docker compose up --build
```

Open <http://localhost:8080>. The API is available on the same origin at `/api/v1/health`.

## Documentation

| Guide | Description |
| --- | --- |
| [Architecture](docs/architecture.md) | System structure, data format, and live archive access |
| [Deployment](docs/deployment.md) | Compose topology, Caddy, configuration, and publishing |
| [Fixtures](docs/fixtures.md) | Committed test fixture and untracked demo fixture |
| [Challenge details](docs/challenge-details.md) | NASA Space Apps brief addressed by this project |

## Data and credits

- **SPHEREx** spectral images, quarterly releases, and the DR1 all-sky cube via NASA/IPAC Infrared Science Archive (IRSA) SIA2 and the IRSA cutout service.
- **Gaia DR3** (ESA) for source positions, proper motions, and variability classes.
- **SkyBoT** for known moving-object positions at the observation date.

SPHEREx data are produced by NASA/JPL-Caltech and served by IRSA. This project is not affiliated with or endorsed by NASA. Data remain subject to their providers' terms.

## License

This project is available under the [AGPL-3.0-only](LICENSE) license.
