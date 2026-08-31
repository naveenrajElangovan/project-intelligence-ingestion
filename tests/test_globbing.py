"""fnmatch has no path semantics, and every exclusion list in this service was
written as if it did.

The failures were silent in the worst direction: a pattern that reads as though
it excludes something and does not. These are the three cases that mattered in
production.
"""

import pytest

from app.globbing import matches, matches_any


@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        # The reason this module exists: fnmatch returns False for all three.
        ("project-documentation/POS-RAG-00.md", "**/project-documentation/**"),
        ("notes.docx", "**/*.docx"),
        ("project-documentation", "**/project-documentation/**"),
        ("docs/project-documentation/a.md", "**/project-documentation/**"),
        ("docs/deep/notes.docx", "**/*.docx"),
        ("src/main/Cart.kt", "src/**/*.kt"),
        ("src/Cart.kt", "src/**/*.kt"),
        ("build/x/y.kt", "build/**"),
        ("build", "build/**"),
        ("Cart.kt", "*.kt"),
        ("a/b/c.md", "a/?/c.md"),
    ],
)
def test_patterns_that_should_match(path, pattern):
    assert matches(path, pattern) is True


@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        # fnmatch returns True here, because its * crosses separators: a pattern
        # meant for one directory quietly matched the whole tree.
        ("src/main/Cart.kt", "*.kt"),
        ("src/main/Cart.kt", "**/project-documentation/**"),
        ("notes.md", "**/*.docx"),
        ("rebuild/x.kt", "build/**"),
        ("a/bb/c.md", "a/?/c.md"),
    ],
)
def test_patterns_that_should_not_match(path, pattern):
    assert matches(path, pattern) is False


def test_a_leading_slash_is_irrelevant():
    assert matches("/src/Cart.kt", "src/*.kt") is True


def test_no_patterns_matches_nothing():
    assert matches_any("src/Cart.kt", ()) is False


def test_any_is_a_disjunction():
    assert matches_any("notes.docx", ("**/*.md", "**/*.docx")) is True
