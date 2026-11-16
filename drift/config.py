"""DRIFT service config

All settings are read from ``DRIFT_*`` env vars. It will pull values
from a local `.env` by default. ``DRIFT_ENV_FILE`` must be set to
use a different file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_ENV_FILE = ".env"
DEFAULT_SNAPSHOT_DIR = Path("/data/snapshots")
DEFAULT_CACHE_DIR = Path("/data/cache")
DEFAULT_IRSA_BASE = "https://irsa.ipac.caltech.edu"


class Settings(BaseSettings):
    """The service configuration, resolved once from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="DRIFT_",
        env_file=DEFAULT_ENV_FILE,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR
    cache_dir: Path = DEFAULT_CACHE_DIR
    live_enabled: bool = False
    allow_fixture: bool = False
    irsa_base: str = DEFAULT_IRSA_BASE
    trusted_proxy_hops: int = 0
    live_timeout_s: float = 28.0
    live_rate_limit: int = 6
    live_rate_window_s: float = 60.0
    live_max_concurrency: int = 2
    live_max_fov_deg: float = 1.0
    live_cache_ttl_s: float = 60.0
    spatial_index: Path | None = None
    tls_cert: Path | None = None
    tls_key: Path | None = None
    domain: str | None = None

    @field_validator(
        "snapshot_dir",
        "cache_dir",
        "spatial_index",
        "tls_cert",
        "tls_key",
        mode="before",
    )
    @classmethod
    def _expand_path(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return Path(str(value)).expanduser()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        env_file = os.environ.get("DRIFT_ENV_FILE")
        if env_file is not None:
            dotenv_settings = DotEnvSettingsSource(
                settings_cls, env_file=env_file or None
            )
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        if environ is None:
            return cls()

        prefix = cls.model_config.get("env_prefix", "")
        values: dict[str, object] = {}
        for name, field in cls.model_fields.items():
            raw = environ.get(f"{prefix}{name}".upper())
            if raw not in (None, ""):
                values[name] = raw
            else:
                # Fill every unmentioned field with its declared default so
                # model_validate cannot pull a value from os.environ or .env.
                values[name] = field.get_default(call_default_factory=True)
        return cls.model_validate(values)
