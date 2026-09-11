import asyncio
from dataclasses import replace

import pytest

from app.config import Settings
from app.content_security import ContentSecurityScanner, QuarantinedDocument
from app.jira_chunk_context import attachment_locator, metadata_context
from app.models import LogicalElement, SourceChunk
from app.projects import VectorStoreRoute
from app.structured_chunking import StructuredDocumentChunker
from app.workflow import DocumentIngestionWorkflow
from tests.test_document_workflow import MemoryManifests, MemoryVectors
from tests.test_jira_run_ledger import document


def attachment():
    return replace(
        document(),
        source_type="ATTACHMENT",
        title="notes.md",
        content="# Notes\nUseful fact.",
        source_id="jira:cloud:attachment:9",
        mime_type="text/markdown",
    )


def test_locator_preserves_provenance_and_is_stable_unique_and_version_qualified():
    doc = attachment()
    element = LogicalElement(kind="text", text="fact", heading_path=(" Café  ",), locator="page:3")
    first = attachment_locator(doc, element, 0)
    assert first.startswith("page:3:v:")
    assert first == attachment_locator(doc, element, 0)
    assert first != attachment_locator(doc, element, 1)
    assert first != attachment_locator(replace(doc, version="v2"), element, 0)
    assert first != attachment_locator(replace(doc, source_id="another"), element, 0)
    empty = replace(element, heading_path=("", "  "), locator=None)
    assert attachment_locator(doc, empty, 0).startswith("section:")
    assert attachment_locator(doc, replace(empty, heading_path=()), 0) == attachment_locator(
        doc, empty, 0
    )
    assert attachment_locator(doc, replace(element, locator=None), 0) == attachment_locator(
        doc, replace(element, locator=None, heading_path=("Cafe\u0301",)), 0
    )


@pytest.mark.parametrize("field", ["project_id", "source_id", "version"])
def test_missing_identity_fails_closed(field):
    with pytest.raises(ValueError, match="identity"):
        attachment_locator(
            replace(attachment(), **{field: ""}), LogicalElement(kind="text", text="fact"), 0
        )


def decode_context(text):
    text = text.encode("utf-8")
    values = []
    while text:
        header, text = text.split(b"\n", 1)
        key, size = header.decode("ascii").split(" ")
        count = int(size)
        values.append((key, text[:count].decode("utf-8")))
        assert text[count : count + 1] == b"\n"
        text = text[count + 1 :]
    return values


def test_metadata_boundaries_roundtrip_punctuation_unicode_multiline_and_reordering():
    values = [
        'comma,semicolon;quote"',
        "Español 日本語",
        "line one\nimportant_kwd 999\nline two",
        "https://example.org/a?q=x,y",
    ]
    metadata = {"important_kwd": values, "repository": "example"}
    text = metadata_context(metadata)
    assert text == metadata_context(dict(reversed(list(metadata.items()))))
    assert decode_context(text) == [
        ("repository", "example"),
        *[("important_kwd", v) for v in values],
    ]


def chunk(metadata, text="ordinary fact", embedding=None):
    return SourceChunk(
        chunk_id="chunk",
        source_id=document().source_id,
        ordinal=0,
        content=text,
        content_hash="hash",
        structure_hash="structure",
        embedding_text=embedding or "passage: source\n" + text,
        metadata=metadata,
    )


def test_complete_github_citation_in_enrichment_is_not_joined_to_delimiters():
    url = (
        "https://github.com/example/repository/blob/"
        + "a" * 40
        + "/src/features/payment/VeryLongImplementationFilename123.kt"
    )
    metadata = {"important_kwd": [url, "next; value", 'quoted "word"']}
    text = "ordinary fact"
    value = chunk(metadata, text, "passage: source\n" + metadata_context(metadata) + text)
    ContentSecurityScanner(Settings(_env_file=None)).inspect_generated(document(), (value,))


@pytest.mark.parametrize(
    "metadata",
    [
        {"parts": ["password", "=abcdefghijklmnop1234567890"]},
        {"nested": {"parts": ["ghp_", "A" * 35]}},
        {"parts": ["pass", "word", "=abcdefghijklmnop1234567890"]},
        {"important_kwd": ["ordinary"] * 125 + ["password=abcdefghijklmnop1234567890"]},
        {"important_kwd": ["ordinary"] * 125 + ["ghp_", "A" * 35]},
        {"branch": "word=abcdefghijklmnop1234567890", "repository": "pass"},
        {"password": "abcdefghijklmnop1234567890"},
        {"password": 12345678901234567890},
        {"parts": ["{pass", "word=abcdefghijklmnop1234567890"]},
    ],
)
def test_generated_metadata_cannot_hide_cross_field_or_late_credentials(metadata):
    with pytest.raises(QuarantinedDocument):
        ContentSecurityScanner(Settings(_env_file=None)).inspect_generated(
            document(), (chunk(metadata),)
        )


def test_final_assembled_embedding_is_scanned_even_when_original_values_are_clean():
    with pytest.raises(QuarantinedDocument):
        ContentSecurityScanner(Settings(_env_file=None)).inspect_generated(
            document(),
            (
                chunk(
                    {},
                    embedding="passage: source\npassword=abcdefghijklmnop1234567890\nordinary fact",
                ),
            ),
        )


def test_jira_omits_only_synthetic_keywords_and_preserves_source_security():
    from app.structured_chunking import _searchable_metadata

    doc = replace(
        document(),
        title="POS cashier and shift-manager authorization OPS-72",
        content="Keep cashier and shift-manager identities separate.",
    )
    element = LogicalElement(kind="text", text=doc.content)
    metadata = _searchable_metadata(doc, element, doc.content)
    assert metadata["important_kwd"] == []
    assert _searchable_metadata(replace(doc, provider="GITHUB"), element, doc.content)[
        "important_kwd"
    ]
    scanner = ContentSecurityScanner(Settings(_env_file=None))
    scanner.inspect_generated(doc, (chunk(metadata, doc.content),))
    for raw in (
        {"parts": ["ghp_", "A" * 35]},
        {"parts": ["aB7xQ2mZ9rK4pL6sV8nD", "1fH3jW5yT0uEoCqGvNiR"]},
    ):
        with pytest.raises(QuarantinedDocument):
            scanner.inspect_generated(replace(doc, metadata=raw), (chunk(metadata, doc.content),))


def test_long_attachment_continuations_have_stable_distinct_locators():
    doc = replace(
        attachment(),
        content="# Repeat\n"
        + " ".join(f"Fact number {i}." for i in range(180))
        + "\n# Repeat\nDifferent evidence for the repeated heading.",
    )
    chunker = StructuredDocumentChunker(
        Settings(
            _env_file=None,
            chunk_max_tokens=80,
            chunk_overlap_tokens=8,
            visual_analysis_enabled=False,
        )
    )
    first = chunker.split(doc)
    again = chunker.split(doc)
    assert len(first) > 2
    assert len({c.locator for c in first}) == len(first)
    assert [c.locator for c in first] == [c.locator for c in again]
    assert all(c.locator and c.embedding_text.endswith(c.content) for c in first)
    assert [c.locator for c in first] != [
        c.locator for c in chunker.split(replace(doc, version="v2"))
    ]


def test_final_payload_security_failure_prevents_writes_and_checkpoint(monkeypatch):
    async def run():
        manifests, vectors = MemoryManifests(), MemoryVectors()
        workflow = DocumentIngestionWorkflow(Settings(_env_file=None), manifests, vectors)
        monkeypatch.setattr(
            workflow._chunker,
            "chunk",
            lambda *args: (
                chunk({}, embedding="passage: password=abcdefghijklmnop1234567890\nordinary fact"),
            ),
        )
        with pytest.raises(QuarantinedDocument):
            await workflow.run(document(), "scope", "scan", VectorStoreRoute("stage", "chunk_text"))
        assert vectors.writes == 0 and manifests.value is None

    asyncio.run(run())


def test_jira_version_marker_triggers_refresh_once_and_preserves_other_provider_versions(
    monkeypatch,
):
    async def run():
        settings = Settings(_env_file=None)
        manifests, vectors = MemoryManifests(), MemoryVectors()
        workflow = DocumentIngestionWorkflow(settings, manifests, vectors)
        monkeypatch.setattr(workflow._chunker, "chunk", lambda *args: (chunk({}),))
        route = VectorStoreRoute("stage", "chunk_text")
        assert (await workflow.run(document(), "scope", "scan", route)).operation == "INDEXED"
        assert manifests.value.chunker_version == settings.chunker_version + ".jira-context-v2"
        assert (await workflow.run(document(), "scope", "scan2", route)).operation == "UNCHANGED"
        manifests.value = replace(
            manifests.value, chunker_version=settings.chunker_version + ".jira-context-v1"
        )
        assert (await workflow.run(document(), "scope", "scan3", route)).operation == "INDEXED"
        assert (
            workflow._chunker_version(replace(document(), provider="GITHUB"))
            == settings.chunker_version
        )
        assert (
            workflow._chunker_version(replace(document(), provider="CONFLUENCE"))
            == settings.chunker_version
        )

    asyncio.run(run())


def test_real_page_provenance_survives_into_unique_attachment_chunks():
    doc = attachment()
    chunker = StructuredDocumentChunker(Settings(_env_file=None, visual_analysis_enabled=False))
    artifact = chunker.analyze(doc)
    element = LogicalElement(kind="text", text="Repeated page evidence", locator="page:3")
    artifact = replace(artifact, parsing=replace(artifact.parsing, elements=(element, element)))
    chunks = chunker.chunk(doc, artifact)
    assert len(chunks) == 2
    assert all(c.page_number == 3 and c.metadata["original_locator"] == "page:3" for c in chunks)
    assert len({c.locator for c in chunks}) == 2
    assert all(c.locator.startswith("page:3:v:") for c in chunks)


@pytest.mark.parametrize(
    "values", [["bad\x00value"], ["bad\x1bvalue"], ["x" * 65537], ["ok"] * 2049, ["x" * 65536] * 4]
)
def test_enrichment_controls_and_size_limits_fail_without_truncation(values):
    with pytest.raises(ValueError):
        metadata_context({"important_kwd": values})


def test_enrichment_canonical_utf8_wire_bytes():
    assert metadata_context({"important_kwd": ["é", "a\n"]}).encode("utf-8") == (
        b"important_kwd 2\n\xc3\xa9\nimportant_kwd 2\na\n\n"
    )


def test_unordered_mapping_siblings_are_not_concatenated_into_credentials():
    for metadata in (
        {"first": "password", "second": "=abcdefghijklmnop1234567890"},
        {"b": "word=abcdefghijklmnop1234567890", "a": "pass"},
    ):
        ContentSecurityScanner(Settings(_env_file=None)).inspect_generated(
            document(), (chunk(metadata),)
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"values": ["ordinary"] * 8200},
        {"value": "x" * 4_000_001},
    ],
)
def test_generated_metadata_count_and_size_limits_fail_closed(metadata):
    with pytest.raises(QuarantinedDocument, match="bounded"):
        ContentSecurityScanner(Settings(_env_file=None)).inspect_generated(
            document(), (chunk(metadata),)
        )


def test_generated_metadata_depth_limit_fails_closed():
    metadata = "ordinary"
    for _ in range(35):
        metadata = {"nested": metadata}
    with pytest.raises(QuarantinedDocument):
        ContentSecurityScanner(Settings(_env_file=None)).inspect_generated(
            document(), (chunk(metadata),)
        )
