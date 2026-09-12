from functools import lru_cache

from app.config import get_settings
from app.control_plane import BackendControlPlaneClient
from app.embedding import build_passage_embedder
from app.service import IngestionService
from app.state import AzureTableManifestStore, ManifestStore, MongoManifestStore
from app.vector import ChromaVectorStore
from app.workflow import DocumentIngestionWorkflow


@lru_cache
def get_project_reader() -> BackendControlPlaneClient:
    return BackendControlPlaneClient(get_settings())


@lru_cache
def get_vector_store() -> ChromaVectorStore:
    settings = get_settings()
    return ChromaVectorStore(
        settings.chroma_host, settings.chroma_port, settings.chroma_collection,
        build_passage_embedder(settings),
    )


@lru_cache
def get_manifest_store() -> ManifestStore:
    settings = get_settings()
    if settings.state_backend == "MONGODB":
        return MongoManifestStore.from_settings(settings)
    return AzureTableManifestStore.from_settings(settings)


@lru_cache
def get_document_workflow() -> DocumentIngestionWorkflow:
    settings = get_settings()
    return DocumentIngestionWorkflow(
        settings,
        get_manifest_store(),
        get_vector_store(),
    )


@lru_cache
def get_ingestion_service() -> IngestionService:
    return IngestionService(
        get_settings(),
        get_project_reader(),
        get_manifest_store(),
        get_document_workflow(),
    )
