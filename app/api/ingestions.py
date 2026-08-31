import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.dependencies import get_ingestion_service
from app.models import ProviderIngestionResult
from app.service import IngestionService

router = APIRouter(prefix="/v1/projects", tags=["ingestions"])


class IngestionRequest(BaseModel):
    providers: list[Literal["GITHUB", "JIRA", "CONFLUENCE"]] = Field(
        default_factory=lambda: ["GITHUB", "JIRA", "CONFLUENCE"]
    )
    full: bool = False


class ProviderResult(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    project_id: str = Field(alias="projectId")
    provider: str
    discovered: int
    indexed: int
    unchanged: int
    deleted: int
    failed: int
    chunks_written: int = Field(alias="chunksWritten")
    documents_analyzed: int = Field(alias="documentsAnalyzed")
    text_only_documents: int = Field(alias="textOnlyDocuments")
    visual_eligible_documents: int = Field(alias="visualEligibleDocuments")
    visual_assets_stored: int = Field(alias="visualAssetsStored")
    visual_processing_failures: int = Field(alias="visualProcessingFailures")


@router.post(
    "/{project_id}/ingestions",
    response_model=list[ProviderResult],
    response_model_by_alias=True,
)
async def run_ingestion(
    project_id: str,
    body: IngestionRequest,
    request: Request,
    internal_key: Annotated[str | None, Header(alias="X-Internal-Api-Key")] = None,
    settings: Settings = Depends(get_settings),
    service: IngestionService = Depends(get_ingestion_service),
) -> list[ProviderIngestionResult]:
    _authorize(request, internal_key, settings)
    if not body.providers:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Select a provider.")
    try:
        return list(
            await service.ingest_project(
                project_id, tuple(body.providers), full=body.full
            )
        )
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error


def _authorize(request: Request, supplied: str | None, settings: Settings) -> None:
    if settings.internal_api_key:
        if not supplied or not secrets.compare_digest(supplied, settings.internal_api_key):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal API key.")
        return
    host = request.client.host if request.client else ""
    if settings.is_production or host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Internal API authentication is not configured.",
        )
