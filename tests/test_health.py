import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from drift.api import create_app
from drift.config import DEFAULT_IRSA_BASE, Settings
from drift.logging import log_external_request
from drift.manifest import validate

_CONFIG_VARS = [
    "DRIFT_SNAPSHOT_DIR",
    "DRIFT_CACHE_DIR",
    "DRIFT_LIVE_ENABLED",
    "DRIFT_IRSA_BASE",
    "DRIFT_TLS_CERT",
    "DRIFT_TLS_KEY",
    "DRIFT_DOMAIN",
    "DRIFT_ENV_FILE",
]


@pytest.fixture
def clean_config_env(monkeypatch, tmp_path):
    for name in _CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "DRIFT_SNAPSHOT_DIR": str(tmp_path / "snapshots"),
            "DRIFT_CACHE_DIR": str(tmp_path / "cache"),
            "DRIFT_LIVE_ENABLED": "true",
        }
    )


def _write_snapshot(root, generated_at: str, plane_count: int = 2) -> str:
    snapshot_id = "20261114T235900Z-deadbeef"
    manifest = validate(
        {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "generated_at": generated_at,
            "generator": {"name": "spherex-epoch-viewer", "version": "0.1.0"},
            "processing": {"plane_px": 512},
            "status": "published",
            "source": {"service": "IRSA SIA2", "collections": []},
            "targets": [],
            "files": [
                {
                    "path": f"v1/{snapshot_id}/planes/plane_{i}.spx",
                    "bytes": 1,
                    "sha256": "<sha256>",
                }
                for i in range(plane_count)
            ],
            "alerts": [],
        }
    )
    snapshot_dir = root / "v1" / snapshot_id
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "manifest.json").write_text(manifest.model_dump_json())
    (root / "latest.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "manifest": f"v1/{snapshot_id}/manifest.json",
            }
        )
    )
    return snapshot_id


def test_config_reads_only_documented_environment_variables(tmp_path):
    settings = Settings.from_env(
        {
            "DRIFT_SNAPSHOT_DIR": str(tmp_path / "snap"),
            "DRIFT_CACHE_DIR": str(tmp_path / "cache"),
            "DRIFT_IRSA_BASE": "https://irsa.example.invalid",
            "DRIFT_TLS_CERT": "/run/secrets/tls.crt",
            "DRIFT_TLS_KEY": "/run/secrets/tls.key",
            "DRIFT_DOMAIN": "drift.example.invalid",
            "UNRELATED_SETTING": "must-not-be-read",
        }
    )

    assert settings.snapshot_dir == tmp_path / "snap"
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.irsa_base == "https://irsa.example.invalid"
    assert (
        settings.tls_cert == tmp_path.parent / "secrets" / "tls.crt"
        or str(settings.tls_cert) == "/run/secrets/tls.crt"
    )
    assert settings.tls_key is not None
    assert settings.domain == "drift.example.invalid"
    assert not hasattr(settings, "unrelated_setting")


def test_config_defaults_come_from_the_process_environment(
    clean_config_env, monkeypatch
):
    monkeypatch.setenv("DRIFT_SNAPSHOT_DIR", str(clean_config_env / "snap"))
    monkeypatch.setenv("DRIFT_IRSA_BASE", "https://irsa.example.invalid")

    settings = Settings.from_env()

    assert settings.snapshot_dir == clean_config_env / "snap"
    assert settings.irsa_base == "https://irsa.example.invalid"
    assert settings.live_enabled is True
    assert settings.tls_cert is None


def test_config_reads_a_local_env_file(clean_config_env):
    (clean_config_env / ".env").write_text(
        f"DRIFT_SNAPSHOT_DIR={clean_config_env / 'snap'}\nDRIFT_LIVE_ENABLED=true\n",
        encoding="utf-8",
    )

    settings = Settings.from_env()

    assert settings.snapshot_dir == clean_config_env / "snap"
    assert settings.live_enabled is True


def test_real_environment_wins_over_the_env_file(clean_config_env, monkeypatch):
    (clean_config_env / ".env").write_text(
        "DRIFT_IRSA_BASE=https://file.example.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DRIFT_IRSA_BASE", "https://env.example.invalid")

    assert Settings.from_env().irsa_base == "https://env.example.invalid"


def test_env_file_can_be_pointed_elsewhere(clean_config_env, monkeypatch):
    (clean_config_env / "other.env").write_text(
        "DRIFT_IRSA_BASE=https://other.example.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DRIFT_ENV_FILE", "other.env")

    assert Settings.from_env().irsa_base == "https://other.example.invalid"


def test_empty_env_file_variable_disables_file_loading(clean_config_env, monkeypatch):
    (clean_config_env / ".env").write_text(
        "DRIFT_IRSA_BASE=https://file.example.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DRIFT_ENV_FILE", "")

    assert Settings.from_env().irsa_base == DEFAULT_IRSA_BASE


def test_config_rejects_an_unparseable_boolean(tmp_path):
    with pytest.raises(ValidationError):
        Settings.from_env(
            {
                "DRIFT_SNAPSHOT_DIR": str(tmp_path / "snap"),
                "DRIFT_LIVE_ENABLED": "maybe",
            }
        )


def test_health_without_a_snapshot_returns_four_fields_and_nulls(tmp_path):
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "snapshot_id",
        "snapshot_age_s",
        "plane_count",
        "disk_free_bytes",
    }
    assert body["snapshot_id"] is None
    assert body["snapshot_age_s"] is None
    assert body["plane_count"] == 0
    assert body["disk_free_bytes"] > 0


def test_health_with_a_snapshot_reports_id_age_and_plane_count(tmp_path):
    settings = _settings(tmp_path)
    generated_at = datetime.now(UTC) - timedelta(hours=2)
    snapshot_id = _write_snapshot(
        settings.snapshot_dir,
        generated_at.isoformat().replace("+00:00", "Z"),
        plane_count=2,
    )
    client = TestClient(create_app(settings))

    body = client.get("/api/v1/health").json()

    assert body["snapshot_id"] == snapshot_id
    assert 7100 <= body["snapshot_age_s"] <= 7300
    assert body["plane_count"] == 2
    assert body["disk_free_bytes"] > 0


def test_external_request_logger_emits_one_line_with_status_retries_and_latency(
    caplog,
):
    caplog.set_level(logging.INFO, logger="drift.logging")

    def fake_caller():
        return log_external_request(
            method="GET",
            url="https://irsa.example.invalid/SIA?COLLECTION=spherex_dr1",
            status=503,
            retries=2,
            latency_s=1.234,
        )

    fake_caller()

    records = [r for r in caplog.records if r.name == "drift.logging"]
    assert len(records) == 1
    line = records[0].getMessage()
    assert "\n" not in line
    assert "status=503" in line
    assert "retries=2" in line
    assert "latency_ms=1234.0" in line


def test_unhandled_errors_never_return_a_stack_trace(tmp_path):
    app = create_app(_settings(tmp_path))

    @app.get("/api/v1/boom")
    def boom():
        raise RuntimeError("secret internal failure")

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/v1/boom")

    assert response.status_code == 500
    assert "Traceback" not in response.text
    assert "secret internal failure" not in response.text
    assert response.json() == {"detail": "internal server error"}
