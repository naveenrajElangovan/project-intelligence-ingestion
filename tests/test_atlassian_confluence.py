import asyncio
from datetime import UTC, datetime

import httpx

from app.atlassian import AtlassianSourceClient
from app.config import Settings
from app.projects import ConfluenceMapping


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str | int] | None]] = []

    async def atlassian_get(
        self,
        project_id: str,
        target: str,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        assert project_id == "DEMO"
        self.calls.append((target, params))
        request = httpx.Request("GET", target)
        if target.endswith("/wiki/api/v2/pages") and params is not None:
            return httpx.Response(
                200,
                request=request,
                json={
                    "results": [
                        {
                            "id": "10",
                            "title": "POS",
                            "status": "current",
                            "parentId": None,
                            "version": {"number": 1, "createdAt": "2026-08-15T10:00:00Z"},
                            "body": {"storage": {"value": "<p>Root page</p>"}},
                            "_links": {"webui": "/spaces/T20/pages/10"},
                        }
                    ],
                    "_links": {"next": "/wiki/api/v2/pages?cursor=next"},
                },
            )
        if target.endswith("/wiki/api/v2/pages?cursor=next"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "results": [
                        {
                            "id": "11",
                            "title": "BOT",
                            "status": "current",
                            "parentId": "10",
                            "version": {"number": 2, "createdAt": "2026-08-16T10:00:00Z"},
                            "body": {"storage": {"value": "<p>Child page</p>"}},
                            "_links": {"webui": "/spaces/T20/pages/11"},
                        }
                    ],
                    "_links": {},
                },
            )
        if target.endswith("/wiki/api/v2/pages/10/attachments"):
            return httpx.Response(200, request=request, json={"results": [], "_links": {}})
        if target.endswith("/wiki/api/v2/pages/11/attachments"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "results": [
                        {
                            "id": "20",
                            "title": "design.txt",
                            "mediaType": "text/plain",
                            "fileSize": 6,
                            "version": {"number": 1, "createdAt": "2026-08-16T11:00:00Z"},
                            "downloadLink": "/download/attachments/11/design.txt",
                            "_links": {"webui": "/spaces/T20/pages/11"},
                        }
                    ],
                    "_links": {},
                },
            )
        if target.endswith("/download/attachments/11/design.txt"):
            return httpx.Response(200, request=request, content=b"design")
        raise AssertionError(f"Unexpected Atlassian target: {target}")


def test_confluence_v2_pages_and_page_attachments_use_current_scopes() -> None:
    asyncio.run(_confluence_v2_pages_and_page_attachments_use_current_scopes())


async def _confluence_v2_pages_and_page_attachments_use_current_scopes() -> None:
    gateway = FakeGateway()
    client = AtlassianSourceClient(
        Settings(_env_file=None, source_page_size=50),
        gateway,  # type: ignore[arg-type]
        "DEMO",
        "cloud-id",
        "https://example.atlassian.net",
    )
    mapping = ConfluenceMapping(
        site_url="https://example.atlassian.net",
        space_key="T20",
        space_id="2916360",
        root_page_ids=("10",),
    )

    documents = [
        document
        async for document in client.confluence_documents(
            "DEMO", mapping, datetime(2026, 8, 16, tzinfo=UTC)
        )
    ]

    assert [document.source_id for document in documents] == ["page:11", "attachment:20"]
    assert documents[0].content == "<p>Child page</p>"
    assert documents[0].mime_type == "text/html"
    assert documents[1].content == ""
    assert documents[1].content_bytes == b"design"
    assert documents[1].metadata["page_id"] == "11"
    targets = [target for target, _params in gateway.calls]
    assert all("/wiki/rest/api/content/search" not in target for target in targets)
    assert any("/wiki/api/v2/pages/11/attachments" in target for target in targets)
