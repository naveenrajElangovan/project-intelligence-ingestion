from app.config import Settings
from app.models import SourceDocument
from app.structured_chunking import StructuredDocumentChunker


def _document(content: str, *, mime_type: str = "text/markdown") -> SourceDocument:
    return SourceDocument(
        project_id="DEMO",
        provider="ANY_FUTURE_PROVIDER",
        source_id="contract:events",
        source_type="ANY_CONTRACT_TYPE",
        title="Event and Integration Contract",
        reference="events",
        source_url="https://example.invalid/events",
        version="1",
        content=content,
        updated_at=None,
        metadata={},
        mime_type=mime_type,
    )


def test_entity_section_is_one_chunk_with_identity_metadata() -> None:
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        _document(
            "### `POS_LOGIN` — id 101, version 0.3\n"
            "Purpose: POS login activity.\n\n"
            "| Field | Type |\n|---|---|\n| user | EventUser |\n| status | String |"
        )
    )

    assert len(chunks) == 1
    assert chunks[0].structure_path == ("POS_LOGIN",)
    assert chunks[0].metadata["entity_key"] == "POS_LOGIN"
    assert chunks[0].metadata["entity_id"] == "101"
    assert chunks[0].metadata["entity_version"] == "0.3"
    assert chunks[0].content.startswith("### `POS_LOGIN`")


def test_oversized_field_table_repeats_heading_and_header() -> None:
    rows = "\n".join(f"| field_{index} | String with detailed value {index} |" for index in range(30))
    chunks = StructuredDocumentChunker(
        Settings(_env_file=None, chunk_max_tokens=70, chunk_overlap_tokens=0)
    ).split(
        _document(
            "### `POS_LOGIN` — id 101, version 0.3\n"
            "| Field | Type |\n|---|---|\n" + rows
        )
    )

    assert len(chunks) > 1
    assert all(chunk.content.startswith("### `POS_LOGIN` — id 101, version 0.3\n| Field | Type |") for chunk in chunks)
    assert all(chunk.metadata["entity_key"] == "POS_LOGIN" for chunk in chunks)


def test_entity_profile_accepts_confluence_html_headings() -> None:
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        _document(
            "<h3><code>POS_LOGIN</code> — id 101, version 0.3</h3>"
            "<p>POS login activity.</p>",
            mime_type="text/html",
        )
    )

    assert chunks[0].metadata["entity_key"] == "POS_LOGIN"


def test_numbered_parenthesized_entity_heading_has_identity_without_version() -> None:
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        _document(
            "### 4.3 `POS_CLOSE_SHIFT` (id 104) — POS shift closed with full summary\n"
            "Purpose: publish the completed shift summary."
        )
    )

    assert len(chunks) == 1
    assert chunks[0].structure_path == ("POS_CLOSE_SHIFT",)
    assert chunks[0].metadata["entity_key"] == "POS_CLOSE_SHIFT"
    assert chunks[0].metadata["entity_id"] == "104"
    assert "entity_version" not in chunks[0].metadata


def test_multi_entity_contract_writes_application_scope_metadata() -> None:
    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        _document(
            "### `POS_LOGIN` — id 101, version 0.3\nPOS activity.\n\n"
            "### `BOT_TRANSFER` — id 201, version 0.2\nBOT activity."
        )
    )

    assert {chunk.metadata["application"] for chunk in chunks} == {"pos", "bot"}
    assert all(chunk.metadata["shared_across_entities"] is True for chunk in chunks)
    assert {chunk.metadata["entity_key"] for chunk in chunks} == {
        "POS_LOGIN",
        "BOT_TRANSFER",
    }


def test_both_heading_conventions_detect_all_57_contract_sections() -> None:
    conventional = "\n\n".join(
        f"### `APP_EVENT_{index}` — id {100 + index}, version 1.0\nPurpose {index}."
        for index in range(27)
    )
    parenthesized = "\n\n".join(
        f"### {index + 1}.1 `APP_COMMAND_{index}` (id {200 + index}) — Command {index}\n"
        f"Purpose {index}."
        for index in range(30)
    )

    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(
        _document(f"{conventional}\n\n{parenthesized}")
    )

    assert len(chunks) == 57
    assert sum("entity_version" in chunk.metadata for chunk in chunks) == 27
