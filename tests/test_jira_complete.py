import asyncio
from copy import deepcopy

import httpx
import pytest

from app.atlassian import AtlassianSourceClient
from app.config import Settings
from app.control_plane import retry_delay
from app.jira import JiraReader, issue_document, link_kind, rich_text
from app.projects import JiraMapping
from app.structured_chunking import StructuredDocumentChunker

MAPPING = JiraMapping("https://personal.atlassian.net", "T0")
ISSUE = {
    "id": "101",
    "key": "T0-1",
    "fields": {
        "project": {"key": "T0"},
        "summary": "POS payment",
        "status": {"name": "In Review"},
        "updated": "2026-09-10T10:00:00Z",
        "description": "POS means point of sale software.",
        "labels": ["POS"],
        "parent": {"key": "T0-10"},
        "customfield_1": "Sprint A",
        "attachment": [
            {
                "id": "9",
                "filename": "evidence.md",
                "size": 8,
                "mimeType": "text/markdown",
                "created": "2026-09-10T09:00:00Z",
                "content": "https://personal.atlassian.net/unsafe",
            }
        ],
    },
}


class Gateway:
    def __init__(self, *, restrict=False, changing=False, fail_page=False):
        self.calls = []
        self.restrict, self.changing, self.fail_page = restrict, changing, fail_page

    async def atlassian_get(self, project, target, params=None):
        self.calls.append((target, deepcopy(params)))
        path = target.split("/rest/api/3")[-1]
        data = None
        if path == "/field":
            data = [{"id": "customfield_1", "name": "Sprint"}]
        elif path == "/search/jql":
            data = {"issues": [{"id": "101", "key": "T0-1"}], "isLast": True}
        elif path == "/issue/101":
            data = deepcopy(ISSUE)
            if params == {"fields": "updated"} and self.changing:
                data["fields"]["updated"] = "2026-09-10T11:00:00Z"
        elif path.endswith("/comment"):
            offset = params["startAt"]
            if offset and self.fail_page:
                data = {"comments": [], "total": 2}
            else:
                comment = {
                    "id": str(offset + 1),
                    "body": "An actual comment",
                    "created": "2026-09-10T09:00:00Z",
                    "author": {"displayName": "Reader"},
                }
                if self.restrict:
                    comment["visibility"] = {"type": "role", "value": "Administrators"}
                data = {"comments": [comment], "total": 2}
        elif path.endswith("/changelog"):
            data = {
                "values": [
                    {
                        "id": "7",
                        "created": "2026-09-10T08:00:00Z",
                        "items": [
                            {"field": "status", "fromString": "To Do", "toString": "In Review"}
                        ],
                    }
                ],
                "total": 1,
            }
        elif path.endswith("/worklog"):
            data = {"worklogs": [], "total": 0}
        elif path.endswith("/remotelink"):
            data = [{"id": 4, "object": {"url": "https://github.com/org/repo/pull/123"}}]
        elif path == "/attachment/content/9":
            return httpx.Response(200, content=b"evidence", request=httpx.Request("GET", target))
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data, request=httpx.Request("GET", target))


async def collect(gateway):
    client = AtlassianSourceClient(
        Settings(_env_file=None), gateway, "APP", "cloud", MAPPING.site_url
    )
    reader = JiraReader(client)
    docs = [doc async for doc in reader.documents("APP", MAPPING, None)]
    return docs, reader


def test_complete_pages_relationships_history_attachment_and_vocabulary():
    gateway = Gateway()
    docs, reader = asyncio.run(collect(gateway))
    assert len(docs) == 2
    assert docs[0].source_id == "jira:cloud:issue:101"
    assert docs[1].source_id == "jira:cloud:attachment:9"
    assert reader.counts["comments_discovered"] == 2
    assert "To Do" in docs[0].content and "In Review" in docs[0].content
    sections = docs[0].metadata["_jira_sections"]
    assert len([s for s in sections if s["kind"] == "COMMENT"]) == 2
    assert any(s["kind"] == "GLOSSARY" for s in sections)
    assert any("Sprint" in s["text"] for s in sections)
    assert docs[1].metadata["parent_issue_source_id"] == docs[0].source_id
    assert all("/unsafe" not in url for url, _ in gateway.calls)


def test_restricted_comments_are_excluded_and_counted():
    docs, reader = asyncio.run(collect(Gateway(restrict=True)))
    assert reader.counts["comments_restricted"] == 2
    assert not any(s["kind"] == "COMMENT" for s in docs[0].metadata["_jira_sections"])


@pytest.mark.parametrize("kwargs", [{"changing": True}, {"fail_page": True}])
def test_incomplete_snapshot_fails_before_any_document_is_yielded(kwargs):
    async def run():
        client = AtlassianSourceClient(
            Settings(_env_file=None), Gateway(**kwargs), "APP", "cloud", MAPPING.site_url
        )
        output = []
        with pytest.raises(RuntimeError):
            async for doc in JiraReader(client).documents("APP", MAPPING, None):
                output.append(doc)
        assert output == []

    asyncio.run(run())


def test_site_mismatch_fails_before_network():
    client = AtlassianSourceClient(
        Settings(_env_file=None), Gateway(), "APP", "cloud", "https://other.atlassian.net"
    )

    async def run():
        with pytest.raises(ValueError):
            await anext(JiraReader(client).documents("APP", MAPPING, None))

    asyncio.run(run())


def test_stable_version_and_cloud_qualified_ids():
    args = ("APP", MAPPING, "cloud", ISSUE, {}, [], [], [], [])
    assert issue_document(*args).version == issue_document(*args).version
    other = issue_document("APP", MAPPING, "other", ISSUE, {}, [], [], [], [])
    assert other.source_id != issue_document(*args).source_id


def test_adf_preserves_links_headings_code_and_mentions():
    result = rich_text(
        {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": "Acceptance"}],
                },
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "PR",
                            "marks": [
                                {"type": "link", "attrs": {"href": "https://github.com/o/r/pull/1"}}
                            ],
                        },
                        {"type": "mention", "attrs": {"text": "@Reader"}},
                    ],
                },
                {
                    "type": "codeBlock",
                    "attrs": {"language": "kotlin"},
                    "content": [{"type": "text", "text": "val a = 1"}],
                },
            ],
        }
    )
    assert "## Acceptance" in result and "[PR](https://github.com/o/r/pull/1)" in result
    assert "@Reader" in result and "```kotlin" in result
    assert link_kind("https://github.com/o/r/issues/1") == "GITHUB_ISSUE"
    assert link_kind("https://github.com/o/r/pull/1") == "GITHUB_PR"
    assert link_kind("https://evil.test/o/r/pull/1") == "REMOTE_LINK"


def test_retry_after_is_respected_and_long_wait_is_deferred():
    assert retry_delay("10", 0) >= 10
    with pytest.raises(RuntimeError):
        retry_delay("300", 0)


def test_jira_event_boundaries_do_not_mix_comments():
    class Chunker(StructuredDocumentChunker):
        def _prose_windows(self, text, maximum, overlap):
            return [text]

    docs, _ = asyncio.run(collect(Gateway()))
    values = list(Chunker(Settings(_env_file=None))._issue_values(docs[0]))
    comments = [v for v in values if v[3]["jira_chunk_kind"] == "COMMENT"]
    assert len(comments) == 2
    assert {v[3]["event_id"] for v in comments} == {"1", "2"}


def test_real_tokenizer_preserves_long_bilingual_issue_evidence():
    source = deepcopy(ISSUE)
    source["fields"]["description"] = (
        "The cashier confirms payment before printing the receipt. El cajero confirma el pago antes de imprimir el recibo.\n\n"
        * 100
    ) + "FINAL_EVIDENCE_MARKER"
    document = issue_document("APP", MAPPING, "cloud", source, {}, [], [], [], [])
    chunker = StructuredDocumentChunker(Settings(_env_file=None))
    chunks = chunker.split(document)
    assert len(chunks) > 3
    assert any("FINAL_EVIDENCE_MARKER" in c.content for c in chunks)
    assert all(c.embedding_text.endswith(c.content) for c in chunks)
    assert all(chunker._count_tokens(c.embedding_text) <= 512 for c in chunks)
    assert all(c.locator and c.metadata.get("jira_chunk_kind") for c in chunks)
