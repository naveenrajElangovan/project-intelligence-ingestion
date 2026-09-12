"""Chroma-backed local vector storage for ingestion output."""
import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from statistics import median
from pathlib import Path
from app.access_rules import resolve_access_policy
from app.chroma_collections import (
    project_collection_metadata,
    project_collection_name,
    verify_project_collection,
)
from app.models import SourceChunk, SourceDocument
from app.projects import SourceAccessRule, VectorStoreRoute

_RESERVED_METADATA_KEYS = {
    "access_policy_id", "canonical_chunk_id", "chunk_ordinal", "content_hash",
    "embedding_model", "language", "locator", "mime_type", "parent_id",
    "project_id", "provider", "reference", "schema_version",
    "security_classification", "source_id", "source_type", "source_url",
    "source_version", "structure_hash", "structure_path", "title",
    "visual_asset_ids", "visual_eligible", "visual_types",
}

_VOCABULARY_RECORD_KIND = "__vocabulary__"
_VOCABULARY_PAGE_SIZE = 500
logger = logging.getLogger(__name__)

class ChromaVectorStore:
    def __init__(self, host: str, port: int, collection_name: str, embedder: object) -> None:
        self._host, self._port, self._collection_name, self._embedder = host, port, collection_name, embedder
    def embedding_model(self):
        return self._embedder.sentence_transformer()
    def _collection(self, project_id: str, logical_collection: str):
        from chromadb import HttpClient
        collection = HttpClient(host=self._host, port=self._port).get_or_create_collection(
            project_collection_name(logical_collection, project_id),
            metadata=project_collection_metadata(logical_collection, project_id),
        )
        verify_project_collection(collection, logical_collection, project_id)
        return collection
    async def ready(self) -> bool:
        from chromadb import HttpClient
        await _with_retry(lambda: HttpClient(host=self._host, port=self._port).heartbeat())
        return True
    async def replace_document(
        self,
        mapping: VectorStoreRoute,
        document: SourceDocument,
        chunks: tuple[SourceChunk, ...],
        source_access_rules: tuple[SourceAccessRule, ...] = (),
        access_policy_id: str | None = None,
    ) -> int:
        collection = await _with_retry(
            lambda: self._collection(document.project_id, mapping.collection_name)
        )
        where = _source_filter(document.project_id, document.provider, document.source_id)
        prior = await _with_retry(
            lambda: collection.get(where=where, include=["metadatas"])
        )
        prior_ids = set(prior.get("ids") or [])
        if not chunks:
            await _with_retry(lambda: collection.delete(where=where))
            return 0
        prior_by_canonical = {
            str(metadata.get("canonical_chunk_id") or ""): storage_id
            for storage_id, metadata in zip(
                prior.get("ids") or [], prior.get("metadatas") or []
            )
            if isinstance(metadata, dict) and metadata.get("canonical_chunk_id")
        }
        storage_ids = []
        changed_chunks = []
        changed_ids = []
        retained_ids = []
        retained_metadata = []
        for chunk in chunks:
            if document.provider in {"JIRA", "CONFLUENCE"}:
                storage_id = prior_by_canonical.get(chunk.chunk_id, chunk.chunk_id)
            else:
                storage_id = _storage_id(document, chunk.ordinal)
            storage_ids.append(storage_id)
            metadata = _metadata(
                mapping, document, chunk, source_access_rules,
                access_policy_id=access_policy_id,
            )
            if storage_id in prior_ids and document.provider in {"JIRA", "CONFLUENCE"}:
                retained_ids.append(storage_id)
                retained_metadata.append(metadata)
            else:
                changed_ids.append(storage_id)
                changed_chunks.append((chunk, metadata))
        if changed_chunks:
            vectors = await asyncio.to_thread(
                self._embedder.embed_passages,
                [chunk.embedding_text or chunk.content for chunk, _ in changed_chunks],
            )
            await _with_retry(
                lambda: collection.upsert(
                    ids=changed_ids,
                    embeddings=vectors,
                    documents=[chunk.content for chunk, _ in changed_chunks],
                    metadatas=[metadata for _, metadata in changed_chunks],
                )
            )
        if retained_ids:
            await _with_retry(
                lambda: collection.update(ids=retained_ids, metadatas=retained_metadata)
            )
        written = await _with_retry(lambda: collection.get(ids=storage_ids, include=[]))
        if len(written.get("ids") or []) != len(storage_ids):
            raise RuntimeError("Chroma did not persist the complete replacement generation.")
        obsolete = sorted(prior_ids.difference(storage_ids))
        if obsolete:
            await _with_retry(lambda: collection.delete(ids=obsolete))
        return len(changed_ids)
    async def delete_document(self, mapping: VectorStoreRoute, project_id: str, provider: str, source_id: str) -> None:
        collection = await _with_retry(
            lambda: self._collection(project_id, mapping.collection_name)
        )
        await _with_retry(lambda: collection.delete(where=_source_filter(project_id, provider, source_id)))
    async def delete_provider(self, mapping: VectorStoreRoute, project_id: str, provider: str) -> bool:
        collection = await _with_retry(
            lambda: self._collection(project_id, mapping.collection_name)
        )
        await _with_retry(
            lambda: collection.delete(
                where={"$and": [{"project_id": project_id}, {"provider": provider}]}
            )
        )
        return True

    async def refresh_project_vocabulary(
        self, mapping: VectorStoreRoute, project_id: str
    ) -> None:
        """Rebuild the reserved vocabulary from persisted project metadata."""

        collection = await _with_retry(
            lambda: self._collection(project_id, mapping.collection_name)
        )
        metadatas: list[dict[str, object]] = []
        offset = 0
        where = {
            "project_id": project_id,
        }
        while True:
            page = await _with_retry(
                lambda: collection.get(
                    where=where,
                    include=["metadatas"],
                    limit=_VOCABULARY_PAGE_SIZE,
                    offset=offset,
                )
            )
            values = [dict(value or {}) for value in page.get("metadatas", [])]
            metadatas.extend(
                value
                for value in values
                if value.get("record_kind") != _VOCABULARY_RECORD_KIND
            )
            if len(values) < _VOCABULARY_PAGE_SIZE:
                break
            offset += len(values)

        by_policy: dict[str, list[dict[str, object]]] = {}
        for metadata in metadatas:
            policy = str(metadata.get("access_policy_id") or "")
            if policy:
                by_policy.setdefault(policy, []).append(metadata)
        active_ids = []
        for policy, values in sorted(by_policy.items()):
            vocabulary = _observed_vocabulary(project_id, values)
            document = json.dumps(vocabulary, sort_keys=True, separators=(",", ":"))
            record_id = _VOCABULARY_RECORD_KIND if policy == f"project:{project_id}" else _VOCABULARY_RECORD_KIND + ":" + hashlib.sha256(policy.encode()).hexdigest()
            metadata = {
                "record_kind": _VOCABULARY_RECORD_KIND,
                "canonical_chunk_id": _VOCABULARY_RECORD_KIND, "project_id": project_id,
                "access_policy_id": policy, "source_id": f"vocabulary:{project_id}:{policy}",
                "source_type": "SYSTEM", "provider": "INGESTION", "title": "Project vocabulary",
                "reference": record_id, "schema_version": mapping.schema_version,
                "embedding_model": mapping.embedding_model,
            }
            # Control metadata is fetched by ID/policy, never semantic search.
            # A constant passage avoids truncating a growing vocabulary JSON.
            vector = await asyncio.to_thread(self._embedder.embed_passages, ["passage: Project routing vocabulary"])
            await _with_retry(lambda: collection.upsert(ids=[record_id], embeddings=vector, documents=[document], metadatas=[metadata]))
            active_ids.append(record_id)
        old = await _with_retry(lambda: collection.get(where={"$and": [{"project_id": project_id}, {"record_kind": _VOCABULARY_RECORD_KIND}]}, include=[]))
        obsolete = sorted(set(old.get("ids") or []).difference(active_ids))
        if obsolete:
            await _with_retry(lambda: collection.delete(ids=obsolete))


async def _with_retry(operation, attempts: int = 3):
    for attempt in range(attempts):
        try:
            return await asyncio.to_thread(operation)
        except Exception as error:
            status = getattr(error, "status", None) or getattr(error, "status_code", None)
            transient = isinstance(error, (ConnectionError, TimeoutError, OSError)) or status in {408, 429, 500, 502, 503, 504}
            if not transient or attempt + 1 >= attempts:
                raise
            await asyncio.sleep(0.25 * (2**attempt))


def _source_filter(project_id: str, provider: str, source_id: str) -> dict[str, object]:
    return {
        "$and": [
            {"project_id": project_id},
            {"provider": provider},
            {"source_id": source_id},
        ]
    }
def _storage_id(document: SourceDocument, ordinal: int) -> str:
    identity = f"{document.project_id}|{document.provider}|{document.source_id}|{ordinal}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
def _scalar(value: object) -> str | int | float | bool:
    return value if isinstance(value, (str, int, float, bool)) else json.dumps(value, sort_keys=True, default=str)
def _metadata(
    mapping: VectorStoreRoute,
    document: SourceDocument,
    chunk: SourceChunk,
    source_access_rules: tuple[SourceAccessRule, ...] = (),
    *,
    access_policy_id: str | None = None,
) -> dict[str, object]:
    reserved = {*_RESERVED_METADATA_KEYS, mapping.embedding_field}
    extra: dict[str, object] = {}
    for source in (document.metadata, chunk.metadata):
        for key, value in source.items():
            safe = re.sub(r"[^A-Za-z0-9_]", "_", key)[:64]
            if safe in reserved:
                raise ValueError(f"Source metadata cannot override reserved key: {safe}")
            if safe and not safe.startswith("_") and value is not None:
                extra[safe] = _scalar(value)
    path = tuple(str(value) for value in chunk.structure_path if str(value))
    values: dict[str, object] = {
        **extra,
        mapping.embedding_field: chunk.embedding_text or chunk.content,
        "canonical_chunk_id": chunk.chunk_id,
        "project_id": document.project_id,
        "access_policy_id": access_policy_id
        or resolve_access_policy(
            source_access_rules,
            document,
            f"project:{document.project_id}",
        ),
        "provider": document.provider,
        "source_id": document.source_id,
        "source_type": document.source_type,
        "source_version": document.version,
        "title": document.title,
        "reference": document.reference,
        "source_url": document.source_url,
        "chunk_ordinal": chunk.ordinal,
        "content_hash": chunk.content_hash,
        "structure_hash": chunk.structure_hash,
        "parent_id": document.source_id,
        "language": chunk.language,
        "structure_path": _scalar(path),
        "structure_root": path[0] if path else "",
        "structure_leaf": path[-1] if path else "",
        "locator": chunk.locator or "",
        "visual_eligible": chunk.visual_eligible,
        "visual_types": _scalar(chunk.visual_types),
        "visual_type": chunk.visual_types[0] if chunk.visual_types else "",
        "visual_asset_ids": _scalar(chunk.visual_asset_ids),
        "visual_asset_id": chunk.visual_asset_ids[0] if chunk.visual_asset_ids else "",
        "page_number": chunk.page_number or 0,
        "mime_type": document.mime_type,
        "security_classification": document.security_classification,
        "schema_version": mapping.schema_version,
        "embedding_model": mapping.embedding_model,
    }
    return values


def _observed_vocabulary(
    project_id: str, metadatas: list[dict[str, object]]
) -> dict[str, object]:
    def observed(key: str, *, uppercase: bool = False) -> list[str]:
        values = {
            str(metadata.get(key) or "").strip()
            for metadata in metadatas
            if str(metadata.get(key) or "").strip()
        }
        if uppercase:
            values = {value.upper() for value in values}
        return sorted(values, key=str.casefold)

    code_extensions = {
        Path(value).suffix.casefold()
        for metadata in metadatas
        if str(metadata.get("source_type") or "").upper() == "CODE"
        for value in (
            str(metadata.get("path") or ""),
            str(metadata.get("title") or ""),
        )
        if Path(value).suffix
    }
    source_type_counts = Counter(
        str(metadata.get("source_type") or "").strip().upper()
        for metadata in metadatas
        if str(metadata.get("source_type") or "").strip()
    )
    source_chunk_counts = Counter(
        str(metadata.get("source_id") or "").strip()
        for metadata in metadatas
        if str(metadata.get("source_id") or "").strip()
    )
    source_count = len(source_chunk_counts)
    chunk_count = len(metadatas)
    median_chunks = float(median(source_chunk_counts.values())) if source_chunk_counts else 0.0
    # A recommendation is evidence for a human configuration decision, never a
    # runtime override. Narrow/deep corpora start with a wider page allowance.
    recommended_cap = 25 if source_count and median_chunks >= 20 else 12 if median_chunks >= 8 else 3
    def terms():
        result = set()
        for metadata in metadatas:
            if metadata.get("provider") != "JIRA":
                continue
            for name in ("issue_key", "status", "glossary_term", "parent_issue_key"):
                if metadata.get(name):
                    result.add(str(metadata[name]))
            for name in ("labels", "components"):
                value = metadata.get(name) or []
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = [value]
                if isinstance(value, list):
                    result.update(str(v) for v in value if v)
        return sorted(result, key=str.casefold)
    return {
        "jira_terms": terms(),
        "record_kind": _VOCABULARY_RECORD_KIND,
        "project_id": project_id,
        "entities": observed("entity"),
        "doc_categories": observed("doc_category"),
        "providers": observed("provider", uppercase=True),
        "source_types": sorted(
            source_type for source_type, count in source_type_counts.items() if count >= 5
        ),
        "source_type_counts": dict(sorted(source_type_counts.items())),
        "code_extensions": sorted(code_extensions),
        "languages": observed("language"),
        "corpus_stats": {
            "sources": source_count,
            "chunks": chunk_count,
            "median_chunks_per_source": median_chunks,
        },
        "recommended_retrieval_profile": {
            "maxChunksPerSource": recommended_cap,
            "rerankTopN": 8,
            "mixedSourceTopN": 8,
        },
    }
