import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings
from app.control_plane import BackendControlPlaneClient
from app.models import SourceDocument
from app.projects import ConfluenceMapping, JiraMapping


class AtlassianSourceClient:
    """Reads only source mappings selected for the active project."""

    def __init__(
        self,
        settings: Settings,
        gateway: BackendControlPlaneClient,
        project_id: str,
        cloud_id: str,
        resource_url: str,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._project_id = project_id
        self._cloud_id = cloud_id
        self._resource_url = resource_url.rstrip("/")

    async def jira_documents(
        self,
        project_id: str,
        mapping: JiraMapping,
        updated_since: datetime | None,
    ) -> AsyncIterator[SourceDocument]:
        from app.jira import JiraReader

        reader = JiraReader(self)
        self.jira_reader = reader
        async for document in reader.documents(project_id, mapping, updated_since):
            yield document

    async def confluence_documents(
        self,
        project_id: str,
        mapping: ConfluenceMapping,
        updated_since: datetime | None,
    ) -> AsyncIterator[SourceDocument]:
        origin = f"https://api.atlassian.com/ex/confluence/{self._cloud_id}"
        pages = await self._confluence_pages(origin, mapping)
        pages_by_id = {
            str(page.get("id") or ""): page for page in pages if str(page.get("id") or "")
        }
        for page in pages:
            if not _inside_v2_roots(page, mapping, pages_by_id):
                continue
            page_updated_at = _confluence_updated_at(page)
            page_id = str(page.get("id") or "")
            if page_id:
                page["labels"] = await self._confluence_labels(origin, page_id)
            if _on_or_after(page_updated_at, updated_since):
                document = _confluence_page(project_id, mapping, page)
                if not document.content.strip():
                    document = await self._confluence_live_body(origin, project_id, mapping, page)
                yield document
            if not page_id:
                continue
            async for attachment in self._confluence_page_attachments(origin, page_id):
                if not _on_or_after(_confluence_updated_at(attachment), updated_since):
                    continue
                document = await self._confluence_attachment(
                    origin, project_id, mapping, attachment
                )
                if document:
                    yield document

    async def _confluence_pages(
        self, origin: str, mapping: ConfluenceMapping
    ) -> list[dict[str, Any]]:
        url: str | None = f"{origin}/wiki/api/v2/pages"
        params: dict[str, str | int] | None = {
            "space-id": mapping.space_id,
            "status": "current",
            # One value only. `body-format` does not accept a comma-separated
            # list: Atlassian rejects it with 400, which the control-plane proxy
            # surfaces as 502. Live docs that carry no storage body are handled
            # per page by _confluence_live_body below, which costs one extra
            # request for the few pages that need it instead of breaking every
            # request for the many that do not.
            "body-format": "storage",
            "limit": self._settings.source_page_size,
        }
        pages: list[dict[str, Any]] = []
        while url:
            response = await self._get(url, params)
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise RuntimeError("Confluence returned an invalid pages response.")
            pages.extend(item for item in results if isinstance(item, dict))
            url = _next_url(origin, payload)
            params = None
        return pages

    async def _confluence_labels(self, origin: str, page_id: str) -> list[str]:
        try:
            response = await self._get(
                f"{origin}/wiki/api/v2/pages/{page_id}/labels",
                {"limit": self._settings.source_page_size},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            # Labels improve routing but are not page content. Structural
            # inference remains available when an older gateway lacks this API.
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        return sorted(
            {
                str(item.get("name") or item.get("label") or "").strip()
                for item in results
                if isinstance(item, dict)
                and str(item.get("name") or item.get("label") or "").strip()
            }
        )

    async def _confluence_live_body(
        self,
        origin: str,
        project_id: str,
        mapping: ConfluenceMapping,
        page: dict[str, Any],
    ) -> SourceDocument:
        """Re-read one page as ADF when its storage body was empty.

        A live doc is authored in ADF and may return no storage representation.
        Rather than ask every request for both formats -- which the API rejects --
        the second format is fetched only for the page that needs it. A failure
        here returns the original empty document, which the chunker then rejects
        with a named error and counts as failed, instead of quietly indexing
        nothing.
        """

        page_id = str(page.get("id") or "")
        if not page_id:
            return _confluence_page(project_id, mapping, page)
        try:
            response = await self._get(
                f"{origin}/wiki/api/v2/pages/{page_id}",
                {"body-format": "atlas_doc_format"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return _confluence_page(project_id, mapping, page)
        if not isinstance(payload, dict):
            return _confluence_page(project_id, mapping, page)
        # Keep the listing's metadata (version, title, timestamps) and take only
        # the body from the second read, so the manifest still versions on what
        # the listing reported.
        merged = {**page, "body": payload.get("body") or {}}
        return _confluence_page(project_id, mapping, merged)

    async def _confluence_page_attachments(
        self, origin: str, page_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        url: str | None = f"{origin}/wiki/api/v2/pages/{page_id}/attachments"
        params: dict[str, str | int] | None = {
            "status": "current",
            "limit": self._settings.source_page_size,
        }
        while url:
            response = await self._get(url, params)
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise RuntimeError("Confluence returned an invalid attachments response.")
            for attachment in results:
                if isinstance(attachment, dict):
                    attachment.setdefault("pageId", page_id)
                    yield attachment
            url = _next_url(origin, payload)
            params = None

    async def _jira_attachment(
        self,
        project_id: str,
        mapping: JiraMapping,
        issue: dict[str, Any],
        attachment: dict[str, Any],
    ) -> SourceDocument | None:
        size = _integer(attachment.get("size"))
        if size is None or size > self._settings.max_attachment_bytes:
            return None
        filename = str(attachment.get("filename") or attachment.get("id") or "attachment")
        media_type = str(attachment.get("mimeType") or "application/octet-stream")
        download = str(attachment.get("content") or "")
        if not download:
            return None
        response = await self._get(download, {"jira_issue_key": str(issue.get("key") or "")})
        response.raise_for_status()
        if len(response.content) > self._settings.max_attachment_bytes:
            return None
        attachment_id = str(attachment.get("id") or "")
        issue_key = str(issue.get("key") or "")
        created = _datetime(attachment.get("created"))
        return SourceDocument(
            project_id=project_id,
            provider="JIRA",
            source_id=f"attachment:{attachment_id}",
            source_type="ATTACHMENT",
            title=filename,
            reference=f"{issue_key}:{filename}",
            source_url=f"{mapping.site_url}/browse/{issue_key}",
            version=f"{attachment_id}:{attachment.get('created') or size}",
            content="",
            updated_at=created,
            metadata={
                "project_key": mapping.project_key,
                "issue_key": issue_key,
                "attachment_id": attachment_id,
                "media_type": media_type,
                "file_size": size,
            },
            mime_type=media_type,
            content_bytes=response.content,
        )

    async def _confluence_attachment(
        self,
        origin: str,
        project_id: str,
        mapping: ConfluenceMapping,
        content: dict[str, Any],
    ) -> SourceDocument | None:
        extensions = (
            content.get("extensions") if isinstance(content.get("extensions"), dict) else {}
        )
        metadata = content.get("metadata") if isinstance(content.get("metadata"), dict) else {}
        size = _integer(content.get("fileSize")) or _integer(extensions.get("fileSize"))
        if size is not None and size > self._settings.max_attachment_bytes:
            return None
        title = str(content.get("title") or content.get("id") or "attachment")
        media_type = str(
            metadata.get("mediaType") or content.get("mediaType") or "application/octet-stream"
        )
        links = content.get("_links") if isinstance(content.get("_links"), dict) else {}
        download = str(content.get("downloadLink") or links.get("download") or "")
        if not download:
            return None
        download_url = _confluence_absolute(origin, download)
        response = await self._get(download_url)
        response.raise_for_status()
        if len(response.content) > self._settings.max_attachment_bytes:
            return None
        content_id = str(content.get("id") or "")
        version = content.get("version") if isinstance(content.get("version"), dict) else {}
        container = content.get("container") if isinstance(content.get("container"), dict) else {}
        updated_at = _confluence_updated_at(content)
        return SourceDocument(
            project_id=project_id,
            provider="CONFLUENCE",
            source_id=f"attachment:{content_id}",
            source_type="ATTACHMENT",
            title=title,
            reference=content_id,
            source_url=_confluence_url(mapping.site_url, content),
            version=str(
                version.get("number")
                or version.get("createdAt")
                or version.get("when")
                or content_id
            ),
            content="",
            updated_at=updated_at,
            metadata={
                "space_key": mapping.space_key,
                "space_id": mapping.space_id,
                "attachment_id": content_id,
                "page_id": str(content.get("pageId") or container.get("id") or ""),
                "media_type": media_type,
                "file_size": size or len(response.content),
            },
            mime_type=media_type,
            content_bytes=response.content,
        )

    async def _get(self, target: str, params: dict[str, str | int] | None = None) -> httpx.Response:
        return await self._gateway.atlassian_get(self._project_id, target, params)


def _jira_issue(project_id: str, mapping: JiraMapping, issue: dict[str, Any]) -> SourceDocument:
    issue_id = str(issue.get("id") or "")
    key = str(issue.get("key") or issue_id)
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    summary = str(fields.get("summary") or key)
    comments = fields.get("comment") if isinstance(fields.get("comment"), dict) else {}
    comment_values = comments.get("comments") if isinstance(comments.get("comments"), list) else []
    description = _adf_text(fields.get("description"))
    comment_text = "\n\n".join(
        _adf_text(comment.get("body"))
        for comment in comment_values
        if isinstance(comment, dict) and _adf_text(comment.get("body")).strip()
    )
    body = "\n\n".join(
        part
        for part in (
            "## Issue summary\n"
            f"{key}: {summary}\n"
            f"Type: {_nested_name(fields.get('issuetype')) or ''}\n"
            f"Status: {_nested_name(fields.get('status')) or ''}\n"
            f"Priority: {_nested_name(fields.get('priority')) or ''}",
            f"## Description\n{description}" if description else "",
            f"## Comments\n{comment_text}" if comment_text else "",
        )
        if part
    )
    updated = str(fields.get("updated") or fields.get("created") or issue_id)
    raw_status = fields.get("status")
    status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
    raw_status_category = status.get("statusCategory")
    status_category: dict[str, Any] = (
        raw_status_category if isinstance(raw_status_category, dict) else {}
    )
    raw_resolution = fields.get("resolution")
    resolution: dict[str, Any] = raw_resolution if isinstance(raw_resolution, dict) else {}
    return SourceDocument(
        project_id=project_id,
        provider="JIRA",
        source_id=f"issue:{issue_id or key}",
        source_type="ISSUE",
        title=summary,
        reference=key,
        source_url=f"{mapping.site_url}/browse/{key}",
        version=updated,
        content=body,
        updated_at=_datetime(updated),
        metadata={
            "project_key": mapping.project_key,
            "issue_key": key,
            "issue_type": _nested_name(fields.get("issuetype")),
            "status": _nested_name(fields.get("status")),
            "status_category": str(status_category.get("name") or ""),
            "status_category_key": str(status_category.get("key") or ""),
            "resolution": str(resolution.get("name") or ""),
            "resolution_id": str(resolution.get("id") or ""),
            "priority": _nested_name(fields.get("priority")),
            "assignee": _nested_display_name(fields.get("assignee")),
            "reporter": _nested_display_name(fields.get("reporter")),
            "labels": [str(value) for value in fields.get("labels", []) if isinstance(value, str)],
            "due_date": str(fields.get("duedate") or ""),
        },
    )


def _atlas_doc_text(body: dict[str, Any]) -> str:
    """Render an atlas_doc_format body as text when no storage body was returned.

    A live doc is authored in ADF, and the storage representation may be absent.
    Without this the page body is an empty string, which used to be indexed as a
    source with zero chunks and a committed manifest -- reported as success.

    Tables are rendered row-wise with pipes, matching how the HTML path renders
    them, so the same document produces comparable evidence whichever
    representation it arrives in.
    """

    document = body.get("atlas_doc_format") if isinstance(body, dict) else None
    if not isinstance(document, dict):
        return ""
    raw = document.get("value")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ""
    if not isinstance(raw, dict):
        return ""
    return "\n".join(_adf_blocks(raw)).strip()


def _adf_blocks(node: dict[str, Any]) -> list[str]:
    """Flatten ADF into block-level lines, preserving headings and table rows."""

    kind = str(node.get("type") or "")
    children = node.get("content") if isinstance(node.get("content"), list) else []

    if kind == "text":
        return [str(node.get("text") or "")]
    if kind == "hardBreak":
        return [""]
    if kind == "table":
        rows: list[str] = []
        for row in children:
            if not isinstance(row, dict) or row.get("type") != "tableRow":
                continue
            cells = [
                " ".join(" ".join(_adf_blocks(cell)).split())
                for cell in (row.get("content") or [])
                if isinstance(cell, dict)
            ]
            if any(cells):
                rows.append(" | ".join(cells))
        return rows
    if kind in {"heading", "paragraph", "listItem", "taskItem", "blockquote"}:
        inline = " ".join(
            part for child in children if isinstance(child, dict) for part in _adf_blocks(child)
        )
        collapsed = " ".join(inline.split())
        if not collapsed:
            return []
        if kind == "heading":
            level = (
                node.get("attrs", {}).get("level") if isinstance(node.get("attrs"), dict) else None
            )
            hashes = "#" * int(level or 1)
            # Markdown heading syntax so the heading splitter can see structure,
            # which is what populates structure_path.
            return [f"{hashes} {collapsed}"]
        if kind == "listItem":
            return [f"- {collapsed}"]
        return [collapsed]
    if kind == "codeBlock":
        return ["\n".join(_adf_blocks_flat(children))]

    blocks: list[str] = []
    for child in children:
        if isinstance(child, dict):
            blocks.extend(_adf_blocks(child))
    return blocks


def _adf_blocks_flat(children: list[Any]) -> list[str]:
    return [part for child in children if isinstance(child, dict) for part in _adf_blocks(child)]


def _confluence_page(
    project_id: str, mapping: ConfluenceMapping, content: dict[str, Any]
) -> SourceDocument:
    content_id = str(content.get("id") or "")
    title = str(content.get("title") or content_id)
    body = content.get("body") if isinstance(content.get("body"), dict) else {}
    storage = body.get("storage") if isinstance(body.get("storage"), dict) else {}
    version = content.get("version") if isinstance(content.get("version"), dict) else {}
    raw_html = str(storage.get("value") or "")
    # Routing in the chunker is by mime type, and the ADF fallback produces
    # Markdown rather than HTML. Left as text/html it would reach the HTML path,
    # where BeautifulSoup finds no elements and yields zero chunks -- a
    # non-empty document that produces nothing, which the empty-body guard
    # cannot catch.
    mime_type = "text/html"
    if not raw_html.strip():
        raw_html = _atlas_doc_text(body)
        mime_type = "text/markdown"
    updated_at = _confluence_updated_at(content)
    return SourceDocument(
        project_id=project_id,
        provider="CONFLUENCE",
        source_id=f"page:{content_id}",
        source_type="PAGE",
        title=title,
        reference=content_id,
        source_url=_confluence_url(mapping.site_url, content),
        version=str(
            version.get("number") or version.get("createdAt") or version.get("when") or content_id
        ),
        content=raw_html,
        updated_at=updated_at,
        metadata={
            "space_key": mapping.space_key,
            "space_id": mapping.space_id,
            "page_id": content_id,
            "status": str(content.get("status") or ""),
            "labels": tuple(
                str(label) for label in content.get("labels", []) if str(label).strip()
            ),
        },
        mime_type=mime_type,
    )


def _inside_v2_roots(
    page: dict[str, Any],
    mapping: ConfluenceMapping,
    pages_by_id: dict[str, dict[str, Any]],
) -> bool:
    if not mapping.root_page_ids:
        return True
    allowed = set(mapping.root_page_ids)
    page_id = str(page.get("id") or "")
    visited: set[str] = set()
    while page_id and page_id not in visited:
        if page_id in allowed:
            return True
        visited.add(page_id)
        current = pages_by_id.get(page_id)
        if current is None:
            break
        page_id = str(current.get("parentId") or "")
    return False


def _confluence_absolute(origin: str, link: str) -> str:
    """Resolve a Confluence link against the site root rather than the proxy root.

    `origin` addresses the Atlassian proxy, and the Confluence site sits one
    segment below it at `/wiki`. An attachment's `downloadLink` from the v2 API is
    relative to that site root and does not include the segment, so joining it
    straight onto `origin` produces a path the control plane rejects and Atlassian
    would not serve. Pagination links are unaffected: `_links.next` already
    carries `/wiki`, which is why the check below is by prefix rather than
    unconditional.
    """

    if link.startswith("http"):
        return link
    if link.startswith("/wiki/"):
        return f"{origin}{link}"
    return f"{origin}/wiki{link}"


def _next_url(origin: str, payload: object) -> str | None:
    links = payload.get("_links") if isinstance(payload, dict) else None
    next_link = links.get("next") if isinstance(links, dict) else None
    if not isinstance(next_link, str) or not next_link:
        return None
    if next_link.startswith("http"):
        return next_link
    return f"{origin}{next_link if next_link.startswith('/') else f'/{next_link}'}"


def _confluence_updated_at(content: dict[str, Any]) -> datetime | None:
    version = content.get("version") if isinstance(content.get("version"), dict) else {}
    return _datetime(version.get("createdAt") or version.get("when") or content.get("createdAt"))


def _on_or_after(value: datetime | None, threshold: datetime | None) -> bool:
    return threshold is None or value is None or value >= threshold


def _confluence_url(site_url: str, content: dict[str, Any]) -> str:
    links = content.get("_links") if isinstance(content.get("_links"), dict) else {}
    web_ui = str(links.get("webui") or "")
    if web_ui:
        return f"{site_url.rstrip('/')}/wiki{web_ui}"
    return f"{site_url.rstrip('/')}/wiki/pages/viewpage.action?pageId={content.get('id')}"


def _adf_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = [str(value["text"])] if isinstance(value.get("text"), str) else []
        children = value.get("content")
        if isinstance(children, list):
            parts.extend(_adf_text(item) for item in children)
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, list):
        return " ".join(_adf_text(item) for item in value).strip()
    return ""


def _nested_name(value: object) -> str | None:
    return str(value.get("name")) if isinstance(value, dict) and value.get("name") else None


def _nested_display_name(value: object) -> str | None:
    return (
        str(value.get("displayName"))
        if isinstance(value, dict) and value.get("displayName")
        else None
    )


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _jql(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
