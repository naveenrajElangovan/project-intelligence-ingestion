from datetime import UTC, datetime
import asyncio

from app.config import Settings
from app.models import SourceDocument
from app.projects import SourceAccessRule, VectorStoreRoute
from app.state import SourceManifest
from app.workflow import DocumentIngestionWorkflow


class MemoryManifests:
    def __init__(self) -> None:
        self.value = None
        self.touched = 0

    async def get_manifest(self, project_id, provider, scope, source_id):
        return self.value

    async def save_manifest(
        self,
        document,
        scope,
        chunk_count,
        scan_id,
        *,
        parser_version="legacy",
        chunker_version="legacy",
        embedding_profile="legacy",
        schema_version="1",
        access_policy_id="",
    ):
        self.value = SourceManifest(
            document.project_id,
            document.provider,
            scope,
            document.source_id,
            document.source_type,
            document.title,
            document.source_url,
            document.version,
            document.content_hash,
            chunk_count,
            scan_id,
            document.deleted,
            parser_version,
            chunker_version,
            embedding_profile,
            schema_version,
            access_policy_id,
        )

    async def touch_manifest(self, manifest, scan_id):
        self.touched += 1
        self.value = SourceManifest(
            manifest.project_id,
            manifest.provider,
            manifest.scope,
            manifest.source_id,
            manifest.source_type,
            manifest.title,
            manifest.source_url,
            manifest.version,
            manifest.content_hash,
            manifest.chunk_count,
            scan_id,
            manifest.deleted,
            manifest.parser_version,
            manifest.chunker_version,
            manifest.embedding_profile,
            manifest.schema_version,
            manifest.access_policy_id,
        )

    async def mark_deleted(self, manifest, scan_id):
        self.value = SourceManifest(
            manifest.project_id,
            manifest.provider,
            manifest.scope,
            manifest.source_id,
            manifest.source_type,
            manifest.title,
            manifest.source_url,
            manifest.version,
            "",
            0,
            scan_id,
            True,
            manifest.parser_version,
            manifest.chunker_version,
            manifest.embedding_profile,
            manifest.schema_version,
            manifest.access_policy_id,
        )


class MemoryVectors:
    def __init__(self) -> None:
        self.writes = 0
        self.deletes = 0
        self.access_policy_ids = []

    async def replace_document(self, mapping, document, chunks, **kwargs):
        self.writes += 1
        self.access_policy_ids.append(kwargs.get("access_policy_id"))

    async def delete_document(self, mapping, project_id, provider, source_id):
        self.deletes += 1


class EventManifests(MemoryManifests):
    def __init__(self, events):
        super().__init__()
        self.events = events

    async def save_manifest(self, *args, **kwargs):
        self.events.append("manifest")
        await super().save_manifest(*args, **kwargs)


class EventVectors(MemoryVectors):
    def __init__(self, events):
        super().__init__()
        self.events = events

    async def replace_document(self, mapping, document, chunks, **kwargs):
        self.events.append("chroma")
        await super().replace_document(mapping, document, chunks, **kwargs)


def document(*, deleted: bool = False) -> SourceDocument:
    return SourceDocument(
        project_id="DEMO",
        provider="CONFLUENCE",
        source_id="page:1",
        source_type="PAGE",
        title="POS",
        reference="1",
        source_url="https://example.atlassian.net/wiki/1",
        version="7",
        content="A production document with enough text to split.",
        updated_at=datetime.now(UTC),
        deleted=deleted,
    )


def test_unchanged_content_is_not_reembedded() -> None:
    asyncio.run(_unchanged_content_is_not_reembedded())


def test_policy_change_relabels_unchanged_content() -> None:
    asyncio.run(_policy_change_relabels_unchanged_content())


async def _policy_change_relabels_unchanged_content() -> None:
    manifests = MemoryManifests()
    vectors = MemoryVectors()
    workflow = DocumentIngestionWorkflow(
        Settings(_env_file=None, chunk_max_tokens=5, chunk_overlap_tokens=1),
        manifests,
        vectors,
    )
    vector_store = VectorStoreRoute("project-intelligence", "chunk_text")
    department_policy = "department:DEMO:STORE_OPERATIONS"
    matching_rule = SourceAccessRule(
        provider="CONFLUENCE",
        match_field="TITLE",
        prefix="POS",
        access_policy_id=department_policy,
    )

    first = await workflow.run(document(), "space:T20", "scan-1", vector_store)
    relabelled = await workflow.run(
        document(),
        "space:T20",
        "scan-2",
        vector_store,
        source_access_rules=(matching_rule,),
    )
    restored_to_shared = await workflow.run(
        document(), "space:T20", "scan-3", vector_store
    )

    assert first.operation == "INDEXED"
    assert relabelled.operation == "INDEXED"
    assert restored_to_shared.operation == "INDEXED"
    assert vectors.writes == 3
    assert vectors.access_policy_ids == [
        "project:DEMO",
        department_policy,
        "project:DEMO",
    ]
    assert manifests.value.access_policy_id == "project:DEMO"


def test_irrelevant_rule_change_keeps_unchanged_content_skippable() -> None:
    asyncio.run(_irrelevant_rule_change_keeps_unchanged_content_skippable())


async def _irrelevant_rule_change_keeps_unchanged_content_skippable() -> None:
    manifests = MemoryManifests()
    vectors = MemoryVectors()
    workflow = DocumentIngestionWorkflow(
        Settings(_env_file=None, chunk_max_tokens=5, chunk_overlap_tokens=1),
        manifests,
        vectors,
    )
    vector_store = VectorStoreRoute("project-intelligence", "chunk_text")
    unrelated_rule = SourceAccessRule(
        provider="CONFLUENCE",
        match_field="TITLE",
        prefix="BOT",
        access_policy_id="department:DEMO:STORE_OPERATIONS",
    )

    await workflow.run(document(), "space:T20", "scan-1", vector_store)
    unchanged = await workflow.run(
        document(),
        "space:T20",
        "scan-2",
        vector_store,
        source_access_rules=(unrelated_rule,),
    )

    assert unchanged.operation == "UNCHANGED"
    assert vectors.writes == 1


def test_legacy_manifest_without_policy_is_rewritten_once() -> None:
    asyncio.run(_legacy_manifest_without_policy_is_rewritten_once())


async def _legacy_manifest_without_policy_is_rewritten_once() -> None:
    manifests = MemoryManifests()
    vectors = MemoryVectors()
    workflow = DocumentIngestionWorkflow(
        Settings(_env_file=None, chunk_max_tokens=5, chunk_overlap_tokens=1),
        manifests,
        vectors,
    )
    vector_store = VectorStoreRoute("project-intelligence", "chunk_text")

    await workflow.run(document(), "space:T20", "scan-1", vector_store)
    manifests.value = SourceManifest(
        **{
            field: getattr(manifests.value, field)
            for field in SourceManifest.__dataclass_fields__
            if field != "access_policy_id"
        },
        access_policy_id="",
    )
    rewritten = await workflow.run(document(), "space:T20", "scan-2", vector_store)
    stable = await workflow.run(document(), "space:T20", "scan-3", vector_store)

    assert rewritten.operation == "INDEXED"
    assert stable.operation == "UNCHANGED"
    assert vectors.writes == 2
    assert manifests.value.access_policy_id == "project:DEMO"


def test_full_rebuild_forces_unchanged_content_to_be_reembedded() -> None:
    asyncio.run(_full_rebuild_forces_unchanged_content_to_be_reembedded())


async def _full_rebuild_forces_unchanged_content_to_be_reembedded() -> None:
    manifests = MemoryManifests()
    vectors = MemoryVectors()
    workflow = DocumentIngestionWorkflow(
        Settings(_env_file=None, chunk_max_tokens=5, chunk_overlap_tokens=1),
        manifests,
        vectors,
    )
    vector_store = VectorStoreRoute("project-intelligence", "chunk_text")

    first = await workflow.run(document(), "space:T20", "scan-1", vector_store)
    rebuilt = await workflow.run(
        document(), "space:T20", "scan-2", vector_store, force=True
    )

    assert first.operation == "INDEXED"
    assert rebuilt.operation == "INDEXED"
    assert vectors.writes == 2
    assert manifests.touched == 0


async def _unchanged_content_is_not_reembedded() -> None:
    manifests = MemoryManifests()
    vectors = MemoryVectors()
    workflow = DocumentIngestionWorkflow(
        Settings(_env_file=None, chunk_max_tokens=5, chunk_overlap_tokens=1),
        manifests,
        vectors,
    )
    vector_store = VectorStoreRoute("project-intelligence", "chunk_text")

    first = await workflow.run(document(), "space:T20", "scan-1", vector_store)
    second = await workflow.run(document(), "space:T20", "scan-2", vector_store)

    assert first.operation == "INDEXED"
    assert first.chunks_written > 1
    assert second.operation == "UNCHANGED"
    assert vectors.writes == 1
    assert manifests.touched == 1
    assert manifests.value.access_policy_id == "project:DEMO"


def test_deleted_source_is_removed_from_chroma() -> None:
    asyncio.run(_deleted_source_is_removed_from_chroma())


async def _deleted_source_is_removed_from_chroma() -> None:
    manifests = MemoryManifests()
    vectors = MemoryVectors()
    workflow = DocumentIngestionWorkflow(
        Settings(_env_file=None), manifests, vectors
    )
    vector_store = VectorStoreRoute("project-intelligence", "chunk_text")
    await workflow.run(document(), "space:T20", "scan-1", vector_store)

    result = await workflow.run(document(deleted=True), "space:T20", "scan-2", vector_store)

    assert result.operation == "DELETED"
    assert vectors.deletes == 1
    assert manifests.value.deleted is True
    assert manifests.value.access_policy_id == "project:DEMO"
