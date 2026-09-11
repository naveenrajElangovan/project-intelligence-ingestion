import asyncio
import json

from app.projects import VectorStoreRoute
from app.vector import ChromaVectorStore


def test_vocabulary_isolated_by_policy_and_unlabelled_records_are_excluded():
    class Collection:
        def __init__(self):
            self.written = []
            self.deleted = []

        def get(self, *, where, include, **kwargs):
            if "$and" in where:
                return {"ids": ["obsolete-vocabulary"]}
            return {
                "metadatas": [
                    {
                        "provider": "JIRA",
                        "entity": "pos",
                        "glossary_term": "public-term",
                        "access_policy_id": "project:APP",
                    },
                    {
                        "provider": "JIRA",
                        "entity": "finance",
                        "glossary_term": "restricted-term",
                        "access_policy_id": "department:APP:FINANCE",
                    },
                    {"provider": "JIRA", "entity": "unlabelled-secret"},
                ]
            }

        def upsert(self, **values):
            self.written.append(values)

        def delete(self, *, ids):
            self.deleted.extend(ids)

    class Embedder:
        def embed_passages(self, texts):
            return [[1.0, 0.0] for _ in texts]

    collection = Collection()
    store = ChromaVectorStore("localhost", 8000, "test-collection", Embedder())
    store._collection = lambda _: collection
    asyncio.run(
        store.refresh_project_vocabulary(VectorStoreRoute("test-collection", "chunk_text"), "APP")
    )
    assert len(collection.written) == 2
    for write in collection.written:
        metadata = write["metadatas"][0]
        payload = json.loads(write["documents"][0])
        assert metadata["canonical_chunk_id"] == "__vocabulary__"
        assert "unlabelled-secret" not in payload["entities"]
        if metadata["access_policy_id"] == "project:APP":
            assert "restricted-term" not in payload["jira_terms"]
            assert payload["entities"] == ["pos"]
    assert collection.deleted == ["obsolete-vocabulary"]
