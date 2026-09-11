import json
from copy import deepcopy

import pytest

from app.config import Settings
from app.content_security import ContentSecurityScanner, QuarantinedDocument
from app.jira_relationships import relationship_sections
from tests.test_jira_current_state import document


def test_relationship_keeps_identity_and_meaning_together_and_original_intact():
    parent = {
        "id": "52",
        "key": "OPS-88",
        "fields": {
            "summary": "Cierre / Closure",
            "issuetype": {"id": "10011", "name": "Epic", "iconUrl": "https://icons.example/type"},
            "status": {"id": "9", "name": "Awaiting review"},
        },
    }
    link = {
        "id": "42",
        "outwardIssue": parent,
        "type": {"outward": "blocks", "inward": "is blocked by"},
    }
    d = document({"parent": deepcopy(parent), "issuelinks": [deepcopy(link)]})
    s = [x for x in d.metadata["_jira_sections"] if x["kind"] == "RELATIONSHIP"]
    assert len(s) == 2
    text = s[0]["text"]
    assert all(value in text for value in ["OPS-88", "Closure", "Epic", "Awaiting review"])
    assert "iconUrl" not in text and "10011" not in text
    assert s[0]["original_relationship"] == parent
    assert json.loads(s[1]["text"])["relationship"] == "blocks"
    assert s[1]["locator"] == "link:42:outward"
    assert s[1]["original_relationship"] == link


def test_relationship_provenance_still_receives_security_scan():
    d = document(
        {
            "parent": {
                "id": "1",
                "key": "OTHER-2",
                "fields": {"privateNote": "password=abcdefghijklmnop1234567890"},
            }
        }
    )
    with pytest.raises(QuarantinedDocument):
        ContentSecurityScanner(Settings(_env_file=None)).inspect(d)


def test_missing_link_identity_is_explicit_failure():
    with pytest.raises(ValueError, match="stable identity"):
        list(relationship_sections({"issuelinks": [{"type": {"name": "Relates"}}]}, json.dumps))
