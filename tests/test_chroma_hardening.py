import pytest

from app.chroma_collections import (
    project_collection_metadata,
    project_collection_name,
    verify_project_collection,
)
from app.models import SourceChunk, SourceDocument
from app.projects import VectorStoreRoute
from app.vector import _metadata


class Collection:
    def __init__(self, name: str, metadata: dict[str, str]) -> None:
        self.name = name
        self.metadata = metadata


def test_project_collection_is_stable_and_verified() -> None:
    name = project_collection_name("project-intelligence", "DEMO")
    collection = Collection(name, project_collection_metadata("project-intelligence", "DEMO"))
    verify_project_collection(collection, "project-intelligence", "DEMO")
    assert name != project_collection_name("project-intelligence", "OTHER")


def test_wrong_metric_or_project_fails_closed() -> None:
    name = project_collection_name("project-intelligence", "DEMO")
    with pytest.raises(RuntimeError):
        verify_project_collection(
            Collection(name, {"project_id": "OTHER", "logical_collection": "project-intelligence", "hnsw:space": "l2"}),
            "project-intelligence",
            "DEMO",
        )


def test_reserved_tenant_metadata_cannot_be_overridden() -> None:
    document = SourceDocument(
        project_id="DEMO", provider="LOCAL", source_id="one", source_type="CODE",
        title="One.kt", reference="One.kt", source_url="file:///One.kt", version="1",
        content="class One", updated_at=None, metadata={"project-id": "OTHER"},
    )
    chunk = SourceChunk("one", "one", 0, "class One", "hash")
    with pytest.raises(ValueError, match="project_id"):
        _metadata(VectorStoreRoute("project-intelligence", "chunk_text"), document, chunk)


def test_filterable_structure_scalars_are_promoted() -> None:
    document = SourceDocument(
        project_id="DEMO", provider="LOCAL", source_id="one", source_type="CODE",
        title="One.kt", reference="One.kt", source_url="file:///One.kt", version="1",
        content="class One", updated_at=None,
    )
    chunk = SourceChunk(
        "one", "one", 0, "class One", "hash", structure_path=("Module", "One")
    )
    values = _metadata(VectorStoreRoute("project-intelligence", "chunk_text"), document, chunk)
    assert values["structure_root"] == "Module"
    assert values["structure_leaf"] == "One"
