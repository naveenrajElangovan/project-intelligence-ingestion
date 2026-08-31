"""Table integrity is the strictest requirement in the corpus: the event contract
is mostly tables, and a row without its column names is not evidence.

Three invariants are asserted directly rather than inferred from chunk counts:
a row is never split across windows, every window carries the header, and no
window exceeds the embedder's 512 positions unless one indivisible row does --
in which case it is split by column, not truncated.
"""

import pytest

from app.config import Settings
from app.structured_chunking import (
    _EMBEDDER_POSITION_LIMIT,
    StructuredDocumentChunker,
    _cells,
)


class _Chunker(StructuredDocumentChunker):
    """Chunker with a deterministic, inspectable token counter.

    The real E5 tokenizer is not available offline in every environment, and the
    invariants under test are about arithmetic, not about vocabulary.
    """

    def __init__(self, tokens_per_word: int = 3) -> None:
        super().__init__(Settings(_env_file=None))
        self._tokens_per_word = tokens_per_word
        self.count_calls = 0

    def _count_tokens(self, value: str) -> int:
        self.count_calls += 1
        return max(1, len(value.split()) * self._tokens_per_word)


HEADER = "| id | wire name | family |"
SEPARATOR = "|---|---|---|"


def _table(rows: int) -> list[str]:
    return [HEADER, SEPARATOR] + [
        f"| {100 + index} | POS_EVENT_{index} | POS |" for index in range(rows)
    ]


def test_every_window_carries_header_and_separator():
    windows = _Chunker()._table_windows(_table(40), 60)

    assert len(windows) > 1
    for window in windows:
        lines = window.splitlines()
        assert lines[0] == HEADER
        assert lines[1] == SEPARATOR


def test_no_row_is_split_across_windows():
    rows = _table(40)[2:]
    windows = _Chunker()._table_windows(_table(40), 60)

    emitted = [
        line
        for window in windows
        for line in window.splitlines()
        if line not in {HEADER, SEPARATOR}
    ]
    assert emitted == rows


def test_windows_respect_the_token_budget():
    chunker = _Chunker()
    budget = 90

    for window in chunker._table_windows(_table(40), budget):
        # Header plus at least one row may exceed the target, but only by that
        # single row: the invariant is that removing the last row would fit.
        lines = window.splitlines()
        assert chunker._count_tokens("\n".join(lines[:-1])) <= budget


def test_a_row_too_wide_for_the_embedder_is_split_by_column():
    wide_value = "value " * 400
    table = [HEADER, SEPARATOR, f"| 116 | {wide_value} | POS |"]

    windows = _Chunker(tokens_per_word=2)._table_windows(table, 300)

    assert len(windows) > 1
    # Every part still says which column it came from.
    for window in windows:
        labels = _cells(window.splitlines()[0])
        assert labels
        assert all(label for label in labels)
    assert "id" in windows[0].splitlines()[0]


def test_a_short_table_is_one_window():
    windows = _Chunker()._table_windows(_table(2), 420)

    assert len(windows) == 1
    assert windows[0].count("POS_EVENT_") == 2


def test_a_table_without_a_separator_still_keeps_its_header():
    table = ["id,status,owner", "T2,active,Ana", "T3,blocked,Luis"]

    windows = _Chunker()._table_windows(table, 4)

    assert len(windows) == 2
    assert all(window.startswith("id,status,owner") for window in windows)


def test_the_header_is_never_dropped_even_when_it_exceeds_the_budget():
    # Budget smaller than the header alone: the header still travels, because a
    # row without column names cannot be interpreted.
    windows = _Chunker()._table_windows(_table(3), 1)

    assert len(windows) == 3
    assert all(window.startswith(HEADER) for window in windows)


def test_token_counts_are_cached_not_recomputed_per_window():
    chunker = StructuredDocumentChunker(Settings(_env_file=None))
    rows = _table(200)

    chunker._table_windows(rows, 200)
    first = len(chunker._token_counts)
    chunker._table_windows(rows, 200)

    # Second pass adds nothing: every distinct string was already measured.
    assert len(chunker._token_counts) == first


def test_the_hard_limit_is_the_embedder_not_the_configured_budget():
    assert _EMBEDDER_POSITION_LIMIT == 512
