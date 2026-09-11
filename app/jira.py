"""Complete, bounded Jira reads and source-preserving issue normalization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from app.models import SourceDocument
from app.telemetry import record_jira_read


def rich_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(rich_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    kind = value.get("type")
    attrs = value.get("attrs") or {}
    text = str(value.get("text") or "") or rich_text(value.get("content", []))
    for mark in value.get("marks", []):
        if mark.get("type") == "link":
            text = f"[{text}]({mark.get('attrs', {}).get('href', '')})"
    if kind == "mention":
        return str(attrs.get("text") or attrs.get("id") or "")
    if kind in {"inlineCard", "blockCard"}:
        return str(attrs.get("url") or "") + "\n"
    if kind == "hardBreak":
        return "\n"
    if kind == "heading":
        return "#" * min(6, max(1, int(attrs.get("level", 2)))) + " " + text.strip() + "\n\n"
    if kind == "codeBlock":
        return f"```{attrs.get('language', '')}\n{text.strip()}\n```\n\n"
    if kind == "listItem":
        return "- " + text.strip() + "\n"
    if kind in {"tableCell", "tableHeader"}:
        return text.strip().replace("\n", " ") + " | "
    if kind in {"paragraph", "tableRow", "blockquote", "bulletList", "orderedList"}:
        return text.strip() + "\n\n"
    return text


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def link_kind(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname == "github.com":
        for path, kind in (
            ("issues", "GITHUB_ISSUE"),
            ("pull", "GITHUB_PR"),
            ("commit", "GITHUB_COMMIT"),
        ):
            if re.search(rf"/[^/]+/[^/]+/{path}/[^/]+", parsed.path):
                return kind
    return "REMOTE_LINK"


class JiraReader:
    def __init__(self, client):
        self.client = client
        self.settings = client._settings
        self.origin = f"https://api.atlassian.com/ex/jira/{client._cloud_id}/rest/api/3"
        self.counts = Counter()
        self.outcomes: list[dict[str, object]] = []

    async def get(self, path, params=None):
        resource = path.rsplit("/", 1)[-1]
        if resource not in {"field", "jql", "comment", "changelog", "worklog", "remotelink"}:
            resource = "issue"
        began = time.monotonic()
        outcome = "failed"
        try:
            response = await self.client._get(self.origin + path, params)
            response.raise_for_status()
            result = response.json()
            self.counts["requests"] += 1
            outcome = "succeeded"
            return result
        finally:
            record_jira_read(resource, outcome, time.monotonic() - began)

    async def pages(self, path, key):
        offset, result, seen = 0, [], set()
        total_expected = None
        while True:
            payload = await self.get(
                path, {"startAt": offset, "maxResults": self.settings.source_page_size}
            )
            values = payload.get(key)
            total = payload.get("total")
            if not isinstance(values, list) or not isinstance(total, int):
                raise RuntimeError("Invalid Jira subresource pagination")
            if total_expected is not None and total != total_expected:
                raise RuntimeError("Jira subresource changed during pagination; retry required")
            total_expected = total
            for value in values:
                identity = str(value.get("id") or "")
                if not identity or identity in seen:
                    raise RuntimeError("Missing or repeated Jira event identity")
                seen.add(identity)
                result.append(value)
            offset += len(values)
            if offset >= total:
                if offset != total:
                    raise RuntimeError("Jira pagination count mismatch")
                return result
            if not values:
                raise RuntimeError("Jira pagination ended before total")

    async def documents(self, project_id, mapping, updated_since, limit=None, selected_keys=None):
        wanted = set(selected_keys or ())
        found = set()
        if wanted and (limit is None or len(wanted) > limit):
            raise ValueError("Targeted selection requires a bounded canary")
        if mapping.site_url.rstrip("/") != self.client._resource_url:
            raise ValueError("Jira mapping does not match connected Atlassian site")
        field_payload = await self.get("/field")
        if not isinstance(field_payload, list):
            raise RuntimeError("Invalid Jira field catalog")
        names = {str(f["id"]): str(f.get("name") or f["id"]) for f in field_payload}
        clauses = [
            'project = "' + mapping.project_key.replace("\\", "\\\\").replace('"', '\\"') + '"'
        ]
        if updated_since:
            clauses.append(f'updated >= "{updated_since.astimezone(UTC):%Y-%m-%d %H:%M}"')
        params = {
            "jql": " AND ".join(clauses) + " ORDER BY updated ASC, key ASC",
            "maxResults": self.settings.source_page_size,
            "fields": "status,attachment,parent,comment,issuetype" if limit else "id,key",
        }
        tokens, issue_ids, collected = set(), set(), 0
        while True:
            payload = await self.get("/search/jql", params)
            issues = payload.get("issues")
            if not isinstance(issues, list):
                raise RuntimeError("Invalid Jira search response")
            pending = []
            for issue in issues:
                identity = str(issue.get("id") or "")
                if not identity or identity in issue_ids:
                    raise RuntimeError("Missing or repeated Jira issue identity")
                issue_ids.add(identity)
                pending.append(issue)
            if wanted:
                pending = [row for row in pending if row.get("key") in wanted]
                found.update(row["key"] for row in pending)
            if limit is not None:
                # Exercise rich child records before empty epics in a canary.
                # Selection is deterministic and bounded to one search page.
                def richness(row):
                    fields = row.get("fields") or {}
                    return (
                        bool(fields.get("attachment")),
                        bool((fields.get("comment") or {}).get("total")),
                        bool(fields.get("parent")),
                        str(row.get("key") or ""),
                    )

                pending = sorted(pending, key=richness, reverse=True)[: max(0, limit - collected)]
            width = self.settings.jira_issue_concurrency
            for offset in range(0, len(pending), width):
                # One bounded batch provides backpressure; hydrate fully before yielding.
                batches = await asyncio.gather(
                    *(
                        self.issue(project_id, mapping, row, names)
                        for row in pending[offset : offset + width]
                    )
                )
                for batch in batches:
                    collected += 1
                    for document in batch:
                        yield document
            if limit is not None and collected >= limit:
                if wanted - found:
                    raise RuntimeError("Targeted Jira issues were not all found")
                return
            token = payload.get("nextPageToken")
            if not token:
                if payload.get("isLast") is False:
                    raise RuntimeError("Missing Jira next-page token")
                if wanted - found:
                    raise RuntimeError("Targeted Jira issues were not all found")
                return
            if token in tokens:
                raise RuntimeError("Repeated Jira next-page token")
            tokens.add(token)
            params["nextPageToken"] = token

    async def issue(self, project_id, mapping, row, names):
        identity = quote(str(row["id"]), safe="")
        path = f"/issue/{identity}"
        issue = await self.get(path, {"fields": "*all"})
        fields = issue["fields"]
        if fields.get("project", {}).get("key") != mapping.project_key:
            raise RuntimeError("Jira issue outside configured project")
        self.counts["issues_discovered"] += 1
        if fields.get("security"):
            self.counts["issues_restricted"] += 1
            self.outcomes.append(
                {"issue_key": issue["key"], "outcome": "restricted_issue_excluded"}
            )
            return []
        comments = await self.pages(path + "/comment", "comments")
        history = await self.pages(path + "/changelog", "values")
        worklogs = await self.pages(path + "/worklog", "worklogs")
        links = await self.get(path + "/remotelink")
        if not isinstance(links, list):
            raise RuntimeError("Invalid Jira remote links")
        # Detect concurrent edits rather than commit a torn snapshot.
        final = await self.get(path, {"fields": "updated"})
        if final["fields"].get("updated") != fields.get("updated"):
            raise RuntimeError("Jira issue changed during collection; retry required")
        self.counts.update(
            {
                "comments_discovered": len(comments),
                "history_discovered": len(history),
                "worklogs_discovered": len(worklogs),
                "remote_links_discovered": len(links),
            }
        )
        for label, values in (("comments", comments), ("worklogs", worklogs)):
            allowed = []
            for value in values:
                if value.get("visibility"):
                    self.counts[label + "_restricted"] += 1
                    self.outcomes.append(
                        {
                            "issue_key": issue["key"],
                            "event_id": value["id"],
                            "outcome": label + "_restricted_excluded",
                        }
                    )
                else:
                    allowed.append(value)
            if label == "comments":
                comments = allowed
            else:
                worklogs = allowed
        document = issue_document(
            project_id,
            mapping,
            self.client._cloud_id,
            issue,
            names,
            comments,
            history,
            worklogs,
            links,
        )
        documents = [document]
        for attachment in fields.get("attachment") or []:
            self.counts["attachments_discovered"] += 1
            size = attachment.get("size")
            if not isinstance(size, int) or size > self.settings.max_attachment_bytes:
                self.outcomes.append(
                    {
                        "issue_key": issue["key"],
                        "attachment_id": attachment.get("id"),
                        "outcome": "size_limit_or_unknown",
                    }
                )
                self.counts["attachments_excluded"] += 1
                continue
            adjusted = {
                **attachment,
                "content": self.origin
                + "/attachment/content/"
                + quote(str(attachment["id"]), safe=""),
            }
            attachment_doc = await self.client._jira_attachment(
                project_id, mapping, issue, adjusted
            )
            if attachment_doc is None:
                raise RuntimeError("Attachment download exceeded limits or was incomplete")
            from dataclasses import replace

            documents.append(
                replace(
                    attachment_doc,
                    source_id=f"jira:{self.client._cloud_id}:{attachment_doc.source_id}",
                    metadata={
                        **attachment_doc.metadata,
                        "cloud_id": self.client._cloud_id,
                        "labels": document.metadata["labels"],
                        "parent_issue_source_id": document.source_id,
                        "attachment_author": attachment.get("author", {}).get("displayName", ""),
                        "attachment_created": attachment.get("created", ""),
                    },
                )
            )
            self.counts["attachments_downloaded"] += 1
        self.counts["issues_collected"] += 1
        return documents


def current_state_fields(current):
    """Answer-bearing current state, without Jira transport or avatar objects."""
    named = {"issuetype", "status", "resolution", "priority"}
    people = {"assignee", "reporter", "creator"}
    collections = {"components", "versions", "fixVersions"}
    scalar = {"created", "updated", "resolutiondate", "duedate", "labels"}
    result = {}
    for name, value in sorted(current.items()):
        if value is None:
            continue
        if name in named and isinstance(value, dict):
            result[name] = {key: value[key] for key in ("id", "name") if key in value}
        elif name in people and isinstance(value, dict):
            result[name] = {
                key: value[key] for key in ("accountId", "displayName", "active") if key in value
            }
        elif name in collections and isinstance(value, list):
            result[name] = [
                {
                    key: item[key]
                    for key in ("id", "name", "description", "released", "releaseDate")
                    if key in item
                }
                for item in value
                if isinstance(item, dict)
            ]
        elif name == "timetracking" and isinstance(value, dict):
            result[name] = {
                key: value[key]
                for key in (
                    "originalEstimate",
                    "remainingEstimate",
                    "timeSpent",
                    "originalEstimateSeconds",
                    "remainingEstimateSeconds",
                    "timeSpentSeconds",
                )
                if key in value
            }
        elif name in scalar:
            result[name] = value
    return result


def issue_document(project_id, mapping, cloud_id, issue, names, comments, history, worklogs, links):
    fields, key = issue["fields"], issue["key"]
    sections = []

    def section(kind, locator, text, **metadata):
        if text.strip():
            sections.append({"kind": kind, "locator": locator, "text": text.strip(), **metadata})

    current = {
        name: fields.get(name)
        for name in (
            "issuetype",
            "status",
            "resolution",
            "priority",
            "assignee",
            "reporter",
            "creator",
            "created",
            "updated",
            "resolutiondate",
            "duedate",
            "labels",
            "components",
            "versions",
            "fixVersions",
            "timetracking",
        )
    }
    original_current = canonical(current)
    if len(original_current.encode("utf-8")) > 65536:
        raise RuntimeError("Jira current-field provenance exceeds the 64 KiB metadata limit")
    section(
        "CURRENT",
        "current",
        f"{key}: {fields.get('summary', key)}\nCurrent issue fields:\n{canonical(current_state_fields(current))}",
        original_fields=current,
    )
    description = rich_text(fields.get("description")).strip()
    parts = re.split(r"(?m)^(#{1,6} .+)$", description)
    if parts[0].strip():
        section("DESCRIPTION", "description:intro", parts[0])
    for i in range(1, len(parts), 2):
        heading, body = parts[i], parts[i + 1]
        kind = (
            "ACCEPTANCE"
            if re.search(r"acceptance|aceptaci[oó]n", heading, re.I)
            else "REQUIREMENTS"
            if re.search(r"requirement|requisito", heading, re.I)
            else "DESCRIPTION"
        )
        section(kind, f"description:{i}", heading + "\n" + body)
    from app.jira_relationships import relationship_sections

    for locator, projection, original in relationship_sections(fields, canonical):
        if len(canonical(original).encode("utf-8")) > 65536:
            raise RuntimeError("Jira relationship provenance exceeds the 64 KiB metadata limit")
        section("RELATIONSHIP", locator, canonical(projection), original_relationship=original)
    for name, value in sorted(fields.items()):
        if name.startswith("customfield_") and value is not None:
            section("CUSTOM_FIELD", name, f"{names.get(name, name)} ({name}): {canonical(value)}")
    for kind, values in (("COMMENT", comments), ("CHANGELOG", history), ("WORKLOG", worklogs)):
        for event in values:
            event_id = str(event["id"])
            author = event.get("author") or {}
            stamp = event.get("created") or event.get("started") or ""
            body = rich_text(event.get("body") if kind == "COMMENT" else event.get("comment"))
            if kind == "CHANGELOG":
                body = canonical(event.get("items") or [])
            if kind == "WORKLOG":
                body += "\n" + canonical(
                    {k: event.get(k) for k in ("started", "timeSpent", "timeSpentSeconds")}
                )
            section(
                kind,
                f"{kind.lower()}:{event_id}",
                f"{kind} {event_id}; author: {author.get('displayName', '')}; created: {stamp}; updated: {event.get('updated', '')}\n{body}",
                event_id=event_id,
                event_date=stamp,
                event_author=author.get("displayName", ""),
                event_author_id=author.get("accountId", ""),
            )
    for link in links:
        url = str(link.get("object", {}).get("url") or "")
        section(
            "REMOTE_LINK",
            f"remote:{link['id']}",
            canonical(link),
            link_kind=link_kind(url),
            link_url=url,
        )
    # Extract definitions only: no model-generated meanings or guessed translations.
    for index, line in enumerate(description.splitlines()):
        match = re.match(
            r"^([\w][\w /-]{1,50})\s+(?:means|significa|se define como)\s+(.{10,})$",
            line.strip(),
            re.I,
        )
        if match:
            section("GLOSSARY", f"glossary:{index}", line, glossary_term=match[1])
    metadata = {
        "cloud_id": cloud_id,
        "project_key": mapping.project_key,
        "issue_key": key,
        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "priority": (fields.get("priority") or {}).get("name", ""),
        "assignee": (fields.get("assignee") or {}).get("displayName", ""),
        "reporter": (fields.get("reporter") or {}).get("displayName", ""),
        "due_date": fields.get("duedate") or "",
        "labels": fields.get("labels") or [],
        "parent_issue_key": (fields.get("parent") or {}).get("key", ""),
        "components": [v.get("name", "") for v in fields.get("components") or []],
        "issue_updated": fields.get("updated", ""),
        "_jira_sections": sections,
    }
    body = "\n\n".join(f"## {s['kind']} [{s['locator']}]\n{s['text']}" for s in sections)
    version = (
        "jira-v2:"
        + hashlib.sha256(canonical({"fields": current, "sections": sections}).encode()).hexdigest()
    )
    updated = datetime.fromisoformat(fields["updated"].replace("Z", "+00:00"))
    return SourceDocument(
        project_id=project_id,
        provider="JIRA",
        source_id=f"jira:{cloud_id}:issue:{issue['id']}",
        source_type="ISSUE",
        title=fields.get("summary") or key,
        reference=key,
        source_url=f"{mapping.site_url}/browse/{key}",
        version=version,
        content=body,
        updated_at=updated,
        metadata=metadata,
    )
