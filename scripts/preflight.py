"""Read-only live readiness checks for the ingestion data plane."""

import argparse
import asyncio
from pathlib import Path

from app.config import get_settings
from app.chroma_collections import project_collection_name, verify_project_collection
from app.control_plane import BackendControlPlaneClient
from app.github import GitHubAppClient
from app.state import AzureTableManifestStore


async def _project():
    project = await BackendControlPlaneClient(get_settings()).get("DEMO")
    if project is None:
        raise RuntimeError("DEMO is not available from the backend control plane.")
    print(
        "control_plane=ok "
        f"project={project.project_id} repositories={len(project.repositories)} "
        f"atlassian_connected={project.atlassian is not None}"
    )
    return project


async def _github() -> None:
    settings = get_settings()
    project = await _project()
    if not project.repositories:
        raise RuntimeError("DEMO has no configured GitHub repository.")
    mapping = project.repositories[0]
    branch = mapping.indexed_branches[0]
    commit, files = await GitHubAppClient(settings, mapping).repository_files(branch)
    print(f"github=ok branch={branch} commit={commit[:12]} indexable_files={sum(not item.visual_asset for item in files)}")


async def _table() -> None:
    settings = get_settings()
    project = await _project()
    scope = f"{project.repository.full_name}|main"
    cursor = await AzureTableManifestStore.from_settings(settings).get_cursor(
        project.project_id, "GITHUB", scope
    )
    print(f"azure_table=ok table={settings.state_table_name} existing_cursor={cursor is not None}")


async def _chroma() -> None:
    settings = get_settings()
    project = await _project()
    from chromadb import HttpClient
    physical_name = project_collection_name(
        project.vector_store.collection_name, project.project_id
    )
    collection = await asyncio.to_thread(
        HttpClient(host=settings.chroma_host, port=settings.chroma_port).get_collection,
        physical_name,
    )
    verify_project_collection(
        collection, project.vector_store.collection_name, project.project_id
    )
    total = await asyncio.to_thread(collection.count)
    print(
        f"chroma=ok collection={physical_name} total_records={total}"
    )


async def _visual_models() -> None:
    print("visual_models=disabled")


async def _run(component: str) -> None:
    checks = {
        "control-plane": _project,
        "github": _github,
        "table": _table,
        "chroma": _chroma,
        "visual-models": _visual_models,
    }
    await checks[component]()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "component",
        choices=("control-plane", "github", "table", "chroma", "visual-models"),
    )
    arguments = parser.parse_args()
    asyncio.run(_run(arguments.component))
