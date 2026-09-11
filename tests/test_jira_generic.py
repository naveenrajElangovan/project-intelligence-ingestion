import asyncio
from copy import deepcopy

import httpx
import pytest

from app.atlassian import AtlassianSourceClient
from app.config import Settings
from app.jira import JiraReader, issue_document
from app.projects import JiraMapping
from tests.test_jira_complete import ISSUE, Gateway


@pytest.mark.parametrize(
    "key,status,kind",
    [
        ("SERVICE_DESK", "Awaiting customer approval", "Service request"),
        ("HR7", "Pendiente de validación", "Incorporación"),
        ("Z", "Custom terminal state", "Experiment"),
    ],
)
def test_arbitrary_project_workflow_and_fields(key, status, kind):
    issue = deepcopy(ISSUE)
    issue["key"] = f"{key}-903"
    issue["fields"].update(
        project={"key": key},
        status={"name": status},
        issuetype={"name": kind},
        customfield_98765={"value": "Especial"},
    )
    documents = [
        issue_document(
            "APPLICATION",
            JiraMapping(f"https://{site}.atlassian.net", key),
            cloud,
            issue,
            {"customfield_98765": "Local classification"},
            [],
            [],
            [],
            [],
        )
        for site, cloud in [("one", "cloud-one"), ("two", "cloud-two")]
    ]
    assert documents[0].source_id != documents[1].source_id
    for doc in documents:
        sections = doc.metadata["_jira_sections"]
        current = next(s["text"] for s in sections if s["kind"] == "CURRENT")
        assert status in current and kind in current and issue["key"] in doc.content
        assert any(
            "Local classification" in s["text"] and "Especial" in s["text"] for s in sections
        )


class PagedGateway(Gateway):
    async def atlassian_get(self, project, target, params=None):
        if target.endswith("/search/jql"):
            self.calls.append((target, deepcopy(params)))
            assert 'project = "OPS7"' in params["jql"]
            page = params.get("nextPageToken")
            data = (
                {
                    "issues": [{"id": "100", "key": "OPS7-1"}],
                    "nextPageToken": "next",
                    "isLast": False,
                }
                if not page
                else {"issues": [{"id": "101", "key": "OPS7-2"}], "isLast": True}
            )
            return httpx.Response(200, json=data, request=httpx.Request("GET", target))
        result = await super().atlassian_get(project, target, params)
        if target.endswith("/issue/101"):
            data = result.json()
            data["key"] = "OPS7-2"
            data["fields"]["project"]["key"] = "OPS7"
            data["fields"]["attachment"] = []
            return httpx.Response(200, json=data, request=httpx.Request("GET", target))
        return result


@pytest.mark.parametrize("selected,missing", [("OPS7-2", False), ("OPS7-99", True)])
def test_targeted_selection_paginates_without_hydrating_unselected_issues(selected, missing):
    gateway = PagedGateway()
    mapping = JiraMapping("https://operations.atlassian.net", "OPS7")
    reader = JiraReader(
        AtlassianSourceClient(
            Settings(_env_file=None), gateway, "APP", "operations-cloud", mapping.site_url
        )
    )

    async def run():
        return [
            doc
            async for doc in reader.documents(
                "APP", mapping, None, limit=1, selected_keys=[selected]
            )
        ]

    if missing:
        with pytest.raises(RuntimeError, match="not all found"):
            asyncio.run(run())
    else:
        assert asyncio.run(run())[0].reference == selected
    assert len([url for url, _ in gateway.calls if url.endswith("/search/jql")]) == 2
    assert not any("/issue/100" in url for url, _ in gateway.calls)
