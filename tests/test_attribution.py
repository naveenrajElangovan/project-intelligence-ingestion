"""Every chunk must be attributable to a section, or a citation tells the reader
nothing and the chunk cannot be filtered by structure.

15.3% of a freshly indexed Confluence corpus had an empty heading trail: content
that precedes a document's first heading has no trail of its own, and a
Confluence page opens with prose under a title that lives outside the body.
"""

from datetime import UTC, datetime

from app.config import Settings
from app.models import SourceDocument
from app.structured_chunking import StructuredDocumentChunker


def _document(content: str, mime_type: str, title: str = "POS-RAG-04 Data and Events") -> SourceDocument:
    return SourceDocument(
        project_id="DEMO",
        provider="CONFLUENCE",
        source_id="page:9",
        source_type="PAGE",
        title=title,
        reference="9",
        source_url="https://example.invalid",
        version="1",
        content=content,
        updated_at=datetime.now(UTC),
        metadata={},
        mime_type=mime_type,
    )


def _chunks(document: SourceDocument):
    return StructuredDocumentChunker(Settings(_env_file=None)).split(document)


def test_html_content_before_the_first_heading_inherits_the_title():
    chunks = _chunks(
        _document(
            "<p>The cart supports a normal sale and cancellation.</p>"
            "<h2>[ARCH-110] Coroutines</h2><p>Flow carries updates.</p>",
            "text/html",
        )
    )

    assert chunks
    assert all(chunk.structure_path for chunk in chunks)
    assert chunks[0].structure_path == ("POS-RAG-04 Data and Events",)


def test_html_semantic_blocks_preserve_table_lists_and_code() -> None:
    chunks = _chunks(
        _document(
            "<h2>Contract</h2>"
            "<table><tr><th>Event</th><th>Owner</th></tr>"
            "<tr><td>SHIFT_CLOSED</td><td>Store</td></tr></table>"
            "<ol><li>Publish event</li><li>Confirm receipt</li></ol>"
            "<pre>val event = SHIFT_CLOSED</pre>",
            "text/html",
        )
    )

    combined = "\n".join(chunk.content for chunk in chunks)
    assert "| Event | Owner |" in combined
    assert "| --- | --- |" in combined
    assert "1. Publish event" in combined
    assert "2. Confirm receipt" in combined
    assert "```" in combined


def test_markdown_preamble_inherits_the_title():
    chunks = _chunks(
        _document("Introductory prose.\n\n## [EVT-001] Section\n\nBody.", "text/markdown")
    )

    assert all(chunk.structure_path for chunk in chunks)


def test_a_real_heading_still_wins_over_the_title():
    chunks = _chunks(_document("## [EVT-001] Events\n\nBody text.", "text/markdown"))

    assert chunks[0].structure_path[-1].startswith("[EVT-001]")


def test_a_titleless_document_is_left_alone_rather_than_given_an_empty_label():
    # Fabricating a path from an empty title would be worse than none: it would
    # read as a real section that does not exist.
    chunks = _chunks(_document("Plain prose with no heading at all.", "text/plain", title=""))

    assert chunks
    assert chunks[0].structure_path == ()
