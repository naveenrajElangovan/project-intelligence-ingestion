import json

import pytest

from app.config import Settings
from app.content_security import ContentSecurityScanner, QuarantinedDocument
from app.models import SourceDocument
from app.source_references import (
    ReferenceValidationError,
    RepositoryReferences,
    build_reference_manifest,
    digest,
)


@pytest.fixture
def pinned(tmp_path):
    root = tmp_path / "repo"
    file = (
        root
        / "docs"
        / "source-materials"
        / "es"
        / "orders"
        / "DOC-2048-stock-movement-reconciliation.md"
    )
    file.parent.mkdir(parents=True)
    file.write_text("# Reconciliación de inventario\nSource-backed requirements.\n")
    manifest = build_reference_manifest(root, "APP", "example-repository", [file])
    path = tmp_path / "references.json"
    path.write_text(json.dumps(manifest))
    reference = "example-repository/" + file.relative_to(root).as_posix()
    return root, file, path, reference


def doc(content):
    return SourceDocument(
        project_id="APP",
        provider="JIRA",
        source_id="jira:cloud:issue:1",
        source_type="ISSUE",
        title="T0-1",
        reference="T0-1",
        source_url="https://example.atlassian.net/browse/T0-1",
        version="v1",
        content=content,
        updated_at=None,
        metadata={},
    )


def test_complete_markdown_and_plain_references_preserve_original_content(pinned):
    root, _, path, reference = pinned
    scanner = ContentSecurityScanner(
        Settings(
            _env_file=None,
            jira_reference_manifests=(str(path),),
            jira_approved_reference_roots=(str(root),),
        )
    )
    for value in [reference, f"Source: {reference}.", f"[Fuente]({reference})", f"`{reference}`"]:
        document = doc(value)
        assert scanner.inspect(document) is False
        assert document.content == value


@pytest.mark.parametrize(
    "tail",
    [
        "docs/missing.md",
        "docs/../secret.md",
        "docs/%2e%2e/secret.md",
        "docs/a%2Fb.md",
        "docs/file.md?token=secret",
        "docs/file.md#unverified",
        "docs/a\x00b.md",
        "docs/a\\b.md",
        "docs/user@host.md",
    ],
)
def test_invalid_complete_values_fail_closed(pinned, tail):
    root, _, path, _ = pinned
    resolver = RepositoryReferences([path], [root])
    with pytest.raises(ReferenceValidationError):
        resolver.normalize("example-repository/" + tail, "APP")


def test_absolute_userinfo_and_unapproved_alias_are_never_exempted(pinned):
    root, _, path, reference = pinned
    resolver = RepositoryReferences([path], [root])
    for value in [
        "/" + reference,
        "https://user@" + reference,
        reference.replace("example-repository", "unapproved-root"),
    ]:
        assert resolver.normalize(value, "APP") == value
    with pytest.raises(ReferenceValidationError):
        RepositoryReferences([path], [root.parent])


def test_missing_changed_and_symlink_escape_targets_are_rejected(pinned, tmp_path):
    root, file, path, reference = pinned
    resolver = RepositoryReferences([path], [root])
    original = file.read_bytes()
    file.write_text("Changed after pinning")
    with pytest.raises(ReferenceValidationError):
        resolver.normalize(reference, "APP")
    file.unlink()
    with pytest.raises(ReferenceValidationError):
        resolver.normalize(reference, "APP")
    outside = tmp_path / "outside.md"
    outside.write_bytes(original)
    file.symlink_to(outside)
    with pytest.raises(ReferenceValidationError):
        resolver.normalize(reference, "APP")


def test_manifest_revision_hash_and_project_are_enforced(pinned):
    root, _, path, reference = pinned
    resolver = RepositoryReferences([path], [root])
    with pytest.raises(ReferenceValidationError):
        resolver.normalize(reference, "OTHER")
    value = json.loads(path.read_text())
    value["source_revision"] = "sha256:wrong"
    value.pop("manifest_hash")
    value["manifest_hash"] = digest(value)
    path.write_text(json.dumps(value))
    with pytest.raises(ReferenceValidationError):
        RepositoryReferences([path], [root])


def test_explicit_and_high_entropy_credentials_are_still_scanned(pinned):
    root, file, path, reference = pinned
    scanner = ContentSecurityScanner(
        Settings(
            _env_file=None,
            jira_reference_manifests=(str(path),),
            jira_approved_reference_roots=(str(root),),
        )
    )
    with pytest.raises(QuarantinedDocument):
        scanner.inspect(doc(reference + " password=abcdefghijklmnop1234567890"))
    token = "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0iL3oR6uX9aB3dE6"
    secret_file = file.parent / (token + ".md")
    secret_file.write_text("No credential in the body")
    manifest = build_reference_manifest(root, "APP", "example-repository", [file, secret_file])
    path.write_text(json.dumps(manifest))
    scanner = ContentSecurityScanner(
        Settings(
            _env_file=None,
            jira_reference_manifests=(str(path),),
            jira_approved_reference_roots=(str(root),),
        )
    )
    with pytest.raises(QuarantinedDocument):
        scanner.inspect(doc("example-repository/" + secret_file.relative_to(root).as_posix()))
