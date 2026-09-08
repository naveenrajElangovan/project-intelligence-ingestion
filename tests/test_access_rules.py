import pytest

from app.access_rules import resolve_access_policy, validate_source_access_rules
from app.models import SourceDocument
from app.projects import SourceAccessRule


def _document(title: str = "Linux POS — sales") -> SourceDocument:
    return SourceDocument(
        project_id="T2.0-STORE",
        provider="CONFLUENCE",
        source_id="page:1",
        source_type="PAGE",
        title=title,
        reference="1",
        source_url="https://example.atlassian.net/wiki/pages/1",
        version="1",
        content="content",
        updated_at=None,
        metadata={"space_key": "StoreUsers", "labels": ("store",)},
    )


def _rule(**changes) -> SourceAccessRule:
    values = {
        "provider": "CONFLUENCE",
        "match_field": "TITLE",
        "prefix": "Linux POS",
        "access_policy_id": "department:T2.0-STORE:STORE_OPERATIONS",
    }
    values.update(changes)
    return SourceAccessRule(**values)


def test_first_matching_rule_selects_department_policy() -> None:
    assert resolve_access_policy(
        (_rule(),), _document(), "project:T2.0-STORE"
    ) == "department:T2.0-STORE:STORE_OPERATIONS"


def test_unmatched_page_remains_project_shared() -> None:
    assert resolve_access_policy(
        (_rule(),), _document("Roles and safe operation"), "project:T2.0-STORE"
    ) == "project:T2.0-STORE"


@pytest.mark.parametrize(
    "rule",
    [
        _rule(provider="UNKNOWN"),
        _rule(match_field="UNKNOWN"),
        _rule(prefix=""),
        _rule(access_policy_id="department:OTHER:STORE_OPERATIONS"),
        _rule(access_policy_id="department:T2.0-STORE:bad-value"),
    ],
)
def test_invalid_rule_configuration_fails_closed(rule: SourceAccessRule) -> None:
    with pytest.raises(ValueError):
        validate_source_access_rules("T2.0-STORE", (rule,))
