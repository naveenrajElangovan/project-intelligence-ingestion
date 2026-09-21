import json
from types import SimpleNamespace

import pytest

from app.chroma_collections import project_collection_metadata, project_collection_name
from app.jira_run import RunContractError
from app.source_references import digest
from scripts.run_jira_ingestion import clone_non_jira


@pytest.mark.parametrize(
    "mismatch", [None, "project", "site", "run", "selection", "baseline", "production"]
)
def test_resume_requires_exact_project_site_run_selection_and_baseline(monkeypatch, mismatch):
    project = SimpleNamespace(
        project_id="GENERIC", vector_store=SimpleNamespace(collection_name="production")
    )
    contract = {
        "run_id": "sample-run",
        "project_id": "GENERIC",
        "site": "https://one.atlassian.net",
        "selected_issue_keys": ["OPS7-1"],
    }
    target = "jira-stage-sample-run"
    stored = dict(contract)
    if mismatch in {"project", "site", "run", "selection"}:
        field = {
            "project": "project_id",
            "site": "site",
            "run": "run_id",
            "selection": "selected_issue_keys",
        }[mismatch]
        stored[field] = "different"
    metadata = {
        **project_collection_metadata(target, "GENERIC"),
        "jira_run_contract": json.dumps(stored, sort_keys=True),
        "jira_baseline_hash": "different" if mismatch == "baseline" else digest([]),
    }
    collection = SimpleNamespace(
        metadata=metadata, get=lambda **kwargs: {"ids": [], "documents": [], "metadatas": []}
    )
    physical = project_collection_name(target, "GENERIC")
    collection.name = physical

    class Client:
        def list_collections(self):
            return [physical]

        def get_collection(self, name):
            assert name == physical
            return collection

        def create_collection(self, *args, **kwargs):
            raise AssertionError("Resume must never create a replacement collection")

    monkeypatch.setattr("chromadb.HttpClient", lambda **kwargs: Client())
    settings = SimpleNamespace(chroma_host="unused", chroma_port=8000)
    if mismatch:
        with pytest.raises(RunContractError):
            clone_non_jira(
                settings,
                project,
                "production" if mismatch == "production" else target,
                contract,
                resume=True,
            )
    else:
        assert clone_non_jira(settings, project, target, contract, resume=True) == (0, collection)
