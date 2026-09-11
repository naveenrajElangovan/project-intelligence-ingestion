from __future__ import annotations

import json
import math
import re
import socket
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from app.config import Settings
from app.jira_chunk_context import enrichment_values
from app.models import SourceChunk, SourceDocument
from app.source_references import ReferenceValidationError, RepositoryReferences

_BLOCKED_EXTENSIONS = {
    ".7z", ".bat", ".cmd", ".com", ".dll", ".dmg", ".docm", ".exe",
    ".gz", ".iso", ".jar", ".jsm", ".msi", ".pptm", ".rar", ".scr",
    ".tar", ".vbs", ".xlsm", ".zip",
}
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|client[_-]?secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_CROSS_FIELD_SECRET_PATTERNS = tuple(
    re.compile(pattern.pattern.replace(r"\b", ""), pattern.flags)
    for pattern in _SECRET_PATTERNS
)


@dataclass(frozen=True, slots=True)
class QuarantineReason:
    code: str
    detail: str


class QuarantinedDocument(ValueError):
    def __init__(self, reason: QuarantineReason) -> None:
        super().__init__(reason.detail)
        self.reason = reason


class ContentSecurityScanner:
    """Fail-closed checks performed before content leaves the ingestion boundary."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._references = RepositoryReferences(
            settings.jira_reference_manifests, settings.jira_approved_reference_roots
        )

    def _sensitive(self, value: str, document: SourceDocument) -> bool:
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            return True
        try:
            normalized = self._references.normalize(value, document.project_id) if document.provider.upper() == "JIRA" else value
        except ReferenceValidationError as error:
            raise QuarantinedDocument(QuarantineReason(
                "UNVERIFIED_SOURCE_REFERENCE", "A repository reference failed pinned provenance validation."
            )) from error
        return _has_high_entropy_secret(normalized)

    def inspect_generated(self, document: SourceDocument, chunks: tuple[SourceChunk, ...]) -> None:
        """Scan final Jira payloads before writes, including generated enrichment."""
        if document.provider.upper() != "JIRA":
            return
        groups = [document.metadata]
        for chunk in chunks:
            groups.append(chunk.metadata)
            for value in (chunk.content, chunk.embedding_text or ""):
                if self._sensitive(value, document):
                    self._reject_generated()
        for metadata in groups:
            values = list(metadata_text_values(metadata))
            if len(values) > 8192 or sum(len(value.encode("utf-8")) for value in values) > 4_000_000:
                raise QuarantinedDocument(QuarantineReason("METADATA_LIMIT", "Generated metadata exceeds its bounded scan limits."))
            if any(self._sensitive(value, document) for value in values):
                self._reject_generated()
            canonical = json.dumps(metadata, sort_keys=True, ensure_ascii=False)
            if any(pattern.search(canonical) for pattern in _SECRET_PATTERNS):
                self._reject_generated()
            if any(self._sensitive(value, document) for value in metadata_text_values(canonical)):
                self._reject_generated()
            # Only lists/tuples and our documented enrichment emission schema
            # define adjacency. Unordered sibling fields are never concatenated.
            ordered_groups = [enrichment_values(metadata), *ordered_metadata_values(metadata)]
            for ordered in ordered_groups:
                if any(pattern.search("".join(ordered)) for pattern in _CROSS_FIELD_SECRET_PATTERNS):
                    self._reject_generated()
                normalized = [_entropy_scan_text(self._references.normalize(value, document.project_id)) for value in ordered]
                if _has_high_entropy_secret("".join(normalized)):
                    self._reject_generated()
                for start in range(len(normalized)):
                    joined = ""
                    for value in normalized[start:]:
                        joined += value
                        if _has_high_entropy_secret(joined):
                            self._reject_generated()
                        if len(joined) >= 256:
                            break

    @staticmethod
    def _reject_generated() -> None:
        raise QuarantinedDocument(QuarantineReason(
            "POTENTIAL_SECRET", "Potential credential material was detected in generated content."
        ))

    def inspect(self, document: SourceDocument) -> bool:
        path = str(document.metadata.get("path") or document.title)
        suffix = Path(path).suffix.lower()
        if suffix in _BLOCKED_EXTENSIONS:
            raise QuarantinedDocument(
                QuarantineReason("BLOCKED_FILE_TYPE", "The source file type is not allowed.")
            )
        payload = document.content_bytes
        if payload is not None:
            if len(payload) > self._settings.max_attachment_bytes:
                raise QuarantinedDocument(
                    QuarantineReason("FILE_TOO_LARGE", "The source exceeds the configured size limit.")
                )
            self._validate_signature(suffix, payload)
            if self._settings.enable_malware_scan:
                self._clamav_scan(payload)
            sample = payload[:2_000_000].decode("utf-8", errors="ignore")
        else:
            sample = document.content[:2_000_000]
        credential_sensitive = self._sensitive(sample, document)
        if document.provider.upper() == "JIRA":
            # Jira retains original fields as provenance. Those values have the
            # same security boundary as embedded text, including nested/JSON
            # metadata; serialization punctuation is not part of a URL value.
            credential_sensitive = credential_sensitive or any(
                self._sensitive(value, document)
                for value in metadata_text_values(document.metadata)
            )
        if credential_sensitive and document.provider.upper() != "LOCAL":
            raise QuarantinedDocument(
                QuarantineReason("POTENTIAL_SECRET", "Potential credential material was detected.")
            )
        for visual in document.local_visuals:
            if len(visual.content) > self._settings.max_attachment_bytes:
                raise QuarantinedDocument(
                    QuarantineReason("FILE_TOO_LARGE", "A linked visual exceeds the configured size limit.")
                )
            valid = (
                visual.content.startswith(b"\x89PNG\r\n\x1a\n")
                or visual.content.startswith(b"\xff\xd8\xff")
                or visual.content.startswith((b"RIFF",)) and visual.content[8:12] == b"WEBP"
            )
            if not valid:
                raise QuarantinedDocument(
                    QuarantineReason("MIME_MISMATCH", "A linked visual has an invalid raster signature.")
                )
            if self._settings.enable_malware_scan:
                self._clamav_scan(visual.content)
        return credential_sensitive

    @staticmethod
    def _validate_signature(suffix: str, payload: bytes) -> None:
        signatures = {
            ".pdf": payload.startswith(b"%PDF-"),
            ".docx": payload.startswith(b"PK\x03\x04"),
            ".pptx": payload.startswith(b"PK\x03\x04"),
            ".xlsx": payload.startswith(b"PK\x03\x04"),
        }
        if suffix in signatures and not signatures[suffix]:
            raise QuarantinedDocument(
                QuarantineReason("MIME_MISMATCH", "The file signature does not match its extension.")
            )
        if suffix in {".docx", ".pptx", ".xlsx"}:
            try:
                with ZipFile(BytesIO(payload)) as archive:
                    names = set(archive.namelist())
            except BadZipFile as error:
                raise QuarantinedDocument(
                    QuarantineReason("MIME_MISMATCH", "The Office document container is invalid.")
                ) from error
            required_prefix = {".docx": "word/", ".pptx": "ppt/", ".xlsx": "xl/"}[suffix]
            if not any(name.startswith(required_prefix) for name in names):
                raise QuarantinedDocument(
                    QuarantineReason("MIME_MISMATCH", "The Office document type does not match its extension.")
                )
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise QuarantinedDocument(
                    QuarantineReason("MACRO_CONTENT", "Macro-enabled Office content is not allowed.")
                )
        if payload.startswith((b"MZ", b"\x7fELF")):
            raise QuarantinedDocument(
                QuarantineReason("EXECUTABLE_CONTENT", "Executable content is not allowed.")
            )
        if suffix == ".pdf" and b"/Encrypt" in payload:
            raise QuarantinedDocument(
                QuarantineReason(
                    "PASSWORD_PROTECTED",
                    "Password-protected documents are not accepted for ingestion.",
                )
            )

    def _clamav_scan(self, payload: bytes) -> None:
        if not self._settings.clamav_socket:
            raise RuntimeError("Malware scanning is enabled but PI_INGEST_CLAMAV_SOCKET is empty.")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(30)
            client.connect(self._settings.clamav_socket)
            client.sendall(b"zINSTREAM\0")
            for start in range(0, len(payload), 64 * 1024):
                block = payload[start : start + 64 * 1024]
                client.sendall(len(block).to_bytes(4, "big") + block)
            client.sendall((0).to_bytes(4, "big"))
            response = client.recv(4096)
        if b"FOUND" in response:
            raise QuarantinedDocument(
                QuarantineReason("MALWARE_DETECTED", "Malware scanning rejected the source.")
            )
        if b"OK" not in response:
            raise RuntimeError("Malware scanner did not return a valid result.")


def metadata_text_values(value, depth=0, *, include_keys=True):
    if depth > 32:
        raise QuarantinedDocument(
            QuarantineReason("METADATA_TOO_DEEP", "Source metadata exceeds the nesting limit.")
        )
    if isinstance(value, dict):
        for key, item in value.items():
            if include_keys:
                yield str(key)
            if include_keys and isinstance(item, (str, int, float)) and re.search(
                r"(?i)(?:api[_-]?key|client[_-]?secret|password)$", str(key)
            ):
                yield f"{key}={item}"
            yield from metadata_text_values(item, depth + 1, include_keys=include_keys)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from metadata_text_values(item, depth + 1, include_keys=include_keys)
    elif isinstance(value, str):
        try:
            decoded = json.loads(value) if value.lstrip().startswith(("[", "{")) else None
        except ValueError:
            decoded = None
        if isinstance(decoded, (list, dict)):
            yield from metadata_text_values(decoded, depth + 1, include_keys=include_keys)
        else:
            yield value


def ordered_metadata_values(value, depth=0):
    if depth > 32:
        raise QuarantinedDocument(QuarantineReason("METADATA_TOO_DEEP", "Source metadata exceeds the nesting limit."))
    if isinstance(value, dict):
        for item in value.values():
            yield from ordered_metadata_values(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        adjacent = []
        for item in value:
            try:
                decoded = json.loads(item) if isinstance(item, str) and item.lstrip().startswith(("[", "{")) else None
            except ValueError:
                decoded = None
            if isinstance(item, str) and not isinstance(decoded, (dict, list)):
                adjacent.append(item)
            else:
                if adjacent:
                    yield adjacent
                    adjacent = []
                yield from ordered_metadata_values(item, depth + 1)
        if adjacent:
            yield adjacent
    elif isinstance(value, str):
        try:
            decoded = json.loads(value) if value.lstrip().startswith(("[", "{")) else None
        except ValueError:
            decoded = None
        if isinstance(decoded, (dict, list)):
            yield from ordered_metadata_values(decoded, depth + 1)


def _entropy_scan_text(value: str) -> str:
    # Immutable source citations contain many ordinary path components. Combining
    # them into one slash-delimited candidate gives misleading token entropy.
    # Only recognize a strict, query-free citation form; scan every component and
    # keep explicit credential matching on the original document in inspect().
    return re.sub(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/blob/"
        r"[0-9a-f]{40}/[A-Za-z0-9_./-]+(?:#L[0-9]+(?:-L[0-9]+)?)?"
        r"(?=$|[\s)>\]])",
        # Percent-encoded paths are deliberately outside this recognizer, so
        # accepted path components are already decoded. Traversal is excluded.
        lambda match: (
            match.group()
            if any(part in {".", ".."} for part in match.group().split("/"))
            else match.group().replace("/", " ")
        ),
        value,
    )


def _has_high_entropy_secret(value: str) -> bool:
    value = _entropy_scan_text(value)
    candidates = re.findall(r"\b[A-Za-z0-9+/=_-]{40,200}\b", value)
    for candidate in candidates:
        counts = {character: candidate.count(character) for character in set(candidate)}
        entropy = -sum(
            (count / len(candidate)) * math.log2(count / len(candidate))
            for count in counts.values()
        )
        if entropy >= 4.7 and any(character.isdigit() for character in candidate):
            return True
    return False
