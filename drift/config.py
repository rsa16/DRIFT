"""DRIFT service config

All settings are read from ``DRIFT_*`` env vars.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SNAPSHOT_DIR = "/data/snapshots"
DEFAULT_CACHE_DIR = "/data/cache"
DEFAULT_IRSA_BASE = "https://irsa.ipac.caltech.edu"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """Raised when an environment value cannot be interpreted."""


def _optional_path(value: str | None) -> Path | None:
    if value is None or value == "":
        return None
    return Path(value).expanduser()


def _parse_bool(name: str, value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean, got {value!r}")


@dataclass(frozen=True)
class Settings:
    """The service configuration, resolved once from the environment."""

    snapshot_dir: Path
    cache_dir: Path
    live_enabled: bool = False
    irsa_base: str = DEFAULT_IRSA_BASE
    tls_cert: Path | None = None
    tls_key: Path | None = None
    domain: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        return cls(
            snapshot_dir=Path(
                env.get("DRIFT_SNAPSHOT_DIR", DEFAULT_SNAPSHOT_DIR)
            ).expanduser(),
            cache_dir=Path(env.get("DRIFT_CACHE_DIR", DEFAULT_CACHE_DIR)).expanduser(),
            live_enabled=_parse_bool(
                "DRIFT_LIVE_ENABLED", env.get("DRIFT_LIVE_ENABLED"), default=False
            ),
            irsa_base=env.get("DRIFT_IRSA_BASE", DEFAULT_IRSA_BASE),
            tls_cert=_optional_path(env.get("DRIFT_TLS_CERT")),
            tls_key=_optional_path(env.get("DRIFT_TLS_KEY")),
            domain=env.get("DRIFT_DOMAIN") or None,
        )
