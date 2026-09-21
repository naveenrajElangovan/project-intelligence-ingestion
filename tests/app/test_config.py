import base64

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_production_rejects_incomplete_or_local_configuration() -> None:
    with pytest.raises(ValidationError, match="Unsafe production configuration"):
        Settings(
            _env_file=None,
            environment="production",
            control_plane_url="http://localhost:8001",
        )


def test_secure_production_configuration_is_accepted() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        allowed_hosts="ingestion.example.com",
        docs_enabled=False,
        force_https=True,
        control_plane_url="https://api.internal.example.com",
        control_plane_api_key="c" * 32,
        github_app_id="12345",
        github_private_key_base64=base64.b64encode(
            b"-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----"
        ).decode(),
        github_webhook_secret="a" * 32,
        chroma_host="chroma.internal.example.com",
        state_table_endpoint="https://projectintelligence.table.core.windows.net",
        internal_api_key="i" * 32,
        enable_malware_scan=True,
        clamav_socket="/var/run/clamav/clamd.ctl",
        service_bus_namespace="project-intelligence.servicebus.windows.net",
        docling_artifacts_path="/opt/docling-models",
    )

    assert settings.is_production is True
