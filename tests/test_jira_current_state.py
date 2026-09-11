import json
from copy import deepcopy

import pytest

from app.config import Settings
from app.content_security import ContentSecurityScanner, QuarantinedDocument, metadata_text_values
from app.jira import issue_document
from app.projects import JiraMapping


def document(extra=None):
    fields = {
        "summary": "POS ventas / sales",
        "updated": "2026-09-10T10:00:00Z",
        "status": {"id": "1", "name": "En revisión", "iconUrl": "https://icons.invalid/icon"},
        "reporter": {
            "accountId": "author-1",
            "displayName": "María",
            "avatarUrls": {
                "48x48": "https://avatar.invalid/image",
            },
            "self": "https://api.invalid/user",
        },
        "customfield_1": {
            "url": "https://docs.example.org/requirements",
            "text": "Requisito: cerrar caja",
        },
    }
    fields.update(extra or {})
    return issue_document(
        "APP",
        JiraMapping("https://example.atlassian.net", "T0"),
        "cloud",
        {"id": "1", "key": "T0-1", "fields": fields},
        {"customfield_1": "Requisitos"},
        [],
        [],
        [],
        [],
    )


def current(doc):
    return next(s for s in doc.metadata["_jira_sections"] if s["kind"] == "CURRENT")


def test_semantic_current_state_retains_provenance_without_transport_noise():
    doc = document()
    section = current(doc)
    assert "María" in section["text"] and "En revisión" in section["text"]
    assert "author-1" in section["text"]
    assert "avatar.invalid" not in section["text"] and "api.invalid" not in section["text"]
    assert (
        section["original_fields"]["reporter"]["avatarUrls"]["48x48"]
        == "https://avatar.invalid/image"
    )
    assert len(section["text"]) < 1000
    custom = next(s for s in doc.metadata["_jira_sections"] if s["kind"] == "CUSTOM_FIELD")
    assert "https://docs.example.org/requirements" in custom["text"]
    assert "Requisito: cerrar caja" in custom["text"]


def test_current_state_order_is_deterministic():
    original = {"accountId": "1", "displayName": "María", "active": True}
    assert (
        current(document({"reporter": original}))["text"]
        == current(
            document(
                {
                    "reporter": dict(reversed(list(original.items()))),
                }
            )
        )["text"]
    )


def test_current_state_exposes_resolution_and_stable_status_category_metadata():
    doc = document(
        {
            "status": {
                "id": "3",
                "name": "Released",
                "statusCategory": {"id": 3, "key": "done", "name": "Done"},
            },
            "resolution": {"id": "1", "name": "Fixed"},
            "resolutiondate": "2026-09-11T10:00:00Z",
        }
    )

    assert doc.metadata["status"] == "Released"
    assert doc.metadata["status_category"] == "Done"
    assert doc.metadata["status_category_key"] == "done"
    assert doc.metadata["resolution"] == "Fixed"
    assert doc.metadata["resolution_id"] == "1"
    assert current(doc)["original_fields"]["status"]["statusCategory"]["key"] == "done"


def test_oversized_original_metadata_fails_without_truncating():
    with pytest.raises(RuntimeError, match="64 KiB"):
        document({"reporter": {"displayName": "Name", "avatarUrls": {"url": "x" * 65537}}})


def test_retained_metadata_receives_the_same_secret_scan():
    doc = document()
    section = current(doc)
    section["original_fields"]["reporter"]["privateNote"] = "password=abcdefghijklmnop1234567890"
    assert "password=" not in doc.content
    with pytest.raises(QuarantinedDocument):
        ContentSecurityScanner(Settings(_env_file=None)).inspect(doc)


def test_json_metadata_is_scanned_at_value_boundaries():
    values = ["https://docs.example.org/reference", "password=abcdefghijklmnop1234567890"]
    assert list(metadata_text_values(json.dumps(values))) == values
    doc = document()
    doc.metadata["retained"] = json.dumps({"nested": values})
    with pytest.raises(QuarantinedDocument):
        ContentSecurityScanner(Settings(_env_file=None)).inspect(doc)


def test_metadata_depth_is_bounded():
    value = "ordinary"
    for _ in range(34):
        value = [value]
    with pytest.raises(QuarantinedDocument):
        list(metadata_text_values(deepcopy(value)))


def test_metadata_key_value_credentials_are_not_separated_by_serialization():
    doc = document()
    doc.metadata["retained"] = json.dumps({"nested": {"password": "abcdefghijklmnop1234567890"}})
    with pytest.raises(QuarantinedDocument):
        ContentSecurityScanner(Settings(_env_file=None)).inspect(doc)
