import pytest
from dataclasses import replace

from app.config import Settings
from app.content_security import ContentSecurityScanner, QuarantinedDocument
from app.models import SourceDocument, SourceVisualInput
from app.structured_chunking import StructuredDocumentChunker


def source(content: str, path: str, mime_type: str = "text/plain") -> SourceDocument:
    return SourceDocument(
        project_id="DEMO",
        provider="GITHUB",
        source_id=f"repo:{path}",
        source_type="FILE",
        title=path,
        reference=path,
        source_url=f"https://example.invalid/{path}",
        version="abc123",
        content=content,
        updated_at=None,
        metadata={"path": path},
        mime_type=mime_type,
    )


def test_markdown_preserves_heading_context_without_polluting_evidence() -> None:
    chunks = StructuredDocumentChunker(
        Settings(_env_file=None, chunk_max_tokens=64, chunk_overlap_tokens=8)
    ).split(source("# Arquitectura\n\n## Seguridad\n\nEl filtro del proyecto siempre se aplica.", "README.md", "text/markdown"))

    assert chunks
    assert chunks[0].structure_path == ("Arquitectura", "Seguridad")
    assert "README.md > Arquitectura > Seguridad" in chunks[0].embedding_text
    assert chunks[0].content.startswith("# Arquitectura")
    assert chunks[0].language == "es"


def test_csv_repeats_headers_and_stable_ids() -> None:
    document = source("id,status,owner\nT2,active,Ana\nT3,blocked,Luis", "work.csv", "text/csv")
    settings = Settings(_env_file=None, table_chunk_max_tokens=2)
    chunker = StructuredDocumentChunker(settings)

    first = chunker.split(document)
    second = chunker.split(document)

    assert len(first) == 2
    assert all(chunk.content.startswith("id,status,owner") for chunk in first)
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]


def test_code_chunk_records_symbol() -> None:
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        source("def authorize_project(project_id):\n    return project_id == 'DEMO'", "auth.py")
    )

    assert chunks[0].locator == "authorize_project"
    assert chunks[0].metadata["symbol"] == "authorize_project"


def test_code_chunk_embedding_contains_generic_file_and_keyword_context() -> None:
    document = source(
        "class CashReliefConfirmationModal:\n    def close_shift(self):\n        return True",
        "core/features/shift/CashReliefConfirmationModal.py",
    )

    chunk = StructuredDocumentChunker(Settings(_env_file=None)).split(document)[0]

    assert chunk.structure_path[0] == "CashReliefConfirmationModal.py"
    assert chunk.metadata["file_name"] == "CashReliefConfirmationModal.py"
    assert "CashReliefConfirmationModal" in chunk.metadata["symbols"]
    assert "PATH: core/features/shift/CashReliefConfirmationModal.py" in chunk.embedding_text
    assert "KEYWORDS:" in chunk.embedding_text
    assert chunk.content.startswith("class CashReliefConfirmationModal")


def test_kotlin_code_chunk_records_modified_class_symbol() -> None:
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        source(
            "@Singleton\ninternal data class VehicleRepositoryImpl(val source: Any)",
            "VehicleRepositoryImpl.kt",
        )
    )

    assert chunks[0].locator == "VehicleRepositoryImpl"
    assert chunks[0].metadata["symbol"] == "VehicleRepositoryImpl"


def test_kotlin_annotations_are_attached_to_the_declaration() -> None:
    chunks = StructuredDocumentChunker(
        Settings(_env_file=None, code_chunk_max_tokens=20, chunk_overlap_tokens=0)
    ).split(
        source(
            "@Composable\nfun CheckoutScreen() {\n    renderCheckout()\n}\n",
            "CheckoutScreen.kt",
        )
    )

    assert chunks
    assert all(chunk.content != "@Composable" for chunk in chunks)
    assert chunks[0].content.startswith("@Composable\nfun CheckoutScreen")


def test_identifier_dense_kotlin_windows_respect_token_budget() -> None:
    settings = Settings(
        _env_file=None, code_chunk_max_tokens=48, chunk_overlap_tokens=8
    )
    chunker = StructuredDocumentChunker(settings)
    content = "\n".join(
        f"const val POS_CLOSE_SHIFT_REQUEST_EVENT_{index} = \"pos.close.shift.{index}\""
        for index in range(80)
    )

    chunks = chunker.split(source(content, "ShiftEvents.kt"))

    assert len(chunks) > 1
    assert all(
        chunker._count_tokens(chunk.content) <= settings.code_chunk_max_tokens
        for chunk in chunks
    )


def test_duplicate_bodies_within_one_source_are_committed_once() -> None:
    chunks = StructuredDocumentChunker(
        Settings(_env_file=None, code_chunk_max_tokens=8, chunk_overlap_tokens=0)
    ).split(source("@Composable\n\n@Composable\n", "Duplicate.kt"))

    assert len({chunk.content_hash for chunk in chunks}) == len(chunks)


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (source("x", "payload.zip"), "BLOCKED_FILE_TYPE"),
        (
            SourceDocument(
                project_id="DEMO", provider="GITHUB", source_id="bad", source_type="FILE",
                title="spoof.pdf", reference="spoof.pdf", source_url="https://example.invalid",
                version="1", content="", updated_at=None, metadata={"path": "spoof.pdf"},
                mime_type="application/pdf", content_bytes=b"not-a-pdf",
            ),
            "MIME_MISMATCH",
        ),
        (source("api_key = 'abcdefghijklmnopqrstuvwxyz123456'", "secrets.txt"), "POTENTIAL_SECRET"),
        (
            SourceDocument(
                project_id="DEMO", provider="GITHUB", source_id="locked", source_type="FILE",
                title="locked.pdf", reference="locked.pdf", source_url="https://example.invalid",
                version="1", content="", updated_at=None, metadata={"path": "locked.pdf"},
                mime_type="application/pdf", content_bytes=b"%PDF-1.7\n/Encrypt true",
            ),
            "PASSWORD_PROTECTED",
        ),
    ],
)
def test_unsafe_content_is_quarantined(document: SourceDocument, reason: str) -> None:
    with pytest.raises(QuarantinedDocument) as failure:
        ContentSecurityScanner(Settings(_env_file=None)).inspect(document)
    assert failure.value.reason.code == reason


def test_local_credential_sensitive_source_is_authorized_for_project_ingestion() -> None:
    document = replace(
        source("api_key = 'abcdefghijklmnopqrstuvwxyz123456'", "local.properties"),
        provider="LOCAL",
    )

    assert ContentSecurityScanner(Settings(_env_file=None)).inspect(document) is True


def test_spoofed_repository_local_image_is_quarantined_before_visual_processing() -> None:
    document = replace(
        source("![diagram](diagram.png)", "README.md", "text/markdown"),
        local_visuals=(SourceVisualInput("diagram.png", "image/png", b"MZ executable"),),
    )
    with pytest.raises(QuarantinedDocument) as failure:
        ContentSecurityScanner(Settings(_env_file=None)).inspect(document)
    assert failure.value.reason.code == "MIME_MISMATCH"
