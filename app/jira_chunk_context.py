"""Deterministic Jira provenance and lossless enrichment boundaries."""

import hashlib
import json
import unicodedata

from app.models import LogicalElement, SourceDocument

ENRICHMENT_FIELDS = (
    "repository",
    "branch",
    "path",
    "file_name",
    "issue_key",
    "symbols",
    "important_kwd",
)


def effective_chunker_version(base: str, provider: str) -> str:
    return base + (".jira-context-v2" if provider == "JIRA" else "")


def enrichment_values(metadata: dict[str, object]) -> list[str]:
    values: list[str] = []
    for key in ENRICHMENT_FIELDS:
        value = metadata.get(key)
        for item in value if isinstance(value, (list, tuple)) else [value]:
            if isinstance(item, str) and item:
                values.append(item)
    return values


def attachment_locator(document: SourceDocument, element: LogicalElement, ordinal: int) -> str:
    if not document.project_id or not document.source_id or not document.version:
        raise ValueError("Jira attachment requires project, source, and version identity")
    if ordinal < 0:
        raise ValueError("Jira attachment window ordinal cannot be negative")
    revision = hashlib.sha256(
        json.dumps(
            [document.project_id, document.source_id, document.version],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:16]
    heading = [
        unicodedata.normalize("NFC", " ".join(value.split()))
        for value in element.heading_path
        if value.strip()
    ]
    section = hashlib.sha256(
        json.dumps(heading, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    provenance = element.locator or f"section:{section}"
    return f"{provenance}:v:{revision}:window:{ordinal + 1}"


def metadata_context(metadata: dict[str, object]) -> str:
    """Length-prefixed raw values keep Unicode and punctuation outside framing.

    Each field is followed by a UTF-8 byte count, newline, exactly that many
    bytes, and a newline. A decoder never infers boundaries from content.
    The final newline also keeps citation terminators out of adjacent values.
    """
    records: list[str] = []
    total = 0
    for key in ENRICHMENT_FIELDS:
        value = metadata.get(key)
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if item is None or item == "":
                continue
            if not isinstance(item, str):
                raise ValueError("Jira enrichment values must be strings")
            raw = item
            if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in raw):
                raise ValueError("Jira enrichment contains unsupported control characters")
            size = len(raw.encode("utf-8"))
            record = f"{key} {size}\n{raw}\n"
            total += len(record.encode("utf-8"))
            if size > 65536 or total > 262144 or len(records) >= 2048:
                raise ValueError("Jira enrichment exceeds its bounded context limits")
            records.append(record)
    return "".join(records)
