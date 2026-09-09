from datetime import UTC, datetime, timedelta
from dataclasses import replace
import logging
import hashlib
import mimetypes
from pathlib import Path
from uuid import uuid4

from app.atlassian import AtlassianSourceClient
from app.access_rules import resolve_access_policy
from app.chunking import BINARY_DOCUMENT_EXTENSIONS
from app.config import Settings
from app.control_plane import BackendControlPlaneClient
from app.github import GitHubAppClient
from app.models import ProviderIngestionResult, SourceDocument, SourceVisualInput
from app.visual import resolve_markdown_asset_paths
from app.projects import IngestionProject
from app.state import ManifestStore, SourceManifest
from app.workflow import DocumentIngestionWorkflow
from app.telemetry import observe_scope, record_document_failure

logger = logging.getLogger(__name__)


def _github_manifest_is_unchanged(
    manifest: SourceManifest | None,
    *,
    blob_sha: str,
    access_policy_id: str,
    full: bool,
) -> bool:
    """Return true only when both GitHub content and its resolved label are unchanged."""

    return bool(
        not full
        and manifest is not None
        and not manifest.deleted
        and manifest.version == blob_sha
        and manifest.access_policy_id == access_policy_id
    )


class IngestionService:
    def __init__(
        self,
        settings: Settings,
        projects: BackendControlPlaneClient,
        manifests: ManifestStore,
        workflow: DocumentIngestionWorkflow,
    ) -> None:
        self._settings = settings
        self._projects = projects
        self._manifests = manifests
        self._workflow = workflow

    async def ingest_project(
        self,
        project_id: str,
        providers: tuple[str, ...] = ("GITHUB", "JIRA", "CONFLUENCE"),
        full: bool = False,
        target_collection_name: str | None = None,
        target_schema_version: str | None = None,
    ) -> tuple[ProviderIngestionResult, ...]:
        project = await self._projects.get(project_id)
        if project is None:
            raise LookupError("The active project mapping does not exist.")
        if target_collection_name or target_schema_version:
            if not full or not target_collection_name or not target_schema_version:
                raise ValueError(
                    "A staging collection override requires --full, collection name, and schema version."
                )
            project = replace(
                project,
                vector_store=replace(
                    project.vector_store,
                    collection_name=target_collection_name,
                    schema_version=target_schema_version,
                ),
            )
        requested = {value.upper() for value in providers}
        results: list[ProviderIngestionResult] = []
        if "GITHUB" in requested:
            results.extend(await self._ingest_github(project, full))
        if not requested.intersection({"JIRA", "CONFLUENCE"}):
            return tuple(results)
        if project.atlassian is None:
            raise RuntimeError("The backend has no Atlassian gateway for this project.")
        client = AtlassianSourceClient(
            self._settings,
            self._projects,
            project.project_id,
            project.atlassian.cloud_id,
            project.atlassian.resource_url,
        )
        if "JIRA" in requested:
            for mapping in project.jira_projects:
                scope = f"{mapping.site_url}|{mapping.project_key}"
                cursor = await self._cursor(project_id, "JIRA", scope, full)
                documents = client.jira_documents(project_id, mapping, cursor)
                results.append(
                    await self._run_scope(project, "JIRA", scope, documents, full)
                )
        if "CONFLUENCE" in requested:
            for mapping in project.confluence_spaces:
                scope = f"{mapping.site_url}|{mapping.space_id}"
                cursor = await self._cursor(project_id, "CONFLUENCE", scope, full)
                documents = client.confluence_documents(project_id, mapping, cursor)
                results.append(
                    await self._run_scope(
                        project, "CONFLUENCE", scope, documents, full
                    )
                )
        return tuple(results)

    async def _ingest_github(
        self, project: IngestionProject, full: bool
    ) -> tuple[ProviderIngestionResult, ...]:
        owner = uuid4().hex
        scope = "__all_github_scopes__"
        if not await self._manifests.acquire_scope_lease(
            project.project_id, "GITHUB", scope, owner, self._settings.scope_lease_seconds
        ):
            raise RuntimeError("A GitHub ingestion is already running for this project.")
        try:
            return await self._ingest_github_unlocked(project, full)
        finally:
            await self._manifests.release_scope_lease(project.project_id, "GITHUB", scope, owner)

    async def _ingest_github_unlocked(
        self, project: IngestionProject, full: bool
    ) -> tuple[ProviderIngestionResult, ...]:
        with observe_scope("GITHUB", full=full):
            return await self._ingest_github_scopes(project, full)

    async def _ingest_github_scopes(
        self, project: IngestionProject, full: bool
    ) -> tuple[ProviderIngestionResult, ...]:
        results: list[ProviderIngestionResult] = []
        for mapping in project.repositories:
            client = GitHubAppClient(self._settings, mapping)
            for branch in mapping.indexed_branches:
                scope = f"{mapping.full_name}|{branch}"
                scan_id = uuid4().hex
                discovered = indexed = unchanged = deleted = failed = chunks_written = 0
                analyzed = text_only = visual_eligible = visual_assets = 0
                visual_failures = 0
                commit_sha, files = await client.repository_files(branch)
                asset_files = {item.path: item for item in files if item.visual_asset}
                for item in (value for value in files if not value.visual_asset):
                    discovered += 1
                    source_id = (
                        f"repository:{mapping.full_name}:branch:{branch}:path:{item.path}"
                    )
                    manifest = await self._manifests.get_manifest(
                        project.project_id, "GITHUB", scope, source_id
                    )
                    current_access_policy_id = resolve_access_policy(
                        project.source_access_rules,
                        SourceDocument(
                            project_id=project.project_id,
                            provider="GITHUB",
                            source_id=source_id,
                            source_type="CODE",
                            title=item.path,
                            reference=f"{mapping.full_name}:{branch}:{item.path}",
                            source_url="",
                            version=item.blob_sha,
                            content="",
                            updated_at=None,
                            metadata={"path": item.path},
                        ),
                        f"project:{project.project_id}",
                    )
                    if _github_manifest_is_unchanged(
                        manifest,
                        blob_sha=item.blob_sha,
                        access_policy_id=current_access_policy_id,
                        full=full,
                    ):
                        await self._manifests.touch_manifest(manifest, scan_id)
                        unchanged += 1
                        record_document_result("GITHUB", "UNCHANGED")
                        continue
                    try:
                        suffix = Path(item.path).suffix.lower()
                        content_bytes = (
                            await client.blob_bytes(item.blob_sha)
                            if suffix in BINARY_DOCUMENT_EXTENSIONS
                            else None
                        )
                        content = "" if content_bytes is not None else await client.blob_content(item.blob_sha)
                        if content is None and content_bytes is None:
                            raise RuntimeError("GitHub blob is not a supported document.")
                        local_visuals: list[SourceVisualInput] = []
                        linked_asset_versions: list[str] = []
                        if suffix in {".md", ".markdown"} and content:
                            for path in resolve_markdown_asset_paths(content, item.path):
                                asset_file = asset_files.get(path)
                                if asset_file is None:
                                    continue
                                payload = await client.blob_bytes(asset_file.blob_sha)
                                if payload is not None:
                                    linked_asset_versions.append(asset_file.blob_sha)
                                    local_visuals.append(
                                        SourceVisualInput(
                                            path=path,
                                            media_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
                                            content=payload,
                                        )
                                    )
                        document = SourceDocument(
                            project_id=project.project_id,
                            provider="GITHUB",
                            source_id=source_id,
                            source_type="CODE",
                            title=item.path,
                            reference=f"{mapping.full_name}:{branch}:{item.path}",
                            source_url=(
                                f"https://github.com/{mapping.full_name}/blob/"
                                f"{branch}/{item.path}"
                            ),
                            version=(
                                hashlib.sha256(
                                    "|".join((item.blob_sha, *sorted(linked_asset_versions))).encode()
                                ).hexdigest()
                                if linked_asset_versions
                                else item.blob_sha
                            ),
                            content=content or "",
                            updated_at=None,
                            metadata={
                                "repository": mapping.full_name,
                                "branch": branch,
                                "path": item.path,
                                "blob_sha": item.blob_sha,
                                "commit_sha": commit_sha,
                                "file_size": item.size,
                            },
                            mime_type=mimetypes.guess_type(item.path)[0] or "application/octet-stream",
                            content_bytes=content_bytes,
                            local_visuals=tuple(local_visuals),
                        )
                        rule_arguments = (
                            {"source_access_rules": project.source_access_rules}
                            if project.source_access_rules
                            else {}
                        )
                        result = await self._workflow.run(
                            document,
                            scope,
                            scan_id,
                            project.vector_store,
                            force=full,
                            **rule_arguments,
                        )
                        if result.operation == "INDEXED":
                            indexed += 1
                            chunks_written += result.chunks_written
                            analyzed += 1
                            visual_eligible += result.visual_eligible
                            text_only += int(not result.visual_eligible)
                            visual_assets += result.visual_assets
                            visual_failures += result.visual_failures
                        else:
                            unchanged += 1
                    except Exception as failure:
                        failed += 1
                        record_document_failure("GITHUB", failure)
                        logger.exception(
                            "github_source_ingestion_failed project_id=%s repository=%s path=%s",
                            project.project_id,
                            mapping.full_name,
                            item.path,
                        )
                        if failed >= self._settings.max_document_failures_per_scope:
                            raise RuntimeError(
                                "The GitHub scope reached its document failure budget."
                            )
                if not failed:
                    deleted += await self._delete_stale(
                        project, "GITHUB", scope, scan_id, discovered
                    )
                    await self._manifests.save_cursor(
                        project.project_id, "GITHUB", scope, datetime.now(UTC)
                    )
                    await self._refresh_project_vocabulary(project)
                result = ProviderIngestionResult(
                    project_id=project.project_id,
                    provider="GITHUB",
                    discovered=discovered,
                    indexed=indexed,
                    unchanged=unchanged,
                    deleted=deleted,
                    failed=failed,
                    chunks_written=chunks_written,
                    documents_analyzed=analyzed,
                    text_only_documents=text_only,
                    visual_eligible_documents=visual_eligible,
                    visual_assets_stored=visual_assets,
                    visual_processing_failures=visual_failures,
                )
                results.append(result)
                logger.info(
                    "ingestion_scope_complete event=ingestion_scope_complete project_id=%s "
                    "provider=GITHUB repository=%s branch=%s commit_sha=%s discovered=%s "
                    "indexed=%s unchanged=%s deleted=%s failed=%s chunks_written=%s",
                    project.project_id,
                    mapping.full_name,
                    branch,
                    commit_sha,
                    discovered,
                    indexed,
                    unchanged,
                    deleted,
                    failed,
                    chunks_written,
                )
        return tuple(results)

    async def _cursor(
        self, project_id: str, provider: str, scope: str, full: bool
    ) -> datetime | None:
        if full:
            return None
        cursor = await self._manifests.get_cursor(project_id, provider, scope)
        return (
            cursor - timedelta(minutes=self._settings.incremental_overlap_minutes)
            if cursor
            else None
        )

    async def _run_scope(
        self,
        project: IngestionProject,
        provider: str,
        scope: str,
        documents,
        full: bool,
    ) -> ProviderIngestionResult:
        owner = uuid4().hex
        if not await self._manifests.acquire_scope_lease(
            project.project_id, provider, scope, owner, self._settings.scope_lease_seconds
        ):
            raise RuntimeError(f"An ingestion is already running for {provider} scope {scope}.")
        try:
            return await self._run_scope_unlocked(project, provider, scope, documents, full)
        finally:
            await self._manifests.release_scope_lease(project.project_id, provider, scope, owner)

    async def _run_scope_unlocked(
        self,
        project: IngestionProject,
        provider: str,
        scope: str,
        documents,
        full: bool,
    ) -> ProviderIngestionResult:
        with observe_scope(provider, full=full):
            return await self._run_scope_documents(
                project, provider, scope, documents, full
            )

    async def _run_scope_documents(
        self,
        project: IngestionProject,
        provider: str,
        scope: str,
        documents,
        full: bool,
    ) -> ProviderIngestionResult:
        scan_started = datetime.now(UTC)
        scan_id = uuid4().hex
        discovered = indexed = unchanged = deleted = failed = chunks_written = 0
        analyzed = text_only = visual_eligible = visual_assets = 0
        visual_failures = 0
        async for document in documents:
            discovered += 1
            try:
                rule_arguments = (
                    {"source_access_rules": project.source_access_rules}
                    if project.source_access_rules
                    else {}
                )
                result = await self._workflow.run(
                    document,
                    scope,
                    scan_id,
                    project.vector_store,
                    force=full,
                    **rule_arguments,
                )
                if result.operation == "INDEXED":
                    indexed += 1
                    chunks_written += result.chunks_written
                    analyzed += 1
                    visual_eligible += result.visual_eligible
                    text_only += int(not result.visual_eligible)
                    visual_assets += result.visual_assets
                    visual_failures += result.visual_failures
                elif result.operation == "UNCHANGED":
                    unchanged += 1
                elif result.operation == "DELETED":
                    deleted += 1
            except Exception as failure:
                failed += 1
                record_document_failure(provider, failure)
                logger.exception(
                    "source_ingestion_failed project_id=%s provider=%s source_id=%s",
                    project.project_id,
                    provider,
                    document.source_id,
                )
                if failed >= self._settings.max_document_failures_per_scope:
                    raise RuntimeError(
                        f"The {provider} scope reached its document failure budget."
                    )
        if not failed:
            if full:
                deleted += await self._delete_stale(
                    project, provider, scope, scan_id, discovered
                )
            await self._manifests.save_cursor(
                project.project_id, provider, scope, scan_started
            )
            await self._refresh_project_vocabulary(project)
        return ProviderIngestionResult(
            project_id=project.project_id,
            provider=provider,
            discovered=discovered,
            indexed=indexed,
            unchanged=unchanged,
            deleted=deleted,
            failed=failed,
            chunks_written=chunks_written,
            documents_analyzed=analyzed,
            text_only_documents=text_only,
            visual_eligible_documents=visual_eligible,
            visual_assets_stored=visual_assets,
            visual_processing_failures=visual_failures,
        )

    async def _refresh_project_vocabulary(self, project: IngestionProject) -> None:
        refresh = getattr(self._workflow, "refresh_project_vocabulary", None)
        if refresh is not None:
            await refresh(project.vector_store, project.project_id)

    async def _delete_stale(
        self,
        project: IngestionProject,
        provider: str,
        scope: str,
        scan_id: str,
        discovered: int,
    ) -> int:
        if discovered < self._settings.deletion_floor_documents:
            logger.warning(
                "stale_deletion_blocked project_id=%s provider=%s scope=%s discovered=%s floor=%s",
                project.project_id,
                provider,
                scope,
                discovered,
                self._settings.deletion_floor_documents,
            )
            return 0
        deleted = 0
        token: str | None = None
        while True:
            manifests, token = await self._manifests.stale_page(
                project.project_id, provider, scope, scan_id, token
            )
            for manifest in manifests:
                result = await self._workflow.delete_manifest(
                    manifest, scan_id, project.vector_store
                )
                if result.operation == "DELETED":
                    deleted += 1
            if not token:
                break
        return deleted
