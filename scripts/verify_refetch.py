"""Prove that every configured source root can be fetched before a purge.

This command is deliberately read-only. It resolves the project through the
backend control plane and uses the same Atlassian gateway and credentials as the
ingestion service. It never reads, prints, or copies an Atlassian token.

    .venv/bin/python -m scripts.verify_refetch \
        --project T2.0-STORE --provider CONFLUENCE
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from app.atlassian import AtlassianSourceClient, _confluence_page
from app.config import get_settings
from app.control_plane import BackendControlPlaneClient
from app.projects import ConfluenceMapping, IngestionProject


@dataclass(frozen=True, slots=True)
class FetchedPage:
    page_id: str
    title: str
    body_length: int


async def _fetch_page(
    client: AtlassianSourceClient,
    project: IngestionProject,
    mapping: ConfluenceMapping,
    page_id: str,
) -> FetchedPage:
    origin = f"https://api.atlassian.com/ex/confluence/{project.atlassian.cloud_id}"
    response = await client._get(  # Same backend proxy used by normal ingestion.
        f"{origin}/wiki/api/v2/pages/{page_id}",
        {"body-format": "storage"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Confluence returned an invalid page response.")

    returned_id = str(payload.get("id") or "")
    if returned_id != page_id:
        raise RuntimeError(
            f"Confluence returned page {returned_id or '<missing>'} for requested page {page_id}."
        )

    document = _confluence_page(project.project_id, mapping, payload)
    if not document.content.strip():
        document = await client._confluence_live_body(
            origin, project.project_id, mapping, payload
        )
    body = document.content.strip()
    if not body:
        raise RuntimeError("Confluence page has an empty body.")

    return FetchedPage(
        page_id=page_id,
        title=document.title,
        body_length=len(body),
    )


async def verify_refetch(project_id: str, provider: str) -> tuple[FetchedPage, ...]:
    if provider.upper() != "CONFLUENCE":
        raise RuntimeError("verify_refetch currently supports provider CONFLUENCE only.")

    settings = get_settings()
    gateway = BackendControlPlaneClient(settings)
    project = await gateway.get(project_id)
    if project is None:
        raise RuntimeError(f"Project {project_id} is not configured in the control plane.")
    if project.atlassian is None:
        raise RuntimeError(f"Project {project_id} has no Atlassian connection.")
    if not project.confluence_spaces:
        raise RuntimeError(f"Project {project_id} has no Confluence space mapping.")

    configured_ids = [
        page_id
        for mapping in project.confluence_spaces
        for page_id in mapping.root_page_ids
    ]
    if not configured_ids:
        raise RuntimeError(f"Project {project_id} has no configured Confluence rootPageIds.")
    if len(configured_ids) != len(set(configured_ids)):
        raise RuntimeError(f"Project {project_id} has duplicate Confluence rootPageIds.")

    client = AtlassianSourceClient(
        settings,
        gateway,
        project.project_id,
        project.atlassian.cloud_id,
        project.atlassian.resource_url,
    )
    fetched: list[FetchedPage] = []
    failures: list[str] = []
    for mapping in project.confluence_spaces:
        for page_id in mapping.root_page_ids:
            try:
                page = await _fetch_page(client, project, mapping, page_id)
            except Exception as error:
                failures.append(f"pageId={page_id} error={type(error).__name__}: {error}")
            else:
                fetched.append(page)

    for page in fetched:
        print(
            f"pageId={page.page_id} title={page.title!r} "
            f"bodyLength={page.body_length}"
        )
    print(
        f"refetch project={project.project_id} provider=CONFLUENCE "
        f"configuredPages={len(configured_ids)} fetchedPages={len(fetched)}"
    )

    if failures:
        failure_lines = "\n".join(f"  - {failure}" for failure in failures)
        raise RuntimeError(
            f"Confluence refetch verification failed for {len(failures)} page(s):\n"
            f"{failure_lines}"
        )
    if len(fetched) != len(configured_ids):
        raise RuntimeError(
            f"Fetched {len(fetched)} of {len(configured_ids)} configured Confluence pages."
        )
    return tuple(fetched)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--provider", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _arguments()
    try:
        asyncio.run(verify_refetch(arguments.project, arguments.provider))
    except Exception as error:
        raise SystemExit(f"REFETCH FAILED: {error}") from error
    print("REFETCH OK")
