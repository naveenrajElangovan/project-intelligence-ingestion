"""Manual/cron entrypoint. Run from the repository root with `python -m scripts.run_ingestion`."""

import argparse
import asyncio
import json

from app.dependencies import get_ingestion_service


async def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--provider",
        action="append",
        choices=("GITHUB", "JIRA", "CONFLUENCE"),
        dest="providers",
    )
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--target-collection-name")
    parser.add_argument("--target-schema-version")
    args = parser.parse_args()
    results = await get_ingestion_service().ingest_project(
        args.project,
        tuple(args.providers or ("GITHUB", "JIRA", "CONFLUENCE")),
        full=args.full,
        target_collection_name=args.target_collection_name,
        target_schema_version=args.target_schema_version,
    )
    payload = [
                {
                    "projectId": value.project_id,
                    "provider": value.provider,
                    "discovered": value.discovered,
                    "indexed": value.indexed,
                    "unchanged": value.unchanged,
                    "deleted": value.deleted,
                    "failed": value.failed,
                    "chunksWritten": value.chunks_written,
                    "documentsAnalyzed": value.documents_analyzed,
                    "textOnlyDocuments": value.text_only_documents,
                    "visualEligibleDocuments": value.visual_eligible_documents,
                    "visualAssetsStored": value.visual_assets_stored,
                    "visualProcessingFailures": value.visual_processing_failures,
                }
                for value in results
            ]
    print(json.dumps(payload, indent=2))
    if any(value.failed for value in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_run())
