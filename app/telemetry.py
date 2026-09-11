"""Content-free Prometheus telemetry for ingestion performance."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import time
from collections.abc import Iterator

from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client import push_to_gateway as _push_to_gateway


LOGGER = logging.getLogger("project_intelligence.ingestion.telemetry")

_JIRA_READS = Counter("pi_ingestion_jira_reads_total", "Jira API reads by bounded resource and outcome.", ("resource", "outcome"))
_JIRA_READ_DURATION = Histogram("pi_ingestion_jira_read_duration_seconds", "Jira API read duration including gateway validation.", ("resource",))
_JIRA_ITEMS = Counter("pi_ingestion_jira_source_items_total", "Collected or excluded Jira source items.", ("kind",))


def record_jira_read(resource: str, outcome: str, seconds: float) -> None:
    _JIRA_READS.labels(resource, outcome).inc()
    _JIRA_READ_DURATION.labels(resource).observe(seconds)


def record_jira_inventory(counts) -> None:
    for kind, count in counts.items():
        _JIRA_ITEMS.labels(kind).inc(count)


_SCOPES = Counter(
    "pi_ingestion_scopes_total",
    "Completed ingestion scopes.",
    ("provider", "mode", "outcome"),
)
_SCOPE_DURATION = Histogram(
    "pi_ingestion_scope_duration_seconds",
    "End-to-end ingestion scope duration.",
    ("provider", "mode"),
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 3600),
)
_ACTIVE_SCOPES = Gauge(
    "pi_ingestion_active_scopes",
    "Ingestion scopes currently executing.",
    ("provider",),
)
_LAST_SUCCESS = Gauge(
    "pi_ingestion_last_success_unixtime",
    "Unix timestamp of the last successful ingestion scope.",
    ("provider",),
)
_DOCUMENTS = Counter(
    "pi_ingestion_documents_total",
    "Documents processed by ingestion result.",
    ("provider", "operation"),
)
_DOCUMENT_STAGE_DURATION = Histogram(
    "pi_ingestion_document_stage_duration_seconds",
    "Per-document ingestion stage duration.",
    ("provider", "stage"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
_CHUNKS_WRITTEN = Counter(
    "pi_ingestion_chunks_written_total",
    "Chunks successfully written to the vector store.",
    ("provider",),
)
_DOCUMENT_FAILURES = Counter(
    "pi_ingestion_document_failures_total",
    "Documents that failed ingestion.",
    ("provider", "failure_type"),
)


def started() -> float:
    return time.perf_counter()


def observe_document_stage(provider: str, stage: str, began: float) -> None:
    _DOCUMENT_STAGE_DURATION.labels(provider.upper(), stage).observe(
        max(0.0, time.perf_counter() - began)
    )


def record_document_result(provider: str, operation: str, chunks_written: int = 0) -> None:
    provider_label = provider.upper()
    _DOCUMENTS.labels(provider_label, operation.upper()).inc()
    if chunks_written > 0:
        _CHUNKS_WRITTEN.labels(provider_label).inc(chunks_written)


def record_document_failure(provider: str, failure: BaseException) -> None:
    failure_type = type(failure).__name__
    if len(failure_type) > 64 or not failure_type.replace("_", "").isalnum():
        failure_type = "UnknownError"
    _DOCUMENT_FAILURES.labels(provider.upper(), failure_type).inc()


def push_metrics(url: str) -> None:
    """Persist short-lived job metrics without making telemetry a dependency."""

    if not url.strip():
        return
    try:
        _push_to_gateway(
            url.strip(),
            job="project-intelligence-ingestion-batch",
            registry=REGISTRY,
            timeout=5,
        )
    except Exception as failure:
        LOGGER.warning("Ingestion metrics push failed (%s).", type(failure).__name__)


@contextmanager
def observe_scope(provider: str, *, full: bool) -> Iterator[None]:
    provider_label = provider.upper()
    mode = "full" if full else "incremental"
    began = time.perf_counter()
    _ACTIVE_SCOPES.labels(provider_label).inc()
    try:
        yield
    except BaseException:
        _SCOPES.labels(provider_label, mode, "failed").inc()
        raise
    else:
        _SCOPES.labels(provider_label, mode, "succeeded").inc()
        _LAST_SUCCESS.labels(provider_label).set_to_current_time()
    finally:
        _ACTIVE_SCOPES.labels(provider_label).dec()
        _SCOPE_DURATION.labels(provider_label, mode).observe(
            max(0.0, time.perf_counter() - began)
        )
