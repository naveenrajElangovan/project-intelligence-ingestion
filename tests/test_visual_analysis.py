from datetime import UTC, datetime
from dataclasses import replace
from io import BytesIO

from PIL import Image

from app.config import Settings
from app.models import (
    LogicalElement,
    ParsingResult,
    SourceDocument,
    SourceVisualInput,
    StructuredArtifact,
    VisualAnalysis,
)
from app.structured_chunking import StructuredDocumentChunker
from app.visual import analyze_markdown, resolve_markdown_asset_paths


def _document(content: str) -> SourceDocument:
    return SourceDocument(
        project_id="DEMO",
        provider="GITHUB",
        source_id="repository:test:path:architecture.md",
        source_type="CODE",
        title="architecture.md",
        reference="test:main:architecture.md",
        source_url="https://github.com/example/test/blob/main/architecture.md",
        version="abc123",
        content=content,
        updated_at=datetime.now(UTC),
        metadata={"path": "architecture.md"},
        mime_type="text/markdown",
    )


def test_text_only_markdown_skips_visual_processing() -> None:
    result = analyze_markdown(_document("# Runbook\n\nNormal text."), Settings(_env_file=None))
    assert result.eligible is False
    assert result.assets == ()


def test_markdown_table_and_mermaid_are_visual_eligible() -> None:
    content = """# Architecture

| Component | Store |
| --- | --- |
| Ingestion | Chroma |

```mermaid
flowchart LR
  GitHub --> Ingestion --> Chroma
```
"""
    result = analyze_markdown(_document(content), Settings(_env_file=None))
    assert result.eligible is True
    assert set(result.visual_types) == {"architecture_diagram", "table"}
    assert {asset.asset_type for asset in result.assets} >= {
        "markdown_table",
        "mermaid_diagram",
    }


def test_remote_and_active_markdown_images_are_not_resolved() -> None:
    result = analyze_markdown(
        _document("![remote](https://example.com/a.png)\n<img src='diagram.svg'>"),
        Settings(_env_file=None),
    )
    assert result.assets == ()
    assert "UNSAFE_MARKDOWN_IMAGE_REFERENCE" in result.reason_codes


def test_visual_metadata_is_added_before_chroma_indexing() -> None:
    chunks = StructuredDocumentChunker(
        Settings(_env_file=None, visual_analysis_enabled=True)
    ).split(
        _document("# Architecture\n\n```mermaid\nflowchart LR\nA --> B\n```")
    )
    assert chunks
    assert chunks[0].visual_eligible is True
    assert chunks[0].visual_asset_ids
    assert "VISUAL TYPE" in chunks[0].embedding_text


def test_only_safe_repository_local_images_are_resolved_and_extracted() -> None:
    assert resolve_markdown_asset_paths(
        "![ok](images/flow.png) ![bad](../../secret.png) ![web](https://x/a.png)",
        "docs/architecture.md",
    ) == ("docs/images/flow.png",)
    output = BytesIO()
    Image.new("RGB", (256, 256), "navy").save(output, format="PNG")
    document = replace(
        _document("![flow](images/flow.png)"),
        local_visuals=(SourceVisualInput("images/flow.png", "image/png", output.getvalue()),),
    )
    result = analyze_markdown(document, Settings(_env_file=None))
    assert result.eligible is True
    assert result.assets[0].media_type == "image/webp"
    assert result.assets[0].locator == "images/flow.png"


def test_binary_document_is_converted_once_before_chunking(monkeypatch) -> None:
    chunker = StructuredDocumentChunker(Settings(_env_file=None))
    binary = replace(_document(""), title="design.pdf", mime_type="application/pdf", content_bytes=b"pdf")
    calls = 0

    def artifact(_document):
        nonlocal calls
        calls += 1
        return StructuredArtifact(
            ParsingResult(
                "3",
                "test-parser",
                binary.source_id,
                binary.version,
                binary.mime_type,
                "en",
                "PROJECT_INTERNAL",
                (LogicalElement("paragraph", "Architecture evidence."),),
            ),
            VisualAnalysis(False),
        )

    monkeypatch.setattr(chunker, "_docling_artifact", artifact)
    parsed = chunker.analyze(binary)
    chunks = chunker.chunk(binary, parsed)
    assert calls == 1
    assert chunks


def test_confluence_html_table_is_analyzed_before_chunking() -> None:
    document = replace(
        _document("<table><tr><th>Service</th><th>Store</th></tr><tr><td>RAG</td><td>Chroma</td></tr></table>"),
        title="Architecture",
        mime_type="text/html",
        metadata={"page_id": "44"},
    )
    chunks = StructuredDocumentChunker(
        Settings(_env_file=None, visual_analysis_enabled=True)
    ).split(document)
    assert chunks
    assert chunks[0].visual_eligible is True
    assert chunks[0].visual_types == ("html_table",)
