"""Tables were the worst-served content in the corpus, and the event contract is
mostly tables.

Two defects, both invisible unless you read the chunks: a Confluence table was
flattened into a single space-joined run and then repeated cell by cell, and an
oversized Markdown section was split mid-table so every window after the first
lost its header row.
"""

from app.config import Settings
from app.structured_chunking import (
    StructuredDocumentChunker,
    _html_table_text,
)


def _table(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").find("table")


def test_a_table_keeps_its_cell_boundaries():
    rendered = _html_table_text(
        _table(
            "<table><tbody>"
            "<tr><th><p>id</p></th><th><p>wire name</p></th></tr>"
            "<tr><td><p>116</p></td><td><p>POS_CLOSE_SHIFT_REQUEST</p></td></tr>"
            "</tbody></table>"
        )
    )

    assert rendered.splitlines() == [
        "| id | wire name |",
        "| --- | --- |",
        "| 116 | POS_CLOSE_SHIFT_REQUEST |",
    ]


def test_a_cell_is_rendered_once_not_once_per_paragraph():
    # Confluence wraps every cell in <p>, and get_text on the table plus find_all
    # on the paragraphs emitted the same values twice.
    rendered = _html_table_text(
        _table("<table><tr><td><p>alpha</p><p>beta</p></td><td><p>gamma</p></td></tr></table>")
    )

    assert rendered.count("alpha") == 1
    assert rendered == "| alpha beta | gamma |\n| --- | --- |"


def test_a_table_with_no_rows_falls_back_to_its_text():
    rendered = _html_table_text(_table("<table><caption>Event ids</caption></table>"))

    assert rendered == "Event ids"


def _windows(lines: list[str], budget: int) -> list[str]:
    # Exercises the live packer rather than the helper it replaced.
    return StructuredDocumentChunker(Settings(_env_file=None))._table_windows(lines, budget)


def test_every_table_window_repeats_the_header():
    table = ["| id | wire name | family |", "|---|---|---|"] + [
        f"| {100 + index} | POS_EVENT_{index} | POS |" for index in range(40)
    ]

    windows = _windows(table, 60)

    assert len(windows) > 1
    for window in windows:
        assert window.startswith("| id | wire name | family |")
        # The separator has to travel with the header or the repeat stops being a
        # table to anything that parses one.
        assert window.splitlines()[1] == "|---|---|---|"


def test_a_short_table_is_not_split_at_all():
    table = ["| id | wire |", "|---|---|", "| 116 | POS_CLOSE_SHIFT_REQUEST |"]

    windows = _windows(table, 400)

    assert len(windows) == 1
