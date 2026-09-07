import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RepositoryMapping:
    owner: str
    repository: str
    indexed_branches: tuple[str, ...]
    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"


@dataclass(frozen=True, slots=True)
class JiraMapping:
    site_url: str
    project_key: str


@dataclass(frozen=True, slots=True)
class ConfluenceMapping:
    site_url: str
    space_key: str
    space_id: str
    root_page_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VectorStoreRoute:
    collection_name: str
    text_field: str
    embedding_field: str = "embedding_text"
    embedding_model: str = "multilingual-e5-large"
    schema_version: str = "3"


@dataclass(frozen=True, slots=True)
class ProjectIngestionSchedule:
    github_merged_pr_enabled: bool = True
    daily_enabled: bool = True
    daily_at: str = "00:00"
    timezone: str = "America/Mexico_City"
    manual_enabled: bool = True


@dataclass(frozen=True, slots=True)
class AtlassianGateway:
    cloud_id: str
    resource_url: str


@dataclass(frozen=True, slots=True)
class IngestionProject:
    project_id: str
    display_name: str
    repositories: tuple[RepositoryMapping, ...]
    jira_projects: tuple[JiraMapping, ...]
    confluence_spaces: tuple[ConfluenceMapping, ...]
    vector_store: VectorStoreRoute
    schedule: ProjectIngestionSchedule = ProjectIngestionSchedule()
    atlassian: AtlassianGateway | None = None

    @property
    def repository(self) -> RepositoryMapping:
        if len(self.repositories) != 1:
            raise ValueError("A repository-specific operation requires one repository mapping.")
        return self.repositories[0]

class ProjectReader(Protocol):
    async def get(self, project_id: str) -> IngestionProject | None: ...

    async def find_by_repository(
        self, owner: str, repository: str
    ) -> IngestionProject | None: ...


def project_from_payload(payload: dict[str, object]) -> IngestionProject:
    repositories = tuple(
        mapping
        for item in _list(payload.get("githubRepositories"))
        if isinstance(item, dict) and (mapping := _repository(item)) is not None
    )
    jira = tuple(
        JiraMapping(str(item.get("siteUrl") or "").rstrip("/"), str(item.get("projectKey") or ""))
        for item in _list(payload.get("jiraProjects"))
        if isinstance(item, dict) and item.get("siteUrl") and item.get("projectKey")
    )
    confluence = tuple(
        ConfluenceMapping(
            site_url=str(item.get("siteUrl") or "").rstrip("/"),
            space_key=str(item.get("spaceKey") or ""),
            space_id=str(item.get("spaceId") or ""),
            root_page_ids=tuple(str(value) for value in _list(item.get("rootPageIds"))),
        )
        for item in _list(payload.get("confluenceSpaces"))
        if isinstance(item, dict) and item.get("siteUrl") and item.get("spaceId")
    )
    vector_store = payload.get("vectorStore") if isinstance(payload.get("vectorStore"), dict) else {}
    schedule = (
        payload.get("ingestionSchedule")
        if isinstance(payload.get("ingestionSchedule"), dict)
        else {}
    )
    atlassian_payload = (
        payload.get("atlassian") if isinstance(payload.get("atlassian"), dict) else None
    )
    return IngestionProject(
        project_id=str(payload.get("projectId") or ""),
        display_name=str(payload.get("displayName") or payload.get("projectId") or ""),
        repositories=repositories,
        jira_projects=jira,
        confluence_spaces=confluence,
        vector_store=VectorStoreRoute(
            collection_name=str(vector_store.get("collectionName") or "project-intelligence"),
            text_field=str(vector_store.get("textField") or "chunk_text"),
            embedding_field=str(vector_store.get("embeddingField") or "embedding_text"),
            embedding_model=str(vector_store.get("embeddingModel") or "multilingual-e5-large"),
            schema_version=str(vector_store.get("schemaVersion") or "3"),
        ),
        schedule=ProjectIngestionSchedule(
            github_merged_pr_enabled=bool(schedule.get("githubMergedPrEnabled", True)),
            daily_enabled=bool(schedule.get("dailyEnabled", True)),
            daily_at=str(schedule.get("dailyAt") or "00:00"),
            timezone=str(schedule.get("timezone") or "America/Mexico_City"),
            manual_enabled=bool(schedule.get("manualEnabled", True)),
        ),
        atlassian=(
            AtlassianGateway(
                cloud_id=str(atlassian_payload.get("cloudId") or ""),
                resource_url=str(atlassian_payload.get("resourceUrl") or "").rstrip("/"),
            )
            if atlassian_payload
            else None
        ),
    )


def _repository(item: dict[str, object]) -> RepositoryMapping | None:
    owner = str(item.get("owner") or "")
    repository = str(item.get("repository") or "")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", owner):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository):
        return None
    return RepositoryMapping(
        owner=owner,
        repository=repository,
        indexed_branches=tuple(str(value) for value in _list(item.get("indexedBranches"))),
        include_paths=tuple(str(value) for value in _list(item.get("includePaths"))),
        exclude_paths=tuple(str(value) for value in _list(item.get("excludePaths"))),
    )


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []
