"""Provider-neutral, fail-closed source access-policy resolution."""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.models import SourceDocument
from app.projects import SourceAccessRule


_PROVIDERS = {"CONFLUENCE", "JIRA", "GITHUB"}
_MATCH_FIELDS = {"TITLE", "SPACE_KEY", "LABEL", "PATH"}
_DEPARTMENT = re.compile(r"[A-Z0-9_]{2,64}")


def validate_source_access_rules(
    project_id: str, rules: Sequence[SourceAccessRule]
) -> None:
    shared = f"project:{project_id}"
    department_prefix = f"department:{project_id}:"
    for index, rule in enumerate(rules):
        if rule.provider not in _PROVIDERS:
            raise ValueError(f"sourceAccessRules[{index}] has an unsupported provider.")
        if rule.match_field not in _MATCH_FIELDS:
            raise ValueError(f"sourceAccessRules[{index}] has an unsupported matchField.")
        if not rule.prefix:
            raise ValueError(f"sourceAccessRules[{index}] has an empty prefix.")
        department = rule.access_policy_id.removeprefix(department_prefix)
        if rule.access_policy_id != shared and not (
            rule.access_policy_id.startswith(department_prefix)
            and _DEPARTMENT.fullmatch(department)
        ):
            raise ValueError(
                f"sourceAccessRules[{index}] accessPolicyId does not belong to {project_id}."
            )


def resolve_access_policy(
    rules: Sequence[SourceAccessRule],
    document: SourceDocument,
    shared_policy: str,
) -> str:
    """Return the first matching rule; unmatched sources remain project-shared."""

    for rule in rules:
        if rule.provider != document.provider:
            continue
        values = _values(rule.match_field, document)
        if any(value.startswith(rule.prefix) for value in values):
            return rule.access_policy_id
    return shared_policy


def _values(match_field: str, document: SourceDocument) -> tuple[str, ...]:
    if match_field == "TITLE":
        return (document.title,)
    if match_field == "SPACE_KEY":
        return (str(document.metadata.get("space_key") or ""),)
    if match_field == "PATH":
        return (str(document.metadata.get("path") or ""),)
    labels = document.metadata.get("labels")
    if isinstance(labels, (list, tuple, set)):
        return tuple(str(value) for value in labels)
    return (str(labels or ""),)
