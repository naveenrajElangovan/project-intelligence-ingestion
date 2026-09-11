"""Compact Jira relationships with exact original values retained as provenance."""


def issue_projection(issue):
    fields = issue.get("fields") or {}
    return {
        name: value
        for name, value in {
            "issue_id": issue.get("id"),
            "issue_key": issue.get("key"),
            "summary": fields.get("summary"),
            "issue_type": (fields.get("issuetype") or {}).get("name"),
            "related_issue_status": (fields.get("status") or {}).get("name"),
            "source_url": issue.get("self"),
        }.items()
        if value is not None
    }


def relationship_sections(fields, canonical):
    parent = fields.get("parent")
    if parent:
        yield "parent", {"relationship": "parent", **issue_projection(parent)}, parent
    for issue in fields.get("subtasks") or []:
        identity = str(issue.get("id") or issue.get("key") or "")
        if not identity:
            raise ValueError("Jira subtask has no stable identity")
        yield "subtask:" + identity, {"relationship": "subtask", **issue_projection(issue)}, issue
    for link in fields.get("issuelinks") or []:
        identity = str(link.get("id") or "")
        if not identity:
            raise ValueError("Jira issue link has no stable identity")
        link_type = link.get("type") or {}
        found = False
        for direction, field in (("inward", "inwardIssue"), ("outward", "outwardIssue")):
            issue = link.get(field)
            if issue:
                found = True
                yield (
                    "link:" + identity + ":" + direction,
                    {
                        "relationship": link_type.get(direction)
                        or link_type.get("name")
                        or direction,
                        "direction": direction,
                        "link_id": identity,
                        **issue_projection(issue),
                    },
                    link,
                )
        if not found:
            # Retain unrecognized shapes visibly rather than silently dropping.
            yield "link:" + identity, link, link
