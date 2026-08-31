from dataclasses import replace

from app.config import Settings
from app.models import SourceDocument
from app.structured_chunking import StructuredDocumentChunker


def _document(title: str, content: str) -> SourceDocument:
    return SourceDocument(
        project_id="DEMO", provider="FUTURE", source_id=title, source_type="FUTURE",
        title=title, reference=title, source_url="https://example.invalid", version="1",
        content=content, updated_at=None, metadata={}, mime_type="text/markdown",
    )


def test_registry_profile_repeats_header_and_synthesizes_keys() -> None:
    rows = "\n".join(f"| EVENT_{index} | {100 + index} | 1.0 |" for index in range(20))
    chunks = StructuredDocumentChunker(
        Settings(_env_file=None, table_chunk_max_tokens=55, chunk_overlap_tokens=0)
    ).split(_document("Large registry table", "## Events\n| Key | Id | Version |\n|---|---|---|\n" + rows))

    table_chunks = [chunk for chunk in chunks if chunk.metadata["chunk_profile"] == "registry-table"]
    key_chunks = [chunk for chunk in chunks if chunk.metadata["chunk_profile"] == "registry-key-list"]
    assert len(table_chunks) > 1
    assert all(chunk.content.startswith("| Key | Id | Version |\n|---|---|---|") for chunk in table_chunks)
    assert key_chunks and "EVENT_0" in key_chunks[0].content
    assert all(chunk.metadata["row_keys"] for chunk in table_chunks)


def test_workflow_profile_keeps_numbered_sequence_atomic() -> None:
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        _document("BOT-RAG-02 Business Workflows", "## [BOTFLOW-050] Close shift\n1. Count cash.\n2. Confirm.\n3. Publish event.")
    )
    assert len(chunks) == 1
    assert chunks[0].metadata["flow_id"] == "BOTFLOW-050"
    assert chunks[0].metadata["step_count"] == 3
    assert chunks[0].metadata["entity"] == "bot"


def test_code_constants_never_become_project_entities() -> None:
    document = _document("about_config.kt", "const val ABOUT_CONFIG = true")
    document = replace(document, source_type="CODE")
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(document)

    assert chunks
    assert all(chunk.metadata["entity"] == "" for chunk in chunks)


def test_oversized_workflow_splits_at_subsections_and_repeats_identity() -> None:
    content = (
        "Workflow introduction.\n"
        "## [BOTFLOW-020] Close store day\n"
        "### Blockers\n" + ("A blocking condition is checked. " * 45) + "\n"
        "### Reconciliation\n" + ("A balance is reconciled. " * 45)
    )
    chunks = StructuredDocumentChunker(
        Settings(_env_file=None, chunk_max_tokens=120, chunk_overlap_tokens=0)
    ).split(_document("BOT-RAG-02 Business Workflows", content))

    support = [chunk for chunk in chunks if chunk.metadata["chunk_profile"] == "workflow-support"]
    flow_chunks = [chunk for chunk in chunks if chunk.metadata["chunk_profile"] == "workflow"]
    assert support and "Workflow introduction" in support[0].content
    assert len(flow_chunks) > 1
    assert all("[BOTFLOW-020] Close store day" in chunk.content for chunk in flow_chunks)
    assert all(chunk.metadata["flow_id"] == "BOTFLOW-020" for chunk in flow_chunks)


def test_glossary_profile_emits_one_term_per_chunk() -> None:
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        _document("Operations Glossary", "## Folio\nA transaction identifier.\n## Relief\nA cash movement.")
    )
    assert [chunk.metadata["term"] for chunk in chunks] == ["Folio", "Relief"]
    assert all(len(chunk.structure_path) == 1 for chunk in chunks)


def test_index_profile_is_small_and_explicit() -> None:
    content = "# Master Index\n" + "\n".join(f"Section {index}: description" for index in range(150))
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        _document("Project Corpus Guide and Master Index", content)
    )
    assert chunks
    assert all(chunk.metadata["chunk_profile"] == "index" for chunk in chunks)
