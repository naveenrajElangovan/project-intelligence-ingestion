"""A Confluence live doc is authored in ADF and may carry no storage body.

Before this, an absent storage body produced an empty document that was indexed
as a source with zero chunks and a committed manifest -- counted as success, so
nobody looked again.
"""

import json

import pytest

from app.atlassian import _atlas_doc_text


def _document(*, as_string: bool) -> dict:
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "[EVT-025] Complete event table"}],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "All 48 entries follow."}]},
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "id"}]}]},
                            {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "wire name"}]}]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "116"}]}]},
                            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "POS_CLOSE_SHIFT_REQUEST"}]}]},
                        ],
                    },
                ],
            },
        ],
    }
    value = json.dumps(adf) if as_string else adf
    return {"atlas_doc_format": {"value": value}}


@pytest.mark.parametrize("as_string", [True, False])
def test_a_live_doc_body_becomes_markdown(as_string):
    text = _atlas_doc_text(_document(as_string=as_string))

    assert text.splitlines() == [
        "## [EVT-025] Complete event table",
        "All 48 entries follow.",
        "id | wire name",
        "116 | POS_CLOSE_SHIFT_REQUEST",
    ]


def test_headings_survive_as_markdown_so_structure_path_is_populated():
    # The heading level matters: it is what the Markdown splitter turns into the
    # heading trail a passage is attributed by.
    text = _atlas_doc_text(_document(as_string=False))

    assert text.startswith("## ")


def test_table_rows_are_delimited_not_space_joined():
    text = _atlas_doc_text(_document(as_string=False))

    assert "116 | POS_CLOSE_SHIFT_REQUEST" in text


def test_a_missing_atlas_doc_body_yields_nothing():
    assert _atlas_doc_text({}) == ""
    assert _atlas_doc_text({"atlas_doc_format": {}}) == ""


def test_unparseable_json_yields_nothing_rather_than_raising():
    # A malformed body must not abort the whole provider run; the empty result
    # is then caught by the chunker's empty-body guard and counted as failed.
    assert _atlas_doc_text({"atlas_doc_format": {"value": "{not json"}}) == ""


def test_the_page_listing_requests_a_single_body_format():
    """`body-format` takes one value.

    A comma-separated list was rejected by Atlassian with 400, which the
    control-plane proxy reported as a 502 with no upstream detail -- so a
    one-character change in the query string looked like an outage. Live docs are
    handled by a second per-page read instead.
    """

    import re
    from pathlib import Path

    source = Path("app/atlassian.py").read_text(encoding="utf-8")
    listing = re.search(r'"space-id": mapping\.space_id,.*?\}', source, re.DOTALL)
    assert listing, "page listing parameters not found"
    formats = re.findall(r'"body-format": "([^"]+)"', listing.group(0))
    assert formats == ["storage"], formats


def test_the_adf_fallback_is_a_separate_single_page_read():
    from pathlib import Path

    source = Path("app/atlassian.py").read_text(encoding="utf-8")
    assert "_confluence_live_body" in source
    assert '{"body-format": "atlas_doc_format"}' in source
