"""Erase one provider's indexed state for a project, so the next full run rebuilds it.

Why this exists: `--full` reconciles against what the provider currently returns.
That is the right behaviour for ordinary drift, but it cannot recover from a
corpus that was reorganised underneath it -- pages deleted and re-created get new
IDs, so the old vectors survive under manifests that no longer match anything the
connector will ever discover again. The result is a collection holding two
generations of the same documents, and retrieval cites the dead one.

Purging is deliberately provider-scoped. Nothing here touches the other
providers' vectors or manifests.

Order matters: vectors are deleted before the manifest rows that describe them.
A failure halfway through therefore leaves manifests pointing at vectors that may
already be gone, which a re-run cleans up idempotently. Deleting the manifests
first would orphan vectors with no record that they exist.

    python -m scripts.purge_provider --project DEMO --provider CONFLUENCE --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

try:
    from app.config import get_settings
    from app.control_plane import BackendControlPlaneClient
    from app.projects import IngestionProject
    from app.state import AzureTableManifestStore
    from app.dependencies import get_vector_store
except ModuleNotFoundError as error:  # pragma: no cover - interpreter guidance
    # A bare `python3 -m scripts.purge_provider` uses the system interpreter,
    # where none of the dependencies are installed, and the resulting traceback
    # points at azure.core rather than at the real mistake. Say the real thing.
    raise SystemExit(
        f"{error.name} is not importable, which usually means this ran on the "
        "system interpreter. Use the project virtual environment:\n"
        "  .venv/bin/python -m scripts.purge_provider ...\n"
        "or scripts/run_confluence_reset.sh, which selects it for you."
    ) from error


def _scopes(project: IngestionProject, provider: str) -> tuple[str, ...]:
    """Mirror the scope identities IngestionService writes state under."""

    if provider == "LOCAL":
        # Local ingestion is discovered from a directory, not from the project
        # record, so there is no mapping to derive a scope from.
        return ()
    if provider == "CONFLUENCE":
        return tuple(f"{m.site_url}|{m.space_id}" for m in project.confluence_spaces)
    if provider == "JIRA":
        return tuple(f"{m.site_url}|{m.project_key}" for m in project.jira_projects)
    return tuple(
        f"{m.full_name}|{branch}"
        for m in project.repositories
        for branch in (m.indexed_branches or ("main",))
    )


async def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required. Without it nothing is deleted and the plan is printed.",
    )
    args = parser.parse_args()

    settings = get_settings()
    project = await BackendControlPlaneClient(settings).get(args.project)
    if project is None:
        raise SystemExit(f"Project {args.project} is not configured in the control plane.")
    scopes = _scopes(project, args.provider)
    # An absent mapping does not mean an absent corpus. Removing a repository from
    # the project record leaves its vectors behind: nothing rediscovers them, so
    # no run ever overwrites or deletes them, and they keep being retrieved --
    # or, if they were written under an older schema, keep being discarded and
    # keep consuming a share of every query's candidates. Refusing here left the
    # only case that actually needs cleaning up with no way to clean it up.
    # Vectors are deleted by a provider filter and need no scope; only the state
    # rows do.
    if not scopes:
        print(
            f"No {args.provider} mapping on {args.project}: deleting orphaned "
            "vectors only, since there are no scopes whose manifests could be "
            "purged."
        )

    store = AzureTableManifestStore.from_settings(settings)
    # No passage embedder: deleting never computes a vector. Going through
    # app.dependencies here would construct the ingestion workflow and load the
    # 9.5 GB E5 encoder into memory just to run a delete.
    vectors = get_vector_store()
    plan = {
        "projectId": project.project_id,
        "provider": args.provider,
        "collectionName": project.vector_store.collection_name,
        "scopes": list(scopes),
        "dryRun": not args.yes,
    }
    if not args.yes:
        print(json.dumps(plan, indent=2))
        if os.environ.get("PI_PURGE_PLAN_ONLY_NOTICE", "true") == "true":
            print("Nothing deleted. Re-run with --yes to execute.")
        return

    deleted_vectors = await vectors.delete_provider(
        project.vector_store, project.project_id, args.provider
    )
    deleted_rows = 0
    for scope in scopes:
        deleted_rows += await store.purge_scope(project.project_id, args.provider, scope)
    if not scopes:
        print(
            "State rows for this provider were left untouched. If the mapping is "
            "restored later, run this again to clear the manifests as well."
        )
    print(
        json.dumps(
            {**plan, "vectorDeleteIssued": deleted_vectors, "stateRowsDeleted": deleted_rows},
            indent=2,
        )
    )
    if scopes:
        print(
            f"Purged. Re-index with: .venv/bin/python -m scripts.run_ingestion "
            f"--project {project.project_id} --provider {args.provider} --full"
        )
    else:
        # Printing a re-index hint here would be wrong: with no mapping there is
        # nothing to rediscover, so the command would find zero documents and
        # report success. The provider is simply gone from this project until a
        # mapping is added back.
        print(
            f"Purged. {args.provider} has no mapping on {project.project_id}, so "
            "there is nothing to re-index -- the provider is now absent from this "
            "project rather than stale."
        )


if __name__ == "__main__":
    asyncio.run(_run())
