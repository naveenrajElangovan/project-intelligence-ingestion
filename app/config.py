import base64
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: str = "development"
    log_level: str = "INFO"
    metrics_pushgateway_url: str = ""
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    docs_enabled: bool = True
    force_https: bool = False
    webhook_max_body_bytes: int = 2_097_152
    control_plane_url: str = "http://localhost:8001"
    control_plane_api_key: str = ""
    github_app_id: str = ""
    github_private_key_base64: str = ""
    github_webhook_secret: str = ""
    github_max_file_bytes: int = 1_000_000
    chroma_host: str = "chroma"
    chroma_port: int = 8000
    chroma_collection: str = "project-intelligence"
    state_table_endpoint: str = ""
    state_table_name: str = "piingestionstate"
    state_managed_identity_client_id: str = ""
    internal_api_key: str = ""
    incremental_overlap_minutes: int = 5
    scope_lease_seconds: int = 900
    deletion_floor_documents: int = 1
    max_document_failures_per_scope: int = 5
    source_page_size: int = 100
    max_attachment_bytes: int = 25_000_000
    chunk_max_tokens: int = 420
    code_chunk_max_tokens: int = 350
    table_chunk_max_tokens: int = 300
    chunk_overlap_tokens: int = 40
    # The embedding model's position count. Exposed only so a different model
    # can be pinned without editing the chunker; it is never raised above what
    # the model actually reads, because the excess is dropped silently.
    embedding_position_limit: int = 512
    embedding_tokenizer: str = "intfloat/multilingual-e5-large"
    # Passage and query vectors are always generated locally from the same model.
    embedding_dimensions: int = 1024
    local_embedding_model: str = "intfloat/multilingual-e5-large"
    local_embedding_revision: str = ""
    local_embedding_device: str = "cpu"
    local_embedding_path: str = ""
    local_embedding_batch_size: int = 16
    parser_version: str = "docling-visual-v2"
    # Bumping this is what makes existing sources re-chunk. The inspect node
    # SKIPs a document when version, content hash, parser, chunker, embedding
    # profile and schema all match, so a chunker rewrite that leaves this alone
    # improves nothing already indexed: every unchanged source keeps the chunks
    # its old chunker produced. v5 covers token-accurate sizing, table integrity,
    # boundary-aware prose windows, and content-sniffed format routing.
    chunker_version: str = "semantic-token-entity-metadata-v9"
    schema_version: str = "3"
    docling_max_pages: int = 500
    docling_timeout_seconds: int = 300
    docling_max_concurrency: int = 2
    docling_artifacts_path: str = ""
    enable_malware_scan: bool = False
    clamav_socket: str = ""
    service_bus_namespace: str = ""
    service_bus_queue_name: str = "pi-document-parsing"
    visual_analysis_enabled: bool = False
    visual_max_assets_per_document: int = 64
    visual_max_pages_per_document: int = 100
    visual_min_area_pixels: int = 16_384

    model_config = SettingsConfigDict(
        env_prefix="PI_INGEST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @model_validator(mode="after")
    def validate_embedding(self) -> "Settings":
        if self.embedding_dimensions not in {384, 768, 1024}:
            raise ValueError("PI_INGEST_EMBEDDING_DIMENSIONS must be 384, 768, or 1024")
        if self.local_embedding_batch_size < 1 or self.local_embedding_batch_size > 64:
            raise ValueError("PI_INGEST_LOCAL_EMBEDDING_BATCH_SIZE must be between 1 and 64")
        if not self.local_embedding_model:
            raise ValueError("PI_INGEST_LOCAL_EMBEDDING_MODEL is required for local embedding")
        # A passage longer than the model's 512 positions would be silently
        # truncated, so the chunk budget must stay inside it.
        if self.chunk_max_tokens > 480:
            raise ValueError(
                "PI_INGEST_CHUNK_MAX_TOKENS must stay at or below 480 so passages fit "
                "the embedding model's 512-token limit without silent truncation"
            )
        return self

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if not self.is_production:
            return self
        errors: list[str] = []
        if not self.github_app_id.isdigit():
            errors.append("PI_INGEST_GITHUB_APP_ID must be configured")
        try:
            private_key = base64.b64decode(
                self.github_private_key_base64, validate=True
            ).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            private_key = ""
        if "BEGIN" not in private_key or "PRIVATE KEY" not in private_key:
            errors.append("PI_INGEST_GITHUB_PRIVATE_KEY_BASE64 must be configured")
        if len(self.github_webhook_secret) < 32:
            errors.append("PI_INGEST_GITHUB_WEBHOOK_SECRET must contain at least 32 characters")
        if not self.chroma_host or self.chroma_port < 1 or not self.chroma_collection:
            errors.append("PI_INGEST_CHROMA_HOST, PI_INGEST_CHROMA_PORT, and PI_INGEST_CHROMA_COLLECTION are required")
        if not self.control_plane_url.startswith("https://"):
            errors.append("PI_INGEST_CONTROL_PLANE_URL must use HTTPS")
        if len(self.control_plane_api_key) < 32:
            errors.append("PI_INGEST_CONTROL_PLANE_API_KEY must contain at least 32 characters")
        if not self.state_table_endpoint.startswith("https://") or not self.state_table_endpoint.endswith(
            ".table.core.windows.net"
        ):
            errors.append("PI_INGEST_STATE_TABLE_ENDPOINT must be an Azure Table endpoint")
        if len(self.internal_api_key) < 32:
            errors.append("PI_INGEST_INTERNAL_API_KEY must contain at least 32 characters")
        if self.docs_enabled:
            errors.append("PI_INGEST_DOCS_ENABLED must be false")
        if not self.force_https:
            errors.append("PI_INGEST_FORCE_HTTPS must be true")
        if not self.allowed_host_list or "*" in self.allowed_host_list:
            errors.append("PI_INGEST_ALLOWED_HOSTS must contain explicit public hosts")
        if not self.enable_malware_scan or not self.clamav_socket:
            errors.append("malware scanning and PI_INGEST_CLAMAV_SOCKET are required")
        if not self.service_bus_namespace.endswith(".servicebus.windows.net"):
            errors.append("PI_INGEST_SERVICE_BUS_NAMESPACE must be an Azure Service Bus namespace")
        if not self.docling_artifacts_path:
            errors.append("PI_INGEST_DOCLING_ARTIFACTS_PATH must contain pinned local models")
        if self.max_attachment_bytes > 25_000_000 or self.docling_max_pages > 500:
            errors.append("document size/page limits exceed the production policy")
        if self.docling_timeout_seconds > 300 or self.docling_max_concurrency > 2:
            errors.append("Docling timeout/concurrency exceed the production policy")
        if errors:
            raise ValueError("Unsafe production configuration: " + "; ".join(errors))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
