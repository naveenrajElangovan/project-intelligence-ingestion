import asyncio
import json
from dataclasses import replace

import pytest

from app.jira_run import LedgerWorkflow, OutcomeLedger, RunContractError, source_integrity
from app.models import DocumentIndexResult, SourceDocument
from app.state import SourceManifest

CONTRACT = {
    "run_id": "sample-run",
    "configuration_hash": "config",
    "source_code_hash": "code",
    "reference_manifest_hash": "references",
    "schema_version": "3",
    "embedding_model": "model",
    "embedding_dimensions": 1024,
    "parser_version": "parser-test",
    "chunker_version": "test.jira-context-v1",
}


def document(version="v1"):
    return SourceDocument(
        project_id="APP",
        provider="JIRA",
        source_id="jira:cloud:issue:1",
        source_type="ISSUE",
        title="T0-1",
        reference="T0-1",
        source_url="https://example.atlassian.net/browse/T0-1",
        version=version,
        content="ordinary source fact",
        updated_at=None,
        metadata={"issue_key": "T0-1"},
    )


def test_atomic_failure_preserves_previous_ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    ledger = OutcomeLedger(path, CONTRACT)
    ledger.record(document(), "a", "STARTED")
    before = path.read_bytes()

    def fail(*_):
        raise OSError("disk failure")

    monkeypatch.setattr("app.jira_run.os.replace", fail)
    with pytest.raises(OSError):
        ledger.record(document(), "a", "SUCCEEDED")
    assert path.read_bytes() == before
    assert (
        OutcomeLedger(path, CONTRACT, resume=True).latest(document().source_id)["status"]
        == "STARTED"
    )


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "configuration_hash",
        "source_code_hash",
        "reference_manifest_hash",
        "schema_version",
        "embedding_model",
        "embedding_dimensions",
        "parser_version",
        "chunker_version",
    ],
)
def test_resume_contract_mismatches_fail_closed(tmp_path, field):
    path = tmp_path / "ledger.json"
    OutcomeLedger(path, CONTRACT)
    with pytest.raises(RunContractError):
        OutcomeLedger(path, {**CONTRACT, field: "different"}, resume=True)


def test_missing_corrupted_and_nonexplicit_resume_are_rejected(tmp_path):
    path = tmp_path / "ledger.json"
    with pytest.raises(RunContractError):
        OutcomeLedger(path, CONTRACT, resume=True)
    OutcomeLedger(path, CONTRACT)
    with pytest.raises(RunContractError):
        OutcomeLedger(path, CONTRACT)
    data = json.loads(path.read_text())
    data["events"].append({"status": "forged"})
    path.write_text(json.dumps(data))
    with pytest.raises(RunContractError):
        OutcomeLedger(path, CONTRACT, resume=True)


def test_verified_resume_skips_only_matching_success_and_rejects_changed_vectors(tmp_path):
    async def run():
        state = {"manifest_hash": "m", "vector_hash": "v", "chunk_count": 1}

        class Workflow:
            def __init__(self):
                self.forces = []

            async def run(self, *_, force, **__):
                self.forces.append(force)
                return DocumentIndexResult("INDEXED" if force else "UNCHANGED", int(force))

        async def integrity(*_):
            return dict(state)

        underlying = Workflow()
        path = tmp_path / "ledger.json"
        await LedgerWorkflow(underlying, OutcomeLedger(path, CONTRACT), "a", integrity).run(
            document(), "scope", "scan", None
        )
        resumed = LedgerWorkflow(
            underlying, OutcomeLedger(path, CONTRACT, resume=True), "b", integrity, resume=True
        )
        assert (await resumed.run(document(), "scope", "scan2", None)).operation == "UNCHANGED"
        assert underlying.forces == [True, False]
        state["vector_hash"] = "tampered"
        with pytest.raises(RunContractError):
            await resumed.run(document(), "scope", "scan3", None)
        assert underlying.forces == [True, False]

    asyncio.run(run())


def test_interrupted_source_reindexes_and_failure_text_is_not_persisted(tmp_path):
    async def run():
        path = tmp_path / "ledger.json"
        ledger = OutcomeLedger(path, CONTRACT)
        ledger.record(document(), "crashed", "STARTED")

        class Workflow:
            async def run(self, *_, force, **__):
                assert force is True
                raise ValueError("SECRET-BEARING ERROR TEXT")

        async def integrity(*_):
            raise AssertionError("Unverified source must not be skipped")

        wrapper = LedgerWorkflow(
            Workflow(), OutcomeLedger(path, CONTRACT, resume=True), "retry", integrity, resume=True
        )
        with pytest.raises(ValueError):
            await wrapper.run(document(), "scope", "scan", None)
        assert "SECRET-BEARING" not in path.read_text()
        failed = wrapper.ledger.summary()["failures"][0]
        assert failed["source_key"] == "T0-1" and failed["reason_code"] == "ValueError"
        assert failed["counts"]["chunks_written"] is None
        with pytest.raises(RunContractError):
            await wrapper.run(document("changed-version"), "scope", "scan2", None)

    asyncio.run(run())


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "deleted",
        "version",
        "hash",
        "count",
        "schema",
        "model",
        "dimension",
        "nonfinite",
        "parser",
        "chunker",
    ],
)
def test_source_integrity_checks_persisted_manifest_and_vectors(defect):
    doc = document()
    manifest = SourceManifest(
        doc.project_id,
        "JIRA",
        "scope",
        doc.source_id,
        "ISSUE",
        doc.title,
        doc.source_url,
        doc.version,
        doc.content_hash,
        1,
        "scan",
        parser_version=CONTRACT["parser_version"],
        chunker_version=CONTRACT["chunker_version"],
    )
    if defect in {"deleted", "version", "hash", "count", "parser", "chunker"}:
        fields = {
            "deleted": {"deleted": True},
            "version": {"version": "wrong"},
            "hash": {"content_hash": "wrong"},
            "count": {"chunk_count": 2},
            "parser": {"parser_version": "old"},
            "chunker": {"chunker_version": "old"},
        }
        manifest = replace(manifest, **fields[defect])

    class Manifests:
        async def get_manifest(self, project, provider, scope, source):
            assert (project, provider, scope, source) == ("APP", "JIRA", "scope", doc.source_id)
            return manifest

    class Collection:
        def get(self, *, where, include):
            assert where["$and"] == [
                {"project_id": "APP"},
                {"provider": "JIRA"},
                {"source_id": doc.source_id},
            ]
            metadata = {
                "source_version": doc.version,
                "schema_version": "3",
                "embedding_model": "model",
            }
            if defect == "schema":
                metadata["schema_version"] = "other"
            if defect == "model":
                metadata["embedding_model"] = "other"
            vector = [0.1] * (1 if defect == "dimension" else 1024)
            if defect == "nonfinite":
                vector[0] = float("nan")
            return {
                "ids": ["chunk"],
                "documents": ["fact"],
                "metadatas": [metadata],
                "embeddings": [vector],
            }

    async def run():
        return await source_integrity(Manifests(), Collection(), CONTRACT, doc, "scope")

    if defect:
        with pytest.raises(RunContractError):
            asyncio.run(run())
    else:
        assert asyncio.run(run())["chunk_count"] == 1
