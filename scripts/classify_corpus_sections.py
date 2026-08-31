"""Classify every SECTION, not every document.

Running the document-level classifier surfaced two problems it cannot fix:

  * BOT-RAG-05 is "Operations, Configuration, Testing, Deployment and Glossary" --
    one glossary section inside an otherwise narrative document. A document-level
    label is wrong for most of it either way.
  * `[EVT-001] How to use this volume` is a navigational section INSIDE the
    entity-contract document. It is the chunk that took a reranked slot ahead of
    the payload definition in the reachability trace. No document-level label can
    demote it, because its document is the most important one in the corpus.

So the unit of categorisation has to be the section -- which is also the unit
Perplexity calls a self-contained span. The document category becomes the default
a section inherits when its own shape says nothing.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

ENTITY_HEADING = re.compile(r"^#{3}\s+`([A-Z][A-Z0-9_]*)`\s+[—-]\s+id\s+\d+,\s+version\s+[\d.]+\s*$")
HEADING = re.compile(r"^(#{2,4})\s+(.+?)\s*$")
ENTITY_HEADING_B = re.compile(
    r"^#{2,4}\s+[\d.]+\s+`(?P<key>[A-Z][A-Z0-9_]*)`\s+\(id\s+(?P<id>\d+)\)"
)


def entity_heading(line: str):
    return ENTITY_HEADING.match(line) or ENTITY_HEADING_B.match(line)

TABLE_ROW = re.compile(r"^\s*\|")
STEP_LINE = re.compile(r"^\s*\d+\.\s+\S")
NAV_TITLE = re.compile(
    r"\b(?:how to use|reading order|volume map|master index|corpus guide|"
    r"what this volume|document metadata|scope and audience|index)\b", re.IGNORECASE)
GLOSSARY_TITLE = re.compile(r"\b(?:glossar\w*|vocabular\w*|terminolog\w*|definitions?)\b", re.IGNORECASE)
REGISTRY_TITLE = re.compile(r"\b(?:registry|matrix|inventory|catalog\w*|map|ownership|reference table)\b", re.IGNORECASE)
WORKFLOW_TITLE = re.compile(r"\b(?:flow|lifecycle|workflow|process|walkthrough|end.to.end)\b", re.IGNORECASE)

TABLE_DOMINANT = 0.28   # lowered from 0.35: PLAT-RAG-00 sits at 0.318 and is
                        # unmistakably a registry document. BOT-RAG-03, the
                        # nearest narrative, is 0.216 -- a comfortable gap.
STEP_DOMINANT = 0.08


def classify_section(title: str, body: list[str], doc_default: str, *, raw_heading: str = "") -> tuple[str, str]:
    live = [line for line in body if line.strip()]
    total = max(len(live), 1)
    tables = sum(bool(TABLE_ROW.match(line)) for line in body)
    steps = sum(bool(STEP_LINE.match(line)) for line in body)

    if entity_heading(raw_heading):
        return "entity-contract", "own heading is an entity contract"
    if REGISTRY_TITLE.search(title):
        return "registry-table", "registry title"
    if NAV_TITLE.search(title):
        return "index", "navigational title"
    if GLOSSARY_TITLE.search(title):
        return "glossary", "glossary title"
    if tables / total >= TABLE_DOMINANT:
        return "registry-table", f"{tables}/{total} table rows"
    if steps / total >= STEP_DOMINANT or (WORKFLOW_TITLE.search(title) and steps >= 3):
        return "workflow", f"{steps} numbered steps"
    return doc_default, "inherited from document"


def sections(path: Path):
    lines = path.read_text(errors="ignore").splitlines()
    marks = [(i, m.group(2)) for i, line in enumerate(lines) if (m := HEADING.match(line))]
    for position, (start, title) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        yield title, lines[start + 1 : end], lines[start]


def document_default(path: Path) -> str:
    lines = path.read_text(errors="ignore").splitlines()
    live = [line for line in lines if line.strip()]
    tables = sum(bool(TABLE_ROW.match(line)) for line in lines)
    if tables / max(len(live), 1) >= TABLE_DOMINANT:
        return "registry-table"
    return "narrative"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--show", action="append", default=[])
    arguments = parser.parse_args()

    totals: Counter[str] = Counter()
    print(f"{'document':44} {'sections':>8}  category breakdown")
    for path in sorted(arguments.corpus.glob("*.md")):
        default = document_default(path)
        local: Counter[str] = Counter()
        for title, body, raw in sections(path):
            category, _why = classify_section(title, body, default, raw_heading=raw)
            local[category] += 1
            totals[category] += 1
        summary = " ".join(f"{k}:{v}" for k, v in local.most_common())
        print(f"{path.name[:43]:44} {sum(local.values()):>8}  {summary}")

    print(f"\n{'CORPUS TOTAL':44} {sum(totals.values()):>8}  " +
          " ".join(f"{k}:{v}" for k, v in totals.most_common()))

    for needle in arguments.show:
        print(f"\n--- sections matching {needle!r} ---")
        for path in sorted(arguments.corpus.glob("*.md")):
            default = document_default(path)
            for title, body, raw in sections(path):
                if needle.lower() in title.lower():
                    category, why = classify_section(title, body, default, raw_heading=raw)
                    print(f"  {category:16} {why:28} {title[:60]}  ({path.name[:28]})")


if __name__ == "__main__":
    main()
