import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.models import SourceChunk, SourceDocument
from app.projects import VectorStoreRoute
from app.vector import ChromaVectorStore


class FakeEmbedder:
    def embed_passages(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeCollection:
    def __init__(self, *, fail_upsert: bool = False) -> None:
        self.fail_upsert = fail_upsert
        self.events: list[str] = []

    def get(self, *, where=None, ids=None, include=None):
        if ids is not None:
            self.events.append("verify")
            return {"ids": list(ids)}
        self.events.append("read-prior")
        return {"ids": ["obsolete-generation"]}

    def upsert(self, *, ids, embeddings, documents, metadatas):
        self.events.append("upsert")
        if self.fail_upsert:
            raise RuntimeError("simulated interrupted upsert")

    def delete(self, *, ids=None, where=None):
        self.events.append("delete")


def _document() -> SourceDocument:
    return SourceDocument(
        project_id="DEMO",
        provider="FUTURE_CONNECTOR",
        source_id="source-1",
        source_type="CUSTOM_ARTIFACT",
        title="Source",
        reference="source-1",
        source_url="local://source-1",
        version="1",
        content="content",
        updated_at=datetime.now(UTC),
    )


def _chunk() -> SourceChunk:
    return SourceChunk("chunk-1", "source-1", 0, "content", "hash")


def _store(collection: FakeCollection) -> ChromaVectorStore:
    store = ChromaVectorStore("localhost", 8000, "project-intelligence", FakeEmbedder())
    store._collection = lambda _project_id: collection
    return store


def test_replacement_is_verified_before_obsolete_vectors_are_deleted() -> None:
    collection = FakeCollection()
    asyncio.run(
        _store(collection).replace_document(
            VectorStoreRoute("project-intelligence", "chunk_text"),
            _document(),
            (_chunk(),),
        )
    )
    assert collection.events == ["read-prior", "upsert", "verify", "delete"]


def test_interrupted_upsert_never_deletes_existing_generation() -> None:
    collection = FakeCollection(fail_upsert=True)
    with pytest.raises(RuntimeError, match="interrupted upsert"):
        asyncio.run(
            _store(collection).replace_document(
                VectorStoreRoute("project-intelligence", "chunk_text"),
                _document(),
                (_chunk(),),
            )
        )
    assert collection.events == ["read-prior", "upsert"]


def test_project_vocabulary_is_rebuilt_from_persisted_chunk_metadata() -> None:
    class VocabularyCollection:
        def __init__(self) -> None:
            self.written = None

        def get(self, *, where=None, include=None, limit=None, offset=0):
            assert where is not None
            return {
                "metadatas": [
                    {
                        "entity": "atlas",
                        "doc_category": "feature-page",
                        "provider": "CONFLUENCE",
                        "source_type": "PAGE",
                        "language": "en",
                    },
                    {
                        "entity": "nova",
                        "doc_category": "workflow",
                        "provider": "GITHUB",
                        "source_type": "CODE",
                        "path": "src/main.rs",
                        "language": "es",
                    },
                ]
            }

        def upsert(self, **kwargs):
            self.written = kwargs

    collection = VocabularyCollection()
    store = _store(collection)
    asyncio.run(
        store.refresh_project_vocabulary(
            VectorStoreRoute("project-intelligence", "chunk_text"), "DEMO"
        )
    )

    assert collection.written["ids"] == ["__vocabulary__"]
    vocabulary = json.loads(collection.written["documents"][0])
    assert vocabulary["entities"] == ["atlas", "nova"]
    assert vocabulary["code_extensions"] == [".rs"]
    assert vocabulary["languages"] == ["en", "es"]
    assert collection.written["metadatas"][0]["record_kind"] == "__vocabulary__"
