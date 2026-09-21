"""Routing by file name alone sent every unlabelled document to the generic
section path, which windows blind character offsets and loses headings and
tables. Sniffing fills that gap -- but a wrong sniff is worse than none, so each
detector requires positive structural evidence."""

import pytest

from app.format_analysis import detect_format


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("# Title\n\nBody text follows.", "markdown"),
        ("Intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |", "markdown"),
        ("```\ncode\n```", "markdown"),
        ('{"event": "POS_CLOSE_SHIFT", "id": 116}', "json"),
        ("[1, 2, 3]", "json"),
        ("id,status,owner\nT2,active,Ana\nT3,blocked,Luis", "csv"),
        ("a\tb\tc\n1\t2\t3\n4\t5\t6", "csv"),
        ("<p>Hello</p><table><tr><td>x</td></tr></table>", "html"),
        (
            "2026-08-21 02:00:01 INFO started\n2026-08-21 02:00:02 INFO ready\n"
            "ERROR failed\nWARN retrying",
            "log",
        ),
        ("package com.x\nimport y.z\nclass A {\nfun b() {}\n}", "code"),
    ],
)
def test_structure_is_recognised(content, expected):
    assert detect_format(content).kind == expected


@pytest.mark.parametrize(
    "content",
    [
        "This is ordinary prose, with commas, semicolons; and full stops.",
        "One line only.",
        "   ",
        "",
    ],
)
def test_prose_is_not_forced_into_a_structure(content):
    assert detect_format(content).kind == "prose"


def test_a_declared_type_always_wins():
    # A repo file's extension is authoritative in a way a heuristic never is.
    assert detect_format("plain words", declared="markdown").kind == "markdown"
    assert detect_format("# Heading", declared="csv").kind == "csv"


def test_one_comma_heavy_sentence_is_not_a_csv():
    # A single line agreeing with itself is not evidence of a delimited file.
    assert detect_format("Ana, Luis, and Marta attended, briefly.").kind == "prose"


def test_broken_json_is_not_reported_as_json():
    assert detect_format('{"a": 1,').kind != "json"


def test_every_decision_explains_itself():
    # The reason is what makes a wrong routing decision diagnosable instead of
    # mysterious.
    for content in ("# H", "a,b,c\n1,2,3\n4,5,6", "<p>x</p>", "words"):
        assert detect_format(content).reason
