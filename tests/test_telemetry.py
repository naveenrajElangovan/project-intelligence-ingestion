from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import telemetry
from app.main import app
from app.telemetry import (
    observe_document_stage,
    observe_scope,
    push_metrics,
    record_document_result,
    started,
)


def _sample_value(name: str, labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return value or 0.0


def test_metrics_endpoint_is_content_free() -> None:
    response = TestClient(app, base_url="http://localhost").get("/metrics")
    assert response.status_code == 200
    assert "pi_ingestion_scopes_total" in response.text
    assert "source_id" not in response.text
    assert "project_id" not in response.text


def test_scope_and_document_metrics_are_recorded() -> None:
    before = _sample_value(
        "pi_ingestion_scopes_total",
        {"provider": "GITHUB", "mode": "incremental", "outcome": "succeeded"},
    )
    with observe_scope("github", full=False):
        began = started()
        record_document_result("github", "indexed", 3)
        observe_document_stage("github", "write", began)

    assert _sample_value(
        "pi_ingestion_scopes_total",
        {"provider": "GITHUB", "mode": "incremental", "outcome": "succeeded"},
    ) == before + 1
    assert _sample_value("pi_ingestion_active_scopes", {"provider": "GITHUB"}) == 0
    assert _sample_value(
        "pi_ingestion_documents_total",
        {"provider": "GITHUB", "operation": "INDEXED"},
    ) >= 1
    assert _sample_value("pi_ingestion_chunks_written_total", {"provider": "GITHUB"}) >= 3


def test_short_lived_job_metrics_use_a_content_free_group(monkeypatch) -> None:
    call: dict[str, object] = {}

    def capture(url: str, **kwargs: object) -> None:
        call["url"] = url
        call.update(kwargs)

    monkeypatch.setattr(telemetry, "_push_to_gateway", capture)
    push_metrics("http://127.0.0.1:9091")

    assert call["url"] == "http://127.0.0.1:9091"
    assert call["job"] == "project-intelligence-ingestion-batch"
    assert "grouping_key" not in call
