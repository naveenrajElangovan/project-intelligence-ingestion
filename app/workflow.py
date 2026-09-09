import asyncio
from dataclasses import replace
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.access_rules import resolve_access_policy
from app.config import Settings
from app.content_security import ContentSecurityScanner, QuarantinedDocument
from app.models import DocumentIndexResult, SourceChunk, SourceDocument, StructuredArtifact
from app.projects import SourceAccessRule, VectorStoreRoute
from app.state import ManifestStore, SourceManifest
from app.structured_chunking import StructuredDocumentChunker
from app.telemetry import observe_document_stage, record_document_result, started
from app.vector import ChromaVectorStore


class IngestionState(TypedDict, total=False):
    document: SourceDocument
    scope: str
    scan_id: str
    vector_store: VectorStoreRoute
    source_access_rules: tuple[SourceAccessRule, ...]
    resolved_access_policy_id: str
    force: bool
    manifest: SourceManifest | None
    operation: Literal["INDEX", "DELETE", "SKIP"]
    chunks: tuple[SourceChunk, ...]
    artifact: StructuredArtifact
    result: DocumentIndexResult


class DocumentIngestionWorkflow:
    """Durable graph boundary for one source document.

    Chroma is updated before the operational manifest. A failed vector write can
    therefore be retried safely without falsely checkpointing the source.
    """

    def __init__(
        self,
        settings: Settings,
        manifests: ManifestStore,
        vectors: ChromaVectorStore,
    ) -> None:
        self._manifests = manifests
        self._vectors = vectors
        self._settings = settings
        self._scanner = ContentSecurityScanner(settings)
        self._chunker = StructuredDocumentChunker(
            settings, embedding_model_loader=getattr(vectors, "embedding_model", None)
        )
        self._conversion_slots = asyncio.Semaphore(settings.docling_max_concurrency)
        graph = StateGraph(IngestionState)
        graph.add_node("inspect", self._inspect)
        graph.add_node("analyze", self._analyze)
        graph.add_node("split", self._split)
        graph.add_node("write", self._write)
        graph.add_node("commit", self._commit)
        graph.add_edge(START, "inspect")
        graph.add_conditional_edges(
            "inspect",
            _route,
            {"INDEX": "analyze", "DELETE": "write", "SKIP": "commit"},
        )
        graph.add_edge("analyze", "split")
        graph.add_edge("split", "write")
        graph.add_edge("write", "commit")
        graph.add_edge("commit", END)
        self._graph = graph.compile()

    async def run(
        self,
        document: SourceDocument,
        scope: str,
        scan_id: str,
        vector_store: VectorStoreRoute,
        *,
        force: bool = False,
        source_access_rules: tuple[SourceAccessRule, ...] = (),
    ) -> DocumentIndexResult:
        try:
            state = await self._graph.ainvoke(
                {
                    "document": document,
                    "scope": scope,
                    "scan_id": scan_id,
                    "vector_store": vector_store,
                    "source_access_rules": source_access_rules,
                    "force": force,
                }
            )
        except QuarantinedDocument as error:
            recorder = getattr(self._manifests, "record_quarantine", None)
            if recorder is not None:
                await recorder(document, scope, scan_id, error.reason.code)
            raise
        return state["result"]

    async def delete_manifest(
        self,
        manifest: SourceManifest,
        scan_id: str,
        vector_store: VectorStoreRoute,
    ) -> DocumentIndexResult:
        document = SourceDocument(
            project_id=manifest.project_id,
            provider=manifest.provider,
            source_id=manifest.source_id,
            source_type=manifest.source_type,
            title=manifest.title,
            reference=manifest.source_id,
            source_url=manifest.source_url,
            version=manifest.version,
            content="",
            updated_at=None,
            deleted=True,
        )
        return await self.run(document, manifest.scope, scan_id, vector_store)

    async def refresh_project_vocabulary(
        self, vector_store: VectorStoreRoute, project_id: str
    ) -> None:
        await self._vectors.refresh_project_vocabulary(vector_store, project_id)

    async def _inspect(self, state: IngestionState) -> IngestionState:
        began = started()
        document = state["document"]
        manifest = await self._manifests.get_manifest(
            document.project_id,
            document.provider,
            state["scope"],
            document.source_id,
        )
        resolved_access_policy_id = resolve_access_policy(
            state.get("source_access_rules", ()),
            document,
            f"project:{document.project_id}",
        )
        if document.deleted:
            operation: Literal["INDEX", "DELETE", "SKIP"] = (
                "SKIP" if manifest is None or manifest.deleted else "DELETE"
            )
        elif not state.get("force", False) and (
            manifest is not None
            and not manifest.deleted
            and manifest.version == document.version
            and manifest.content_hash == document.content_hash
            and manifest.parser_version == self._settings.parser_version
            and manifest.chunker_version == self._settings.chunker_version
            and manifest.embedding_profile == state["vector_store"].embedding_model
            and manifest.schema_version == state["vector_store"].schema_version
            and manifest.access_policy_id == resolved_access_policy_id
        ):
            operation = "SKIP"
        else:
            operation = "INDEX"
        observe_document_stage(document.provider, "inspect", began)
        return {
            "manifest": manifest,
            "operation": operation,
            "resolved_access_policy_id": resolved_access_policy_id,
        }

    async def _analyze(self, state: IngestionState) -> IngestionState:
        began = started()
        document = state["document"]
        credential_sensitive = self._scanner.inspect(document)
        if credential_sensitive:
            document = replace(
                document,
                metadata={**document.metadata, "credential_sensitive": True},
                security_classification="PROJECT_AUTHORIZED_SENSITIVE",
            )
        async with self._conversion_slots:
            artifact = await asyncio.wait_for(
                asyncio.to_thread(self._chunker.analyze, document),
                timeout=self._settings.docling_timeout_seconds,
            )
        observe_document_stage(document.provider, "analyze", began)
        return {"artifact": artifact, "document": document}

    async def _split(self, state: IngestionState) -> IngestionState:
        began = started()
        chunks = await asyncio.to_thread(
            self._chunker.chunk, state["document"], state["artifact"]
        )
        observe_document_stage(state["document"].provider, "split", began)
        return {"chunks": chunks}

    async def _write(self, state: IngestionState) -> IngestionState:
        began = started()
        document = state["document"]
        operation = state["operation"]
        if operation == "DELETE":
            await self._vectors.delete_document(
                state["vector_store"],
                document.project_id,
                document.provider,
                document.source_id,
            )
            observe_document_stage(document.provider, "write", began)
            return {"result": DocumentIndexResult("DELETED")}
        chunks = state.get("chunks", ())
        await self._vectors.replace_document(
            state["vector_store"],
            document,
            chunks,
            access_policy_id=state["resolved_access_policy_id"],
        )
        observe_document_stage(document.provider, "write", began)
        visual = state["artifact"].visual
        return {
            "result": DocumentIndexResult(
                "INDEXED",
                len(chunks),
                visual_eligible=int(visual.eligible),
                visual_assets=0,
                visual_failures=0,
            )
        }

    async def _commit(self, state: IngestionState) -> IngestionState:
        began = started()
        operation = state["operation"]
        manifest = state.get("manifest")
        if operation == "SKIP":
            if manifest is not None:
                await self._manifests.touch_manifest(manifest, state["scan_id"])
            observe_document_stage(state["document"].provider, "commit", began)
            record_document_result(state["document"].provider, "UNCHANGED")
            return {"result": DocumentIndexResult("UNCHANGED")}
        if operation == "DELETE":
            if manifest is not None:
                await self._manifests.mark_deleted(manifest, state["scan_id"])
            observe_document_stage(state["document"].provider, "commit", began)
            record_document_result(state["document"].provider, "DELETED")
            return state
        await self._manifests.save_manifest(
            state["document"],
            state["scope"],
            len(state.get("chunks", ())),
            state["scan_id"],
            parser_version=self._settings.parser_version,
            chunker_version=self._settings.chunker_version,
            embedding_profile=state["vector_store"].embedding_model,
            schema_version=state["vector_store"].schema_version,
            access_policy_id=state["resolved_access_policy_id"],
        )
        observe_document_stage(state["document"].provider, "commit", began)
        record_document_result(
            state["document"].provider,
            "INDEXED",
            len(state.get("chunks", ())),
        )
        return state


def _route(state: IngestionState) -> str:
    return state["operation"]
