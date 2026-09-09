import hashlib
import hmac
import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.api import github as github_api
from app.config import Settings, get_settings
from app.dependencies import get_document_workflow, get_project_reader
from app.models import ChangedFile, DocumentIndexResult
from app.projects import (
    IngestionProject,
    SourceAccessRule,
    VectorStoreRoute,
    ProjectIngestionSchedule,
    RepositoryMapping,
)
from app.main import app


class FakeProjectReader:
    github_merged_pr_enabled = True
    source_access_rules = ()

    async def find_by_repository(self, owner: str, repository: str):
        assert (owner, repository) == ("personal-owner", "private-repo")
        return IngestionProject(
            project_id="POS_BOT",
            display_name="POS/BOT",
            repositories=(
                RepositoryMapping(
                    owner=owner,
                    repository=repository,
                    indexed_branches=("main",),
                    include_paths=("coreApp/**",),
                    exclude_paths=(),
                ),
            ),
            jira_projects=(),
            confluence_spaces=(),
            vector_store=VectorStoreRoute(
                collection_name="project-intelligence",
                text_field="chunk_text",
            ),
            schedule=ProjectIngestionSchedule(
                github_merged_pr_enabled=self.github_merged_pr_enabled
            ),
            source_access_rules=self.source_access_rules,
        )


class FakeWorkflow:
    def __init__(self, fail: bool = False) -> None:
        self.documents = []
        self.rule_arguments = []
        self.fail = fail

    async def run(self, document, scope, scan_id, vector_store, **kwargs):
        if self.fail:
            raise RuntimeError("Chroma unavailable")
        self.documents.append(document)
        self.rule_arguments.append(kwargs)
        return DocumentIndexResult("INDEXED", 1)


def settings() -> Settings:
    return Settings(_env_file=None, github_webhook_secret="secret")


def payload(branch: str = "main") -> dict[str, object]:
    return {
        "action": "closed",
        "number": 9,
        "installation": {"id": 77},
        "repository": {"full_name": "personal-owner/private-repo"},
        "pull_request": {
            "merged": True,
            "merge_commit_sha": "merge-sha",
            "merged_at": datetime.now(UTC).isoformat(),
            "base": {"ref": branch},
        },
    }


def headers(body: bytes) -> dict[str, str]:
    signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery",
        "X-Hub-Signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }


def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


def test_merged_pr_is_resolved_from_backend_and_written_to_chroma(monkeypatch) -> None:
    workflow = FakeWorkflow()
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_project_reader] = FakeProjectReader
    app.dependency_overrides[get_document_workflow] = lambda: workflow

    async def changed_files(self, installation_id, pull_request_number, commit_sha):
        return (
            ChangedFile(
                path="coreApp/Payment.kt",
                status="modified",
                previous_path=None,
                content="class Payment",
                content_hash="hash",
                source_url="https://github.example/file",
            ),
        )

    monkeypatch.setattr(github_api.GitHubAppClient, "pull_request_files", changed_files)
    body = json.dumps(payload()).encode()
    try:
        response = client().post("/v1/webhooks/github", content=body, headers=headers(body))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["projectId"] == "POS_BOT"
    assert response.json()["storedChunks"] == 1
    assert len(workflow.documents) == 1
    assert workflow.documents[0].provider == "GITHUB"


def test_merged_pr_passes_project_access_rules_to_the_workflow(monkeypatch) -> None:
    workflow = FakeWorkflow()
    reader = FakeProjectReader()
    reader.source_access_rules = (
        SourceAccessRule(
            provider="GITHUB",
            match_field="PATH",
            prefix="coreApp/",
            access_policy_id="department:POS_BOT:ENGINEERING",
        ),
    )
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_project_reader] = lambda: reader
    app.dependency_overrides[get_document_workflow] = lambda: workflow

    async def changed_files(self, installation_id, pull_request_number, commit_sha):
        return (
            ChangedFile(
                path="coreApp/Payment.kt",
                status="modified",
                previous_path=None,
                content="class Payment",
                content_hash="hash",
                source_url="https://github.example/file",
            ),
        )

    monkeypatch.setattr(github_api.GitHubAppClient, "pull_request_files", changed_files)
    body = json.dumps(payload()).encode()
    try:
        response = client().post("/v1/webhooks/github", content=body, headers=headers(body))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert workflow.rule_arguments == [
        {"source_access_rules": reader.source_access_rules}
    ]


def test_invalid_signature_is_rejected_before_project_lookup() -> None:
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_project_reader] = FakeProjectReader
    app.dependency_overrides[get_document_workflow] = FakeWorkflow
    body = json.dumps(payload()).encode()
    invalid = headers(body)
    invalid["X-Hub-Signature-256"] = "sha256=invalid"
    try:
        response = client().post("/v1/webhooks/github", content=body, headers=invalid)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401


def test_project_can_disable_merged_pr_ingestion() -> None:
    reader = FakeProjectReader()
    reader.github_merged_pr_enabled = False
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_project_reader] = lambda: reader
    app.dependency_overrides[get_document_workflow] = FakeWorkflow
    body = json.dumps(payload()).encode()
    try:
        response = client().post("/v1/webhooks/github", content=body, headers=headers(body))
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["accepted"] is False


def test_chroma_failure_does_not_commit_manifest(monkeypatch) -> None:
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_project_reader] = FakeProjectReader
    app.dependency_overrides[get_document_workflow] = lambda: FakeWorkflow(fail=True)

    async def changed_files(self, installation_id, pull_request_number, commit_sha):
        return (
            ChangedFile(
                path="coreApp/Payment.kt",
                status="modified",
                previous_path=None,
                content="class Payment",
                content_hash="hash",
                source_url="https://github.example/file",
            ),
        )

    monkeypatch.setattr(github_api.GitHubAppClient, "pull_request_files", changed_files)
    body = json.dumps(payload()).encode()
    try:
        response = client().post("/v1/webhooks/github", content=body, headers=headers(body))
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 502
