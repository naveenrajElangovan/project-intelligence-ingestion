import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SourceDocument:
    project_id: str
    provider: str
    source_id: str
    source_type: str
    title: str
    reference: str
    source_url: str
    version: str
    content: str
    updated_at: datetime | None
    metadata: dict[str, object] = field(default_factory=dict)
    deleted: bool = False
    mime_type: str = "text/plain"
    language: str = "und"
    content_bytes: bytes | None = None
    security_classification: str = "PROJECT_INTERNAL"
    local_visuals: tuple["SourceVisualInput", ...] = field(default_factory=tuple, repr=False)

    @property
    def content_hash(self) -> str:
        value = (
            self.content_bytes if self.content_bytes is not None else self.content.encode("utf-8")
        )
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceChunk:
    chunk_id: str
    source_id: str
    ordinal: int
    content: str
    content_hash: str
    structure_hash: str = ""
    embedding_text: str = ""
    structure_path: tuple[str, ...] = ()
    locator: str | None = None
    language: str = "und"
    visual_eligible: bool = False
    visual_types: tuple[str, ...] = ()
    visual_asset_ids: tuple[str, ...] = ()
    page_number: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LogicalElement:
    kind: str
    text: str
    heading_path: tuple[str, ...] = ()
    locator: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsingResult:
    schema_version: str
    parser_version: str
    source_id: str
    source_version: str
    mime_type: str
    language: str
    security_classification: str
    elements: tuple[LogicalElement, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VisualAsset:
    asset_id: str
    asset_type: str
    page_number: int | None
    locator: str | None
    caption: str
    ocr_text: str
    content_hash: str
    media_type: str
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class SourceVisualInput:
    path: str
    media_type: str
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class VisualAnalysis:
    eligible: bool
    visual_types: tuple[str, ...] = ()
    eligible_pages: tuple[int, ...] = ()
    assets: tuple[VisualAsset, ...] = ()
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructuredArtifact:
    parsing: ParsingResult
    visual: VisualAnalysis


@dataclass(frozen=True, slots=True)
class DocumentIndexResult:
    operation: str
    chunks_written: int = 0
    visual_eligible: int = 0
    visual_assets: int = 0
    visual_failures: int = 0


@dataclass(frozen=True, slots=True)
class ProviderIngestionResult:
    project_id: str
    provider: str
    discovered: int
    indexed: int
    unchanged: int
    deleted: int
    failed: int
    chunks_written: int
    documents_analyzed: int = 0
    text_only_documents: int = 0
    visual_eligible_documents: int = 0
    visual_assets_stored: int = 0
    visual_processing_failures: int = 0
    excluded: int = 0


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    path: str
    blob_sha: str
    size: int
    visual_asset: bool = False


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    status: str
    previous_path: str | None
    content: str | None
    content_hash: str | None
    source_url: str
