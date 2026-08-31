from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
from pathlib import Path
import re
import socket
from zipfile import BadZipFile, ZipFile

from app.config import Settings
from app.models import SourceDocument


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
        credential_sensitive = (
            any(pattern.search(sample) for pattern in _SECRET_PATTERNS)
            or _has_high_entropy_secret(sample)
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


def _has_high_entropy_secret(value: str) -> bool:
    candidates = re.findall(r"\b[A-Za-z0-9+/=_-]{40,200}\b", value)
    for candidate in candidates[:100]:
        counts = {character: candidate.count(character) for character in set(candidate)}
        entropy = -sum(
            (count / len(candidate)) * math.log2(count / len(candidate))
            for count in counts.values()
        )
        if entropy >= 4.7 and any(character.isdigit() for character in candidate):
            return True
    return False
