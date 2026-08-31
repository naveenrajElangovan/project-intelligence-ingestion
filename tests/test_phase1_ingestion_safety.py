import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models import DocumentIndexResult, SourceDocument
from app.projects import IngestionProject, VectorStoreRoute
from app.service import IngestionService


def _project() -> IngestionProject:
    return IngestionProject(
        project_id="DEMO",
        display_name="DEMO",
        repositories=(),
        jira_projects=(),
        confluence_spaces=(),
        vector_store=VectorStoreRoute("project-intelligence", "chunk_text"),
    )


def _document(source_id: str = "source-1") -> SourceDocument:
    return SourceDocument(
        project_id="DEMO",
        provider="FUTURE_CONNECTOR",
        source_id=source_id,
        source_type="CUSTOM_ARTIFACT",
        title=source_id,
        reference=source_id,
        source_url=f"local://{source_id}",
        version="1",
        content="content",
        updated_at=datetime.now(UTC),
    )


async def _documents(count: int = 1):
    for index in range(count):
        yield _document(f"source-{index}")


class MemoryManifestStore:
    def __init__(self) -> None:
        self.owner: str | None = None
        self.acquire_attempts = 0
        self.releases = 0
        self.stale_calls = 0

    async def acquire_scope_lease(self, project_id, provider, scope, owner, ttl_seconds):
        self.acquire_attempts += 1
        if self.owner is not None:
            return False
        self.owner = owner
        return True

    async def release_scope_lease(self, project_id, provider, scope, owner):
        assert self.owner == owner
        self.owner = None
        self.releases += 1

    async def stale_page(self, project_id, provider, scope, scan_id, token):
        self.stale_calls += 1
        return (), None

    async def save_cursor(self, project_id, provider, scope, value):
        return None


class BlockingWorkflow:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
        self.calls = 0
        self.vocabulary_refreshes = 0

    async def run(self, document, scope, scan_id, vector_store, force=False):
        self.calls += 1
        self.started.set()
        await self.resume.wait()
        return DocumentIndexResult("UNCHANGED", 0)

    async def refresh_project_vocabulary(self, vector_store, project_id):
        assert project_id == "DEMO"
        self.vocabulary_refreshes += 1


def test_concurrent_scope_owner_is_rejected_before_it_can_delete() -> None:
    async def scenario() -> None:
        manifests = MemoryManifestStore()
        workflow = BlockingWorkflow()
        service = IngestionService(
            Settings(_env_file=None), SimpleNamespace(), manifests, workflow
        )
        first = asyncio.create_task(
            service._run_scope(_project(), "FUTURE_CONNECTOR", "scope", _documents(), True)
        )
        await workflow.started.wait()
        with pytest.raises(RuntimeError, match="already running"):
            await service._run_scope(
                _project(), "FUTURE_CONNECTOR", "scope", _documents(), True
            )
        workflow.resume.set()
        await first
        assert workflow.calls == 1
        assert manifests.acquire_attempts == 2
        assert manifests.releases == 1
        assert manifests.stale_calls == 1
        assert workflow.vocabulary_refreshes == 1

    asyncio.run(scenario())


def test_deletion_floor_blocks_stale_manifest_scan() -> None:
    async def scenario() -> None:
        manifests = MemoryManifestStore()
        service = IngestionService(
            Settings(_env_file=None, deletion_floor_documents=2),
            SimpleNamespace(),
            manifests,
            SimpleNamespace(),
        )
        deleted = await service._delete_stale(
            _project(), "FUTURE_CONNECTOR", "scope", "scan", discovered=1
        )
        assert deleted == 0
        assert manifests.stale_calls == 0

    asyncio.run(scenario())


def test_scope_stops_at_document_failure_budget() -> None:
    class FailingWorkflow:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, document, scope, scan_id, vector_store, force=False):
            self.calls += 1
            raise RuntimeError("bad source")

    async def scenario() -> None:
        manifests = MemoryManifestStore()
        workflow = FailingWorkflow()
        service = IngestionService(
            Settings(_env_file=None, max_document_failures_per_scope=2),
            SimpleNamespace(),
            manifests,
            workflow,
        )
        with pytest.raises(RuntimeError, match="failure budget"):
            await service._run_scope(
                _project(), "FUTURE_CONNECTOR", "scope", _documents(3), False
            )
        assert workflow.calls == 2
        assert manifests.releases == 1

    asyncio.run(scenario())
