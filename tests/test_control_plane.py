import asyncio

import httpx

from app.config import Settings
from app.control_plane import BackendControlPlaneClient


def test_project_mapping_comes_from_backend_without_provider_token() -> None:
    asyncio.run(_project_mapping_comes_from_backend_without_provider_token())


async def _project_mapping_comes_from_backend_without_provider_token() -> None:
    seen_authorization = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_authorization
        seen_authorization = request.headers.get("Authorization", "")
        return httpx.Response(
            200,
            json={
                "projectId": "DEMO",
                "displayName": "DEMO",
                "active": True,
                "jiraProjects": [{"siteUrl": "https://example.atlassian.net", "projectKey": "T0"}],
                "confluenceSpaces": [],
                "githubRepositories": [],
                "vectorStore": {
                    "collectionName": "project-intelligence",
                    "textField": "chunk_text",
                },
                "ingestionSchedule": {},
                "atlassian": {
                    "cloudId": "cloud-id",
                    "resourceUrl": "https://example.atlassian.net",
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://backend.internal"
    ) as http_client:
        client = BackendControlPlaneClient(
            Settings(
                _env_file=None,
                control_plane_url="https://backend.internal",
                control_plane_api_key="service-secret",
            ),
            http_client,
        )
        project = await client.get("DEMO")

    assert project is not None
    assert project.project_id == "DEMO"
    assert project.atlassian is not None
    assert project.atlassian.cloud_id == "cloud-id"
    assert seen_authorization == "Bearer service-secret"


def test_missing_project_returns_none() -> None:
    asyncio.run(_missing_project_returns_none())


async def _missing_project_returns_none() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://backend.internal"
    ) as http_client:
        client = BackendControlPlaneClient(
            Settings(_env_file=None, control_plane_url="https://backend.internal"),
            http_client,
        )
        assert await client.get("missing") is None
