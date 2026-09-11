import asyncio
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from app.config import Settings
from app.projects import IngestionProject, project_from_payload


def retry_delay(value: str | None, attempt: int) -> float:
    delay = min(30.0, 2.0 ** attempt) + random.uniform(0, 0.25)
    if value:
        try:
            requested = float(value)
        except ValueError:
            try:
                requested = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError):
                requested = 0
        # Do not retry earlier than the server permits. Long delays fail this
        # attempt so the scheduler can retry without holding workers indefinitely.
        if requested > 60:
            raise RuntimeError("Atlassian rate limit requires a later scheduled retry")
        delay = max(delay, requested)
    return delay


class BackendControlPlaneClient:
    """Reads mappings and provider data through the backend; never connects to SQL."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base_url = settings.control_plane_url.rstrip("/")
        self._http_client = http_client

    async def get(self, project_id: str) -> IngestionProject | None:
        return await self._project(
            f"/v1/internal/ingestion/projects/{quote(project_id, safe='')}"
        )

    async def list_jira_projects(self) -> tuple[IngestionProject, ...]:
        response = await self._request("/v1/internal/ingestion/projects")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Backend returned invalid Jira project inventory")
        return tuple(project_from_payload(value) for value in payload)

    async def find_by_repository(
        self, owner: str, repository: str
    ) -> IngestionProject | None:
        return await self._project(
            "/v1/internal/ingestion/github-project",
            params={"owner": owner, "repository": repository},
        )

    async def atlassian_get(
        self,
        project_id: str,
        target: str,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        query: list[tuple[str, str]] = [("target", target)]
        query.extend((key, str(value)) for key, value in (params or {}).items())
        path = f"/v1/internal/ingestion/projects/{quote(project_id, safe='')}/atlassian"
        retryable_statuses = {429, 500, 502, 503, 504}
        for attempt in range(3):
            try:
                response = await self._request(path, params=query, timeout=65.0)
                if response.status_code not in retryable_statuses:
                    response.raise_for_status()
                    return response
                response.raise_for_status()
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                retryable = isinstance(error, httpx.TransportError) or (
                    error.response.status_code in retryable_statuses
                )
                if not retryable or attempt == 2:
                    raise
                delay = retry_delay(error.response.headers.get("Retry-After"), attempt) if isinstance(error, httpx.HTTPStatusError) else retry_delay(None, attempt)
                await asyncio.sleep(delay)

        raise RuntimeError("Atlassian backend request exhausted retries.")

    async def ready(self) -> bool:
        try:
            response = await self._request("/health", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def _project(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> IngestionProject | None:
        retryable_statuses = {500, 502, 503, 504}
        for attempt in range(3):
            try:
                response = await self._request(path, params=params)
                if response.status_code not in retryable_statuses:
                    break
                response.raise_for_status()
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                retryable = isinstance(error, httpx.TransportError) or (
                    error.response.status_code in retryable_statuses
                )
                if not retryable or attempt == 2:
                    raise
                await asyncio.sleep(2**attempt)
        else:  # pragma: no cover - the loop either breaks or raises
            raise RuntimeError("Backend project lookup exhausted retries.")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Backend returned an invalid ingestion project response.")
        return project_from_payload(payload)

    async def _request(
        self,
        path: str,
        *,
        params=None,
        timeout: float = 20.0,
    ) -> httpx.Response:
        if not self._base_url:
            raise RuntimeError("PI_INGEST_CONTROL_PLANE_URL is required.")
        headers = {"Accept": "application/json"}
        if self._settings.control_plane_api_key:
            headers["Authorization"] = f"Bearer {self._settings.control_plane_api_key}"
        if self._http_client is not None:
            return await self._http_client.get(path, params=params, headers=headers)
        async with httpx.AsyncClient(base_url=self._base_url, timeout=timeout) as client:
            return await client.get(path, params=params, headers=headers)
