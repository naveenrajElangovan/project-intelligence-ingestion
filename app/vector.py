"""Chroma-backed local vector storage for ingestion output."""
import asyncio
import hashlib
import json
import re
from pathlib import Path
from app.chroma_collections import (
    project_collection_metadata,
    project_collection_name,
    verify_project_collection,
)
from app.models import SourceChunk, SourceDocument
from app.projects import VectorStoreRoute

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

class ChromaVectorStore:
    def __init__(self, host: str, port: int, collection_name: str, embedder: object) -> None:
        self._host, self._port, self._collection_name, self._embedder = host, port, collection_name, embedder
    def embedding_model(self):
        return self._embedder.sentence_transformer()
    def _collection(self, project_id: str):
        from chromadb import HttpClient
        collection = HttpClient(host=self._host, port=self._port).get_or_create_collection(
            project_collection_name(self._collection_name, project_id),
            metadata=project_collection_metadata(self._collection_name, project_id),
        )
        verify_project_collection(collection, self._collection_name, project_id)
        return collection
    async def ready(self) -> bool:
        from chromadb import HttpClient
        await _with_retry(lambda: HttpClient(host=self._host, port=self._port).heartbeat())
        return True
    async def replace_document(self, mapping: VectorStoreRoute, document: SourceDocument, chunks: tuple[SourceChunk, ...]) -> None:
        if mapping.collection_name != self._collection_name:
            raise ValueError("The project collection does not match the configured Chroma collection.")
        collection = await _with_retry(lambda: self._collection(document.project_id))
        where = _source_filter(document.project_id, document.provider, document.source_id)
        prior = await _with_retry(lambda: collection.get(where=where, include=[]))
        prior_ids = set(prior.get("ids") or [])
        if not chunks:
            await _with_retry(lambda: collection.delete(where=where))
            return
        storage_ids = [_storage_id(document, chunk.ordinal) for chunk in chunks]
        vectors = await asyncio.to_thread(self._embedder.embed_passages, [chunk.embedding_text or chunk.content for chunk in chunks])
        await _with_retry(lambda: collection.upsert(ids=storage_ids, embeddings=vectors, documents=[chunk.content for chunk in chunks], metadatas=[_metadata(mapping, document, chunk) for chunk in chunks]))
        written = await _with_retry(lambda: collection.get(ids=storage_ids, include=[]))
        if len(written.get("ids") or []) != len(storage_ids):
            raise RuntimeError("Chroma did not persist the complete replacement generation.")
        obsolete = sorted(prior_ids.difference(storage_ids))
        if obsolete:
            await _with_retry(lambda: collection.delete(ids=obsolete))
    async def delete_document(self, mapping: VectorStoreRoute, project_id: str, provider: str, source_id: str) -> None:
        collection = await _with_retry(lambda: self._collection(project_id))
        await _with_retry(lambda: collection.delete(where=_source_filter(project_id, provider, source_id)))
    async def delete_provider(self, mapping: VectorStoreRoute, project_id: str, provider: str) -> bool:
        collection = await _with_retry(lambda: self._collection(project_id))
        await _with_retry(lambda: collection.delete(where={"$and": [{"project_id": project_id}, {"access_policy_id": f"project:{project_id}"}, {"provider": provider}]}))
        return True

    async def refresh_project_vocabulary(
        self, mapping: VectorStoreRoute, project_id: str
    ) -> None:
        """Rebuild the reserved vocabulary from persisted project metadata."""

        if mapping.collection_name != self._collection_name:
            raise ValueError("The project collection does not match the configured Chroma collection.")
        collection = await _with_retry(lambda: self._collection(project_id))
        metadatas: list[dict[str, object]] = []
        offset = 0
        where = {
            "$and": [
                {"project_id": project_id},
                {"access_policy_id": f"project:{project_id}"},
            ]
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

        vocabulary = _observed_vocabulary(project_id, metadatas)
        document = json.dumps(vocabulary, sort_keys=True, separators=(",", ":"))
        vector = await asyncio.to_thread(self._embedder.embed_passages, [document])
        metadata = {
            "record_kind": _VOCABULARY_RECORD_KIND,
            "canonical_chunk_id": _VOCABULARY_RECORD_KIND,
            "project_id": project_id,
            "access_policy_id": f"project:{project_id}",
            "source_id": f"vocabulary:{project_id}",
            "source_type": "SYSTEM",
            "provider": "INGESTION",
            "title": "Project vocabulary",
            "reference": f"vocabulary:{project_id}",
            "schema_version": mapping.schema_version,
            "embedding_model": mapping.embedding_model,
            **{
                key: json.dumps(value, separators=(",", ":"))
                for key, value in vocabulary.items()
                if isinstance(value, list)
            },
        }
        await _with_retry(
            lambda: collection.upsert(
                ids=[_VOCABULARY_RECORD_KIND],
                embeddings=vector,
                documents=[document],
                metadatas=[metadata],
            )
        )

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
    return {"$and": [{"project_id": project_id}, {"access_policy_id": f"project:{project_id}"}, {"provider": provider}, {"source_id": source_id}]}
def _storage_id(document: SourceDocument, ordinal: int) -> str:
    identity = f"{document.project_id}|{document.provider}|{document.source_id}|{ordinal}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
def _scalar(value: object) -> str | int | float | bool:
    return value if isinstance(value, (str, int, float, bool)) else json.dumps(value, sort_keys=True, default=str)
def _metadata(mapping: VectorStoreRoute, document: SourceDocument, chunk: SourceChunk) -> dict[str, object]:
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
        "access_policy_id": f"project:{document.project_id}",
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
    return {
        "record_kind": _VOCABULARY_RECORD_KIND,
        "project_id": project_id,
        "entities": observed("entity"),
        "doc_categories": observed("doc_category"),
        "providers": observed("provider", uppercase=True),
        "source_types": observed("source_type", uppercase=True),
        "code_extensions": sorted(code_extensions),
        "languages": observed("language"),
    }
