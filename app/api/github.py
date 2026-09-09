import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.dependencies import get_document_workflow, get_project_reader
from app.github import GitHubAppClient
from app.jobs import IngestionJob, enqueue_job
from app.models import SourceDocument
from app.projects import ProjectReader
from app.workflow import DocumentIngestionWorkflow

router = APIRouter(prefix="/v1/webhooks/github", tags=["github"])
logger = logging.getLogger(__name__)


class WebhookResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    accepted: bool
    reason: str
    project_id: str | None = Field(default=None, alias="projectId")
    repository: str | None = None
    branch: str | None = None
    commit_sha: str | None = Field(default=None, alias="commitSha")
    changed_files: int = Field(default=0, alias="changedFiles")
    deleted_files: int = Field(default=0, alias="deletedFiles")
    stored_chunks: int = Field(default=0, alias="storedChunks")
    unchanged_files: int = Field(default=0, alias="unchangedFiles")


@router.post("", response_model=WebhookResponse, response_model_by_alias=True)
async def github_webhook(
    request: Request,
    event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
    delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
    settings: Settings = Depends(get_settings),
    project_reader: ProjectReader = Depends(get_project_reader),
    workflow: DocumentIngestionWorkflow = Depends(get_document_workflow),
) -> WebhookResponse:
    body = await request.body()
    if len(body) > settings.webhook_max_body_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Webhook payload is too large.")
    _verify_signature(body, signature, settings.github_webhook_secret)
    if not delivery:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "GitHub delivery ID is required.")
    if event != "pull_request":
        return WebhookResponse(accepted=False, reason="Event is not an indexed PR merge.")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid GitHub payload.")
    pull_request = payload.get("pull_request")
    repository_payload = payload.get("repository")
    installation = payload.get("installation")
    if (
        not isinstance(pull_request, dict)
        or not isinstance(repository_payload, dict)
        or not isinstance(installation, dict)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Incomplete GitHub payload.")
    if payload.get("action") != "closed" or pull_request.get("merged") is not True:
        return WebhookResponse(accepted=False, reason="Pull request was not merged.")

    full_name = str(repository_payload.get("full_name") or "")
    owner, separator, repository = full_name.partition("/")
    if not separator or not owner or not repository:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Repository identity is invalid.")
    project = await project_reader.find_by_repository(owner, repository)
    if project is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Repository is not mapped to an active project.")
    if not project.schedule.github_merged_pr_enabled:
        return WebhookResponse(
            accepted=False,
            reason="Merged-PR ingestion is disabled for this project.",
            project_id=project.project_id,
            repository=full_name,
        )
    base = pull_request.get("base") if isinstance(pull_request.get("base"), dict) else {}
    branch = str(base.get("ref") or "")
    if branch not in project.repository.indexed_branches:
        return WebhookResponse(
            accepted=False,
            reason="Target branch is not configured for ingestion.",
            project_id=project.project_id,
            repository=full_name,
            branch=branch,
        )
    commit_sha = str(pull_request.get("merge_commit_sha") or "")
    number = payload.get("number")
    installation_id = installation.get("id")
    if not commit_sha or not isinstance(number, int) or not isinstance(installation_id, int):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Merged PR identifiers are incomplete.")
    if settings.service_bus_namespace:
        try:
            await enqueue_job(
                settings,
                IngestionJob.create(
                    project_id=project.project_id,
                    provider="GITHUB",
                    source_id=f"repository:{full_name}:branch:{branch}:pull:{number}",
                    source_version=commit_sha,
                    trigger="GITHUB_MERGED_PR",
                    delivery_id=delivery,
                ),
            )
        except Exception as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "The ingestion queue is temporarily unavailable.",
            ) from error
        return WebhookResponse(
            accepted=True,
            reason="Merged PR accepted for asynchronous ingestion.",
            project_id=project.project_id,
            repository=full_name,
            branch=branch,
            commit_sha=commit_sha,
        )
    try:
        files = await GitHubAppClient(settings, project.repository).pull_request_files(
            installation_id, number, commit_sha
        )
        scope = f"{full_name}|{branch}"
        indexed = deleted = chunks_written = unchanged = 0
        rule_arguments = (
            {"source_access_rules": project.source_access_rules}
            if project.source_access_rules
            else {}
        )
        for file in files:
            if file.status == "renamed" and file.previous_path:
                prior = _github_document(
                    project.project_id,
                    full_name,
                    branch,
                    commit_sha,
                    number,
                    file.previous_path,
                    None,
                    True,
                )
                prior_result = await workflow.run(
                    prior,
                    scope,
                    delivery,
                    project.vector_store,
                    **rule_arguments,
                )
                deleted += int(prior_result.operation == "DELETED")
            if file.status != "removed" and file.content is None:
                unchanged += 1
                continue
            document = _github_document(
                project.project_id,
                full_name,
                branch,
                commit_sha,
                number,
                file.path,
                file.content,
                file.status == "removed",
            )
            result = await workflow.run(
                document,
                scope,
                delivery,
                project.vector_store,
                **rule_arguments,
            )
            if result.operation == "INDEXED":
                indexed += 1
                chunks_written += result.chunks_written
            elif result.operation == "DELETED":
                deleted += 1
            else:
                unchanged += 1
    except Exception as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Ingestion failed after webhook validation.",
        ) from error

    logger.info(
        "github_webhook_ingestion_complete event=github_webhook_ingestion_complete "
        "project_id=%s repository=%s branch=%s commit_sha=%s changed_files=%s "
        "deleted_files=%s stored_chunks=%s unchanged_files=%s",
        project.project_id,
        full_name,
        branch,
        commit_sha,
        indexed,
        deleted,
        chunks_written,
        unchanged,
    )
    return WebhookResponse(
        accepted=True,
        reason="Merged PR delta indexed in Chroma; manifest stored in Azure Table Storage.",
        project_id=project.project_id,
        repository=full_name,
        branch=branch,
        commit_sha=commit_sha,
        changed_files=indexed,
        deleted_files=deleted,
        stored_chunks=chunks_written,
        unchanged_files=unchanged,
    )


def _verify_signature(body: bytes, signature: str | None, secret: str) -> None:
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook secret is not configured.")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid GitHub webhook signature.")


def _merged_at(value: object) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def _github_document(
    project_id: str,
    repository: str,
    branch: str,
    commit_sha: str,
    pull_request: int,
    path: str,
    content: str | None,
    deleted: bool,
) -> SourceDocument:
    return SourceDocument(
        project_id=project_id,
        provider="GITHUB",
        source_id=f"repository:{repository}:branch:{branch}:path:{path}",
        source_type="CODE",
        title=path,
        reference=f"{repository}:{branch}:{path}",
        source_url=f"https://github.com/{repository}/blob/{commit_sha}/{path}",
        version=commit_sha,
        content=content or "",
        updated_at=datetime.now(UTC),
        deleted=deleted,
        metadata={
            "repository": repository,
            "branch": branch,
            "path": path,
            "commit_sha": commit_sha,
            "pull_request": pull_request,
        },
    )
