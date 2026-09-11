"""Jira-only staged ingestion with durable, content-safe completeness reports.

No invocation promotes a collection. Production promotion requires a separately
verified evaluation report; an indexed collection alone is not a quality pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.atlassian import AtlassianSourceClient
from app.chroma_collections import (
    project_collection_metadata,
    project_collection_name,
    verify_project_collection,
)
from app.config import get_settings
from app.control_plane import BackendControlPlaneClient
from app.embedding import build_passage_embedder
from app.jira import JiraReader
from app.jira_run import (
    LedgerWorkflow,
    OutcomeLedger,
    RunContractError,
    atomic_json,
    run_contract,
    source_integrity,
)
from app.service import IngestionService
from app.source_references import digest
from app.state import AzureTableManifestStore
from app.telemetry import push_metrics, record_jira_inventory
from app.vector import ChromaVectorStore
from app.workflow import DocumentIngestionWorkflow


def revision():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


async def preflight_state_store(manifests, project_id, run_id):
    """Prove checkpoint read/write access before allocating or copying vectors."""
    scope = f"staging:{run_id}|credential-preflight"
    owner = uuid4().hex
    acquired = await manifests.acquire_scope_lease(project_id, "JIRA", scope, owner, 120)
    if not acquired:
        raise RuntimeError("Jira state-store preflight is already owned by another run")
    await manifests.release_scope_lease(project_id, "JIRA", scope, owner)


def non_jira_fingerprint(collection, project_id):
    rows, offset = [], 0
    while True:
        page = collection.get(
            where={"project_id": project_id},
            include=["documents", "metadatas"],
            limit=100,
            offset=offset,
        )
        rows.extend(
            (identity, digest((text, metadata)))
            for identity, text, metadata in zip(
                page["ids"], page["documents"], page["metadatas"], strict=True
            )
            if metadata.get("provider") != "JIRA"
            and metadata.get("record_kind") != "__vocabulary__"
        )
        if len(page["ids"]) < 100:
            return digest(sorted(rows)), len(rows)
        offset += len(page["ids"])


def clone_non_jira(settings, project, target, contract, *, resume=False):
    from chromadb import HttpClient

    client = HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    name = project_collection_name(project.vector_store.collection_name, project.project_id)
    names = {getattr(c, "name", str(c)) for c in client.list_collections()}
    source = client.get_collection(name) if name in names else None
    if source is not None:
        verify_project_collection(source, project.vector_store.collection_name, project.project_id)
    baseline, baseline_count = (
        non_jira_fingerprint(source, project.project_id) if source else (digest([]), 0)
    )
    physical = project_collection_name(target, project.project_id)
    if (
        target != "jira-stage-" + contract["run_id"]
        or target == project.vector_store.collection_name
    ):
        raise RunContractError("Staging target must never be the production route")
    if resume:
        if physical not in names:
            raise RunContractError("The original staging collection is missing")
        destination = client.get_collection(physical)
        verify_project_collection(destination, target, project.project_id)
        if (
            (destination.metadata or {}).get("jira_run_contract")
            != json.dumps(contract, sort_keys=True)
            or (destination.metadata or {}).get("jira_baseline_hash") != baseline
            or non_jira_fingerprint(destination, project.project_id)[0] != baseline
        ):
            raise RunContractError("Staging contract or preserved non-Jira baseline does not match")
        return baseline_count, destination
    if physical in names:
        raise RunContractError("Staging already exists; resume must be explicit")
    destination = client.create_collection(
        physical,
        metadata={
            **project_collection_metadata(target, project.project_id),
            "jira_run_contract": json.dumps(contract, sort_keys=True),
            "jira_baseline_hash": baseline,
        },
    )
    verify_project_collection(destination, target, project.project_id)
    if source is None:
        return 0, destination
    offset = copied = 0
    while True:
        page = source.get(
            where={"project_id": project.project_id},
            include=["documents", "metadatas", "embeddings"],
            limit=100,
            offset=offset,
        )
        selected = [
            i
            for i, m in enumerate(page["metadatas"])
            if m.get("provider") != "JIRA" and m.get("record_kind") != "__vocabulary__"
        ]
        if selected:
            destination.upsert(
                ids=[page["ids"][i] for i in selected],
                documents=[page["documents"][i] for i in selected],
                metadatas=[page["metadatas"][i] for i in selected],
                embeddings=[page["embeddings"][i] for i in selected],
            )
            copied += len(selected)
        if len(page["ids"]) < 100:
            if (
                non_jira_fingerprint(destination, project.project_id)[0] != baseline
                or non_jira_fingerprint(source, project.project_id)[0] != baseline
            ):
                raise RunContractError("Non-Jira source changed while staging was copied")
            return copied, destination
        offset += len(page["ids"])


async def execute(args):
    settings = get_settings()
    settings = settings.model_copy(
        update={
            "jira_reference_manifests": tuple(
                args.reference_manifest or settings.jira_reference_manifests
            ),
            "jira_approved_reference_roots": tuple(
                args.approved_reference_root or settings.jira_approved_reference_roots
            ),
        }
    )
    gateway = BackendControlPlaneClient(settings)
    report = {
        "run_id": args.run_id,
        "attempt_id": uuid4().hex,
        "resumed": args.resume_stage,
        "phase": args.phase,
        "started_at": datetime.now(UTC).isoformat(),
        "code_revision": revision(),
        "production_modified": False,
        "projects": [],
        "quality_verified": False,
        "completeness": False,
        "completeness_note": "Requires full source-versus-index reconciliation; bounded samples cannot establish completeness.",
        "selected_issue_keys": list(args.issue_key),
    }
    began = time.monotonic()
    try:
        projects = await gateway.list_jira_projects()
        if args.project:
            projects = tuple(p for p in projects if p.project_id == args.project)
        if not projects:
            raise RuntimeError("No matching active Jira project mappings")
        semaphore = asyncio.Semaphore(settings.jira_project_concurrency)

        async def run(project):
            async with semaphore:
                row = {
                    "project_id": project.project_id,
                    "mappings": [asdict(m) for m in project.jira_projects],
                    "status": "pending",
                    "scopes": [],
                }
                report["projects"].append(row)
                ledger = None
                try:
                    if not project.atlassian:
                        raise RuntimeError("Atlassian connection missing")
                    row["cloud_id"] = project.atlassian.cloud_id
                    if any(
                        m.site_url.rstrip("/") != project.atlassian.resource_url.rstrip("/")
                        for m in project.jira_projects
                    ):
                        raise RuntimeError("Jira mappings and connected cloud do not match")
                    if any(
                        key.rsplit("-", 1)[0] not in {m.project_key for m in project.jira_projects}
                        for key in args.issue_key
                    ):
                        raise RunContractError(
                            "Targeted issue key is outside the configured Jira mappings"
                        )
                    if args.phase == "preflight":
                        client = AtlassianSourceClient(
                            settings,
                            gateway,
                            project.project_id,
                            project.atlassian.cloud_id,
                            project.atlassian.resource_url,
                        )
                        for mapping in project.jira_projects:
                            reader = JiraReader(client)
                            docs = [
                                d
                                async for d in reader.documents(
                                    project.project_id, mapping, None, limit=1
                                )
                            ]
                            row["scopes"].append(
                                {
                                    "project_key": mapping.project_key,
                                    "source_counts": dict(reader.counts),
                                    "outcomes": reader.outcomes,
                                    "sample_documents": len(docs),
                                }
                            )
                        row["status"] = "source_read_verified"
                        return
                    target = "jira-stage-" + args.run_id
                    row["target_collection"] = target
                    row["rollback_collection"] = project.vector_store.collection_name
                    contract = run_contract(
                        settings, project, args.run_id, args.phase, args.issue_key
                    )
                    row["contract"] = contract
                    staged_settings = settings.model_copy(update={"chroma_collection": target})
                    manifests = AzureTableManifestStore.from_settings(staged_settings)
                    row["state_preflight"] = "started"
                    await preflight_state_store(manifests, project.project_id, args.run_id)
                    row["state_preflight"] = "passed"
                    state_directory = args.state_dir or args.report.parent / ".jira-runs"
                    ledger = OutcomeLedger(
                        state_directory / f"{args.run_id}-{digest(project.project_id)[:12]}.json",
                        contract,
                        resume=args.resume_stage,
                    )
                    copied, collection = await asyncio.to_thread(
                        clone_non_jira,
                        settings,
                        project,
                        target,
                        contract,
                        resume=args.resume_stage,
                    )
                    row["preserved_non_jira_chunks"] = copied
                    route = replace(project.vector_store, collection_name=target)
                    staged_project = replace(project, vector_store=route)
                    vectors = ChromaVectorStore(
                        settings.chroma_host,
                        settings.chroma_port,
                        target,
                        build_passage_embedder(staged_settings),
                    )
                    workflow = DocumentIngestionWorkflow(staged_settings, manifests, vectors)

                    async def integrity(document, scope):
                        return await source_integrity(
                            manifests, collection, contract, document, scope
                        )

                    workflow = LedgerWorkflow(
                        workflow, ledger, report["attempt_id"], integrity, resume=args.resume_stage
                    )
                    service = IngestionService(staged_settings, gateway, manifests, workflow)
                    client = AtlassianSourceClient(
                        settings,
                        gateway,
                        project.project_id,
                        project.atlassian.cloud_id,
                        project.atlassian.resource_url,
                    )
                    for mapping in project.jira_projects:
                        selected_keys = [
                            key
                            for key in args.issue_key
                            if key.rsplit("-", 1)[0] == mapping.project_key
                        ]
                        if args.issue_key and not selected_keys:
                            continue
                        reader = JiraReader(client)
                        scope_row = {"project_key": mapping.project_key}
                        row["scopes"].append(scope_row)
                        scope = f"staging:{args.run_id}|{mapping.site_url}|{mapping.project_key}"
                        try:
                            result = await service._run_scope(
                                staged_project,
                                "JIRA",
                                scope,
                                reader.documents(
                                    project.project_id,
                                    mapping,
                                    None,
                                    limit=10 if args.phase == "canary" else None,
                                    selected_keys=selected_keys or None,
                                ),
                                False,
                            )
                            scope_row["index_counts"] = asdict(result)
                            if result.failed:
                                raise RuntimeError("One or more Jira documents failed indexing")
                        finally:
                            scope_row["source_counts"] = dict(reader.counts)
                            scope_row["outcomes"] = reader.outcomes
                    row["status"] = "indexed_evaluation_pending"
                except Exception as error:
                    row["status"] = "blocked"
                    row["error_type"] = type(error).__name__
                    response = getattr(error, "response", None)
                    if response is not None:
                        row["http_status"] = response.status_code
                    # Exception messages from HTTP clients may contain request
                    # URLs or credentials. Only locally authored failures are safe.
                    if type(error) in {RuntimeError, ValueError} or isinstance(
                        error, RunContractError
                    ):
                        row["reason"] = str(error)[:250]
                finally:
                    if ledger is not None:
                        row["outcome_ledger"] = ledger.summary()

        await asyncio.gather(*(run(p) for p in projects))
    except Exception as error:
        report["status"] = "blocked"
        report["error_type"] = type(error).__name__
        response = getattr(error, "response", None)
        if response is not None:
            report["http_status"] = response.status_code
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["duration_seconds"] = round(time.monotonic() - began, 3)
    report["status"] = report.get("status") or (
        "blocked" if any(p["status"] == "blocked" for p in report["projects"]) else "completed"
    )
    for project_row in report["projects"]:
        for scope_row in project_row["scopes"]:
            record_jira_inventory(scope_row.get("source_counts", {}))
    await asyncio.to_thread(push_metrics, settings.metrics_pushgateway_url)
    atomic_json(args.report, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["status"] == "blocked" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--project")
    selection.add_argument("--all-connected-jira", action="store_true")
    parser.add_argument(
        "--phase", choices=("preflight", "canary", "full-stage"), default="preflight"
    )
    parser.add_argument("--run-id")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--resume-stage", action="store_true")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--issue-key", action="append", default=[])
    parser.add_argument("--reference-manifest", action="append", default=[])
    parser.add_argument("--approved-reference-root", action="append", default=[])
    args = parser.parse_args()
    if args.resume_stage and not args.run_id:
        parser.error("Resume requires the original explicit run-id")
    args.run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
    # The physical collection slug retains only 48 characters of its base.
    # Keep the entire run identity within that base to avoid collisions.
    if not re.fullmatch(r"[a-z0-9-]{6,37}", args.run_id):
        parser.error("run-id must contain 6–37 lowercase letters, digits, or hyphens")
    if args.resume_stage and args.phase == "preflight":
        parser.error("Resume is only supported for staging phases")
    if args.issue_key and (
        args.phase != "canary"
        or not args.project
        or len(set(args.issue_key)) > 10
        or any(not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", key) for key in args.issue_key)
    ):
        parser.error(
            "Targeted selection requires one project and at most ten valid canary issue keys"
        )
    args.issue_key = sorted(set(args.issue_key))
    raise SystemExit(asyncio.run(execute(args)))


if __name__ == "__main__":
    main()
