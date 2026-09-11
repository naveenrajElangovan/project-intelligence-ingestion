import math
import re

import pytest

from app.config import Settings
from app.content_security import (
    _SECRET_PATTERNS,
    ContentSecurityScanner,
    QuarantinedDocument,
    _entropy_scan_text,
    _has_high_entropy_secret,
)
from app.models import SourceDocument

COMMIT = "0123456789abcdef0123456789abcdef01234567"
URL = (
    f"https://github.com/example/pos-kotlin/blob/{COMMIT}/"
    "app/src/main/java/com/example/tiendas3b/presentation/features/"
    "inventory/transfer/validation/StockTransferConfirmationViewModel.kt#L123"
)
TOKEN = "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0iL3oR6uX9aB3dE6"


def test_immutable_citation_components_do_not_form_a_secret():
    # Confirm the regression fixture triggers the old combined-path heuristic.
    candidates = re.findall(r"\b[A-Za-z0-9+/=_-]{40,200}\b", URL)
    assert any(
        -sum((c.count(x) / len(c)) * math.log2(c.count(x) / len(c)) for x in set(c)) >= 4.7
        for c in candidates
    )
    assert not _has_high_entropy_secret(URL)


@pytest.mark.parametrize(
    "text",
    [
        TOKEN,
        URL + " " + TOKEN,
        URL.replace("StockTransferConfirmationViewModel.kt", TOKEN + ".kt"),
        URL.split("#")[0] + "?token=" + TOKEN,
    ],
)
def test_secret_candidates_remain_flagged(text):
    assert _has_high_entropy_secret(text)


def test_many_citations_cannot_hide_a_later_secret():
    assert _has_high_entropy_secret((URL + "\n") * 101 + TOKEN)


@pytest.mark.parametrize(
    "text",
    [
        URL.replace("github.com", "github.com.attacker.example"),
        URL.replace(COMMIT, "main"),
        URL.replace(COMMIT, COMMIT[:-1]),
        URL.replace("https://github.com", "https://user@github.com"),
        URL.replace("/inventory/", "/inventory%2Ftransfer/"),
        URL.replace("/inventory/", "/%2e%2e/inventory/"),
        URL.replace("/inventory/", "/../inventory/"),
        URL.replace("#L123", "#invalid-fragment"),
        URL.replace("/blob/", "/raw/"),
        URL.replace("/blob/", "/download/"),
    ],
)
def test_unrecognized_urls_are_not_normalized(text):
    assert _entropy_scan_text(text) == text


def test_explicit_patterns_are_checked_on_original_text():
    value = URL + " password=abcdefghijklmnop1234567890"
    assert any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    document = SourceDocument(
        project_id="TEST",
        provider="JIRA",
        source_id="jira:test:issue:1",
        source_type="ISSUE",
        title="TEST-1",
        reference="TEST-1",
        source_url="https://example.atlassian.net/browse/TEST-1",
        version="1",
        content=value,
        updated_at=None,
        metadata={},
    )
    with pytest.raises(QuarantinedDocument):
        ContentSecurityScanner(Settings(_env_file=None)).inspect(document)
    assert document.content == value
