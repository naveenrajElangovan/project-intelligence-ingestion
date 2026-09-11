"""Atomic run evidence and fail-closed resume for Jira staging only."""

import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.chroma_collections import project_collection_name
from app.jira_chunk_context import effective_chunker_version
from app.source_references import RepositoryReferences, digest


class RunContractError(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, sort_keys=True, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_contract(settings, project, run_id, phase, selected_keys=()):
    root = Path(__file__).resolve().parents[1]
    source_files = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("app", "scripts")
        for path in sorted((root / folder).rglob("*.py"))
    }
    names = (
        "parser_version",
        "chunker_version",
        "chunk_max_tokens",
        "table_chunk_max_tokens",
        "chunk_overlap_tokens",
        "embedding_position_limit",
        "embedding_dimensions",
        "local_embedding_model",
        "local_embedding_revision",
        "source_page_size",
        "max_attachment_bytes",
        "max_document_failures_per_scope",
        "jira_issue_concurrency",
    )
    configuration = {name: getattr(settings, name) for name in names}
    references = RepositoryReferences(
        settings.jira_reference_manifests, settings.jira_approved_reference_roots
    )
    logical = "jira-stage-" + run_id
    return {
        "contract_version": 1,
        "run_id": run_id,
        "phase": phase,
        "project_id": project.project_id,
        "cloud_id": project.atlassian.cloud_id,
        "mappings": [asdict(mapping) for mapping in project.jira_projects],
        "logical_collection": logical,
        "physical_collection": project_collection_name(logical, project.project_id),
        "production_collection": project.vector_store.collection_name,
        "schema_version": project.vector_store.schema_version,
        "parser_version": settings.parser_version,
        "chunker_version": effective_chunker_version(settings.chunker_version, "JIRA"),
        "embedding_model": project.vector_store.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "reference_manifest_hash": references.fingerprint,
        "source_code_hash": digest(source_files),
        "configuration_hash": digest(configuration),
        "access_rules_hash": digest([asdict(rule) for rule in project.source_access_rules]),
        "vector_route_hash": digest(asdict(project.vector_store)),
        "selected_issue_keys": sorted(selected_keys),
    }


class OutcomeLedger:
    def __init__(self, path, contract, *, resume=False):
        self.path, self.contract = Path(path).resolve(), contract
        if resume:
            if not self.path.is_file():
                raise RunContractError("Resume requires the original durable outcome ledger")
            data = json.loads(self.path.read_text())
            state_hash = data.pop("state_hash", None)
            if state_hash != digest(data) or data.get("contract") != contract:
                raise RunContractError("Resume ledger contract/hash does not match")
            self.data = data
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                reservation = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(reservation)
            except FileExistsError as error:
                raise RunContractError(
                    "An outcome ledger already exists; resume must be explicit"
                ) from error
            self.data = {"contract": contract, "events": []}
            self._save(self.data)

    def _save(self, value):
        atomic_json(self.path, {**value, "state_hash": digest(value)})

    def latest(self, source_id):
        return next(
            (row for row in reversed(self.data["events"]) if row["source_id"] == source_id), None
        )

    def record(self, document, attempt_id, status, **details):
        key = str(document.metadata.get("issue_key") or "")
        row = {
            "sequence": len(self.data["events"]) + 1,
            "attempt_id": attempt_id,
            "configuration_hash": self.contract["configuration_hash"],
            "source_code_hash": self.contract["source_code_hash"],
            "source_id": document.source_id,
            "source_key": key if re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", key) else "",
            "source_version": document.version,
            "source_hash": document.content_hash,
            "status": status,
            "recorded_at": datetime.now(UTC).isoformat(),
            **details,
        }
        candidate = {**self.data, "events": [*self.data["events"], row]}
        self._save(candidate)
        self.data = candidate

    def summary(self):
        latest = {row["source_id"]: row for row in self.data["events"]}
        return {
            "ledger_path": str(self.path),
            "ledger_hash": digest(self.data),
            "sources_started": len(latest),
            "sources_succeeded": sum(row["status"] == "SUCCEEDED" for row in latest.values()),
            "failures": [row for row in latest.values() if row["status"] == "FAILED"],
            "unfinished_sources": [
                row["source_id"] for row in latest.values() if row["status"] == "STARTED"
            ],
        }


async def source_integrity(manifests, collection, contract, document, scope):
    manifest = await manifests.get_manifest(document.project_id, "JIRA", scope, document.source_id)
    if (
        manifest is None
        or manifest.deleted
        or manifest.version != document.version
        or manifest.content_hash != document.content_hash
        or manifest.parser_version != contract["parser_version"]
        or manifest.chunker_version != contract["chunker_version"]
    ):
        raise RunContractError("Source manifest does not match the pinned source revision")

    def read():
        page = collection.get(
            where={
                "$and": [
                    {"project_id": document.project_id},
                    {"provider": "JIRA"},
                    {"source_id": document.source_id},
                ]
            },
            include=["documents", "metadatas", "embeddings"],
        )
        if len(page["ids"]) != manifest.chunk_count or not page["ids"]:
            raise RunContractError("Stored source chunk inventory does not match its manifest")
        rows = []
        for identity, text, metadata, embedding in zip(
            page["ids"], page["documents"], page["metadatas"], page["embeddings"], strict=True
        ):
            vector = [float(value) for value in embedding]
            if (
                len(vector) != contract["embedding_dimensions"]
                or not all(math.isfinite(value) for value in vector)
                or metadata.get("source_version") != document.version
                or metadata.get("schema_version") != contract["schema_version"]
                or metadata.get("embedding_model") != contract["embedding_model"]
            ):
                raise RunContractError("Stored source schema/model/revision is incompatible")
            rows.append((identity, text, metadata, vector))
        return {
            "manifest_hash": digest(asdict(manifest)),
            "vector_hash": digest(sorted(rows)),
            "chunk_count": len(rows),
        }

    return await asyncio.to_thread(read)


class LedgerWorkflow:
    def __init__(self, workflow, ledger, attempt_id, integrity, *, resume=False):
        self.workflow, self.ledger = workflow, ledger
        self.attempt_id, self.integrity, self.resume = attempt_id, integrity, resume

    def __getattr__(self, name):
        return getattr(self.workflow, name)

    async def run(self, document, scope, scan_id, route, **kwargs):
        began = time.monotonic()
        previous = self.ledger.latest(document.source_id)
        verified = False
        result = None
        try:
            if self.resume and previous:
                if (
                    previous["source_version"] != document.version
                    or previous["source_hash"] != document.content_hash
                ):
                    raise RunContractError(
                        "Source revision changed; a fresh staging run is required"
                    )
                if previous["status"] == "SUCCEEDED":
                    actual = await self.integrity(document, scope)
                    if actual != previous["integrity"]:
                        raise RunContractError("Resume source manifest or vector hashes changed")
                    verified = True
            self.ledger.record(document, self.attempt_id, "STARTED", reason_code="PROCESSING")
            # A bare manifest is not enough to skip a source after a crash.
            result = await self.workflow.run(
                document, scope, scan_id, route, **{**kwargs, "force": not verified}
            )
            actual = await self.integrity(document, scope)
            self.ledger.record(
                document,
                self.attempt_id,
                "SUCCEEDED",
                reason_code=result.operation,
                integrity=actual,
                counts={"chunks_written": result.chunks_written},
                duration_seconds=round(time.monotonic() - began, 6),
            )
            return result
        except Exception as error:
            reason = getattr(getattr(error, "reason", None), "code", None) or type(error).__name__
            reason = reason if re.fullmatch(r"[A-Za-z0-9_]{1,100}", reason) else "SOURCE_FAILURE"
            self.ledger.record(
                document,
                self.attempt_id,
                "FAILED",
                reason_code=reason,
                counts={"chunks_written": result.chunks_written if result else None},
                duration_seconds=round(time.monotonic() - began, 6),
            )
            raise
