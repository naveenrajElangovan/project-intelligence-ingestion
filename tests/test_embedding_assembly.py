"""The embedded passage used to be the first `chunk_max_tokens` window of
enrichment-plus-body, with the remainder discarded. Whenever the enrichment
header pushed the total over the budget, the end of the passage was dropped from
the vector while chunk_text kept it -- searchable text that could not be found.
"""

from datetime import UTC, datetime

from app.config import Settings
from app.embedding import _ensure_passage_prefix
from app.models import SourceDocument
from app.structured_chunking import (
    _EMBEDDER_POSITION_LIMIT,
    StructuredDocumentChunker,
)


class _Chunker(StructuredDocumentChunker):
    def __init__(self) -> None:
        super().__init__(Settings(_env_file=None))

    def _count_tokens(self, value: str) -> int:
        return len(value.split())


def test_passage_prefix_is_canonical_on_every_input_route():
    for value in (
        "body",
        "passage: body",
        "passage: passage: body",
        "  PASSAGE:   passage: body",
    ):
        result = _ensure_passage_prefix(value)
        assert result == "passage: body"
        assert result.count("passage:") == 1


def test_sentence_transformer_windows_reserve_special_tokens() -> None:
    chunker = StructuredDocumentChunker(Settings(_env_file=None))
    splitter = chunker._token_splitter()
    text = " ".join(f"POS_CLOSE_SHIFT_REQUEST_EVENT_{index}" for index in range(900))

    windows = splitter.split_text(text)

    assert splitter.tokens_per_chunk == 510
    assert windows
    assert all(splitter.count_tokens(text=window) <= 512 for window in windows)


def test_the_body_survives_when_enrichment_would_not_fit():
    chunker = _Chunker()
    body = " ".join(f"word{index}" for index in range(500))

    assembled = chunker._fit_embedding_text(
        required="passage: Doc > Section",
        optional=("SOURCE TYPE: PAGE", "REFERENCE: 1", "LOCATION: source", "META: lots " * 40),
        body=body,
    )

    assert assembled.startswith("passage: Doc > Section")
    assert assembled.rstrip().endswith("word499")
    assert chunker._count_tokens(assembled) <= _EMBEDDER_POSITION_LIMIT


def test_enrichment_is_kept_when_there_is_room():
    assembled = _Chunker()._fit_embedding_text(
        required="passage: Doc",
        optional=("SOURCE TYPE: PAGE", "REFERENCE: 1"),
        body="a short body",
    )

    assert "SOURCE TYPE: PAGE" in assembled
    assert "REFERENCE: 1" in assembled


def test_the_least_specific_enrichment_is_dropped_first():
    chunker = _Chunker()
    # Sized so exactly one enrichment line has to go: 505 body words plus the
    # prefix already exceed the 512 positions with all four lines attached.
    body = " ".join(f"w{index}" for index in range(505))

    assembled = chunker._fit_embedding_text(
        required="passage: Doc",
        optional=("SOURCE TYPE: PAGE", "REFERENCE: 1", "LOCATION: source", "META: x y z"),
        body=body,
    )

    # SOURCE TYPE is what a filtered query matches on, so it outlives META.
    assert "META: x y z" not in assembled
    assert assembled.rstrip().endswith("w504")


def test_a_body_larger_than_the_model_is_cut_only_as_a_last_resort():
    chunker = _Chunker()
    body = " ".join(f"w{index}" for index in range(2000))

    assembled = chunker._fit_embedding_text(
        required="passage: Doc", optional=("SOURCE TYPE: PAGE",), body=body
    )

    assert assembled.startswith("passage: Doc")
    assert chunker._count_tokens(assembled) <= _EMBEDDER_POSITION_LIMIT


def test_no_chunk_of_a_real_table_document_loses_its_tail():
    content = "\n".join(
        ["# Contract", "", "## [EVT-025] Events", "", "| id | wire | family |", "|---|---|---|"]
        + [f"| {100 + index} | POS_EVENT_{index} | POS |" for index in range(120)]
    )
    document = SourceDocument(
        project_id="DEMO",
        provider="CONFLUENCE",
        source_id="page:1",
        source_type="PAGE",
        title="Contract",
        reference="1",
        source_url="https://example.invalid",
        version="1",
        content=content,
        updated_at=datetime.now(UTC),
        metadata={"path": "contract.md"},
        mime_type="text/markdown",
    )
    chunker = StructuredDocumentChunker(Settings(_env_file=None))

    chunks = chunker.split(document)

    assert chunks
    for chunk in chunks:
        assert chunk.embedding_text.count("passage:") == 1
        assert chunk.content.split()[-1] == chunk.embedding_text.split()[-1]
        assert chunker._count_tokens(chunk.embedding_text) <= _EMBEDDER_POSITION_LIMIT
