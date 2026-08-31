"""Decide how a document should be chunked from its content, not just its name.

Routing used to be by file suffix and MIME type alone. That works for a repo
file and for a Confluence page, and fails for everything else: an attachment
named `notes.txt` holding Markdown, a `.dat` file holding CSV, a code file with
an unregistered extension, an ADF export, a log with no extension. All of those
landed on the generic section path, which windows blind character offsets and
produces the worst chunks in the corpus.

Sniffing is deliberately conservative. A wrong answer here is worse than no
answer, so each detector needs positive structural evidence -- a fenced heading,
a balanced delimiter count across several lines, a parseable document -- and the
result carries the evidence that justified it so a bad decision can be explained
rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re


# Enough lines to see structure, few enough to stay cheap on a large attachment.
SAMPLE_LINES = 200
MINIMUM_TABLE_ROWS = 3


@dataclass(frozen=True, slots=True)
class FormatDecision:
    """A routing decision plus the reason it was made."""

    kind: str
    reason: str

    @property
    def is_confident(self) -> bool:
        return self.kind != "prose"


@dataclass(frozen=True, slots=True)
class CategoryDecision:
    category: str
    reason: str
    warning: str = ""


@dataclass(frozen=True, slots=True)
class EntityDecision:
    entity: str
    reason: str


_MARKDOWN_HEADING = re.compile(r"^ {0,3}#{1,6} \S", re.MULTILINE)
_MARKDOWN_FENCE = re.compile(r"^ {0,3}```", re.MULTILINE)
_MARKDOWN_TABLE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_HTML_TAG = re.compile(r"<(p|div|table|h[1-6]|ul|ol|span|section)\b[^>]*>", re.IGNORECASE)
_LOG_LINE = re.compile(
    r"^\s*(?:\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\d{2}:\d{2}:\d{2}|"
    r"(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b)"
)
_CODE_SIGNAL = re.compile(
    r"^\s*(?:package |import |from \S+ import |#include|using |def |class |func |"
    r"public |private |internal |fun |val |var |const |let |async def )"
)


def detect_format(content: str, *, declared: str | None = None) -> FormatDecision:
    """Classify a text document's shape.

    `declared` is the caller's existing suffix/MIME conclusion. It wins whenever
    it is present, because a repo file's extension is authoritative in a way that
    a heuristic never is; sniffing only fills the gap where there is no
    declaration at all.
    """

    if declared:
        return FormatDecision(declared, "declared by extension or MIME type")

    stripped = content.strip()
    if not stripped:
        return FormatDecision("prose", "empty content")

    lines = stripped.splitlines()
    sample = lines[:SAMPLE_LINES]

    if _looks_like_json(stripped):
        return FormatDecision("json", "parses as a JSON document")

    delimiter, rows = _delimited_shape(sample)
    if delimiter:
        return FormatDecision(
            "csv",
            f"{rows} consecutive lines share {delimiter!r} field counts",
        )

    if _MARKDOWN_TABLE.search("\n".join(sample)):
        pipe_rows = sum(1 for line in sample if line.count("|") >= 2)
        if pipe_rows >= MINIMUM_TABLE_ROWS:
            return FormatDecision("markdown", f"{pipe_rows} pipe-delimited table rows")

    if _MARKDOWN_HEADING.search(stripped) or _MARKDOWN_FENCE.search(stripped):
        return FormatDecision("markdown", "ATX headings or fenced blocks present")

    if _HTML_TAG.search(stripped):
        return FormatDecision("html", "block-level HTML tags present")

    log_lines = sum(1 for line in sample if _LOG_LINE.match(line))
    if log_lines >= max(MINIMUM_TABLE_ROWS, len(sample) // 2):
        return FormatDecision("log", f"{log_lines} timestamped or levelled lines")

    code_lines = sum(1 for line in sample if _CODE_SIGNAL.match(line))
    if code_lines >= MINIMUM_TABLE_ROWS:
        return FormatDecision("code", f"{code_lines} declaration or import lines")

    return FormatDecision("prose", "no structural markers found")


def detect_category(
    title: str, content: str, *, labels: tuple[str, ...] = ()
) -> CategoryDecision:
    """Classify document semantics and always retain the decision evidence."""

    normalized_labels = {label.strip().casefold() for label in labels if label.strip()}
    labelled = next(
        (label.split(":", 1)[1] for label in normalized_labels if label.startswith("pi-category:")),
        "",
    )
    structural = _infer_category(title, content)
    if labelled:
        warning = (
            f"label={labelled} disagrees with structure={structural}"
            if structural != "narrative" and structural != labelled
            else ""
        )
        return CategoryDecision(labelled, "Confluence pi-category label", warning)
    if structural != "narrative":
        return CategoryDecision(structural, "title and structural markers")
    return CategoryDecision("narrative", "no category-specific markers")


def detect_entity(
    title: str,
    content: str,
    *,
    labels: tuple[str, ...] = (),
    structure_path: tuple[str, ...] = (),
) -> EntityDecision:
    """Resolve a project entity from explicit-to-structural evidence only."""

    normalized_labels = {label.strip().casefold() for label in labels if label.strip()}
    labelled = next(
        (
            _normalize_entity(label.split(":", 1)[1])
            for label in normalized_labels
            if label.startswith("pi-entity:")
        ),
        "",
    )
    if labelled:
        return EntityDecision(labelled, "Confluence pi-entity label")

    title_match = re.match(
        r"^\s*([A-Za-z][A-Za-z0-9]{1,30})(?:[-_:]+|\s+RAG[-_ ]*\d+\b)",
        title,
        flags=re.IGNORECASE,
    )
    if title_match:
        candidate = _normalize_entity(title_match.group(1))
        if candidate not in _ENTITY_STOP_WORDS:
            return EntityDecision(candidate, "document title prefix")

    structural = "\n".join((*structure_path, content[:8_000]))
    structural_match = re.search(r"\b([A-Z][A-Z0-9]{1,30})_[A-Z0-9_]{2,}\b", structural)
    if structural_match:
        candidate = _normalize_entity(structural_match.group(1))
        if candidate not in _ENTITY_STOP_WORDS:
            return EntityDecision(candidate, "structural identifier prefix")
    return EntityDecision("", "no high-confidence entity marker")


_ENTITY_STOP_WORDS = {
    "about", "api", "credit", "event", "final", "funds", "http", "items",
    "json", "local", "login", "master", "not", "project", "sql", "example",
    "workflow",
}


def _normalize_entity(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", value.strip().casefold())


def _infer_category(title: str, content: str) -> str:
    name = title.casefold().replace("_", " ")
    sample = content[:80_000]
    if "bot-rag-00" in name:
        return "index"
    if "bot-rag-02" in name:
        return "workflow"
    if any(prefix in name for prefix in ("bot-rag-01", "bot-rag-03", "bot-rag-04")):
        return "narrative"
    if re.search(r"\b(master index|corpus guide|navigation index)\b", name):
        return "index"
    if re.search(r"\b(glossary|terms and definitions)\b", name):
        return "glossary"
    if re.search(r"\b(workflow|business flow|end to end)\b", name) or re.search(
        r"\[(?:BOT)?FLOW-\d+\]", sample, re.IGNORECASE
    ):
        return "workflow"
    entity_sections = re.findall(
        r"(?m)^#{2,4}\s+`?[A-Z][A-Z0-9_]+`?\s+[-—].*\bid\s+\d+.*\bversion\b",
        sample,
        re.IGNORECASE,
    )
    if entity_sections or "event and integration contract" in name:
        return "entity-contract"
    table_rows = sum(1 for line in sample.splitlines() if line.count("|") >= 3)
    prose_lines = sum(1 for line in sample.splitlines() if len(line.split()) >= 8)
    if table_rows >= 20 and table_rows > prose_lines:
        return "registry-table"
    return "narrative"


def _looks_like_json(value: str) -> bool:
    if value[0] not in "{[" or value[-1] not in "}]":
        return False
    try:
        json.loads(value)
    except (ValueError, RecursionError):
        return False
    return True


def _delimited_shape(sample: list[str]) -> tuple[str | None, int]:
    """Detect a delimited table by a stable field count, not by one line.

    A single comma-heavy sentence is not a CSV. Several consecutive lines
    agreeing on the same field count is, and that is also what makes the header
    meaningful enough to repeat into every chunk.
    """

    for delimiter in (",", "\\t", ";"):
        actual = "\t" if delimiter == "\\t" else delimiter
        counts = [line.count(actual) for line in sample if line.strip()]
        if len(counts) < MINIMUM_TABLE_ROWS:
            continue
        first = counts[0]
        if first < 1:
            continue
        agreeing = 1
        for count in counts[1:]:
            if count != first:
                break
            agreeing += 1
        if agreeing >= MINIMUM_TABLE_ROWS:
            return delimiter, agreeing
    return None, 0
