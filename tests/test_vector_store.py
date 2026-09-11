import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.models import SourceChunk, SourceDocument
from app.projects import VectorStoreRoute
from app.vector import ChromaVectorStore, _observed_vocabulary, _source_filter


class FakeEmbedder:
    def embed_passages(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeCollection:
    def __init__(self, *, fail_upsert: bool = False) -> None:
        self.fail_upsert = fail_upsert
        self.events: list[str] = []
        self.deleted_where = None

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
        self.deleted_where = where


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


def test_provider_delete_is_project_scoped_without_pinning_access_policy() -> None:
    collection = FakeCollection()

    asyncio.run(
        _store(collection).delete_provider(
            VectorStoreRoute("project-intelligence", "chunk_text"),
            "DEMO",
            "CONFLUENCE",
        )
    )

    assert collection.deleted_where == {
        "$and": [{"project_id": "DEMO"}, {"provider": "CONFLUENCE"}]
    }


def test_source_filter_covers_every_policy_inside_one_project_source() -> None:
    where = _source_filter("DEMO", "CONFLUENCE", "page:123")

    assert where == {
        "$and": [
            {"project_id": "DEMO"},
            {"provider": "CONFLUENCE"},
            {"source_id": "page:123"},
        ]
    }
    assert "access_policy_id" not in str(where)


def test_project_vocabulary_is_rebuilt_from_persisted_chunk_metadata() -> None:
    class VocabularyCollection:
        def __init__(self) -> None:
            self.written = None
            self.read_where = None

        def get(self, *, where=None, include=None, limit=None, offset=0):
            assert where is not None
            self.read_where = where
            return {
                "metadatas": [
                    {
                        "access_policy_id": "project:DEMO",
                        "entity": "atlas",
                        "doc_category": "feature-page",
                        "provider": "CONFLUENCE",
                        "source_type": "PAGE",
                        "language": "en",
                    },
                    {
                        "access_policy_id": "project:DEMO",
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
    assert collection.read_where == {"$and": [{"project_id": "DEMO"}, {"record_kind": "__vocabulary__"}]}


def test_vocabulary_requires_five_observations_and_reports_corpus_shape() -> None:
    records = [
        {"source_id": "long-page", "source_type": "PAGE", "language": "en"}
        for _ in range(25)
    ] + [
        {"source_id": "stray-issue", "source_type": "ISSUE", "language": "en"}
    ]

    vocabulary = _observed_vocabulary("DEMO", records)

    assert vocabulary["source_types"] == ["PAGE"]
    assert vocabulary["source_type_counts"] == {"ISSUE": 1, "PAGE": 25}
    assert vocabulary["corpus_stats"] == {
        "sources": 2,
        "chunks": 26,
        "median_chunks_per_source": 13.0,
    }
    assert vocabulary["recommended_retrieval_profile"]["maxChunksPerSource"] == 12
