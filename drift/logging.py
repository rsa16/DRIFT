from __future__ import annotations

import logging

logger = logging.getLogger("drift.logging")


def log_external_request(
    *,
    method: str,
    url: str,
    status: int,
    retries: int,
    latency_s: float,
    log: logging.Logger | None = None,
) -> str:
    line = (
        f"external_request method={method} url={url} status={status} "
        f"retries={retries} latency_ms={latency_s * 1000.0:.1f}"
    )
    (log or logger).info(line)
    return line
