"""Read-only visual eligibility preview; never prints source bodies."""

import asyncio
from collections import Counter
from pathlib import Path

from app.config import get_settings
from app.control_plane import BackendControlPlaneClient
from app.github import GitHubAppClient
from app.models import SourceDocument
from app.visual import analyze_markdown


async def main(project_id: str = "DEMO") -> None:
    settings = get_settings()
    project = await BackendControlPlaneClient(settings).get(project_id)
    if project is None:
        raise RuntimeError("Project mapping was not found.")
    documents = eligible = assets = 0
    types: Counter[str] = Counter()
    suffixes: Counter[str] = Counter()
    for mapping in project.repositories:
        client = GitHubAppClient(settings, mapping)
        for branch in mapping.indexed_branches:
            _commit, files = await client.repository_files(branch)
            suffixes.update(
                Path(item.path).suffix.lower() or "[none]"
                for item in files
                if not item.visual_asset
            )
            for item in files:
                if item.visual_asset or Path(item.path).suffix.lower() not in {".md", ".markdown"}:
                    continue
                content = await client.blob_content(item.blob_sha)
                if content is None:
                    continue
                documents += 1
                result = analyze_markdown(
                    SourceDocument(
                        project_id=project_id,
                        provider="GITHUB",
                        source_id=f"preview:{item.path}",
                        source_type="CODE",
                        title=item.path,
                        reference=item.path,
                        source_url="",
                        version=item.blob_sha,
                        content=content,
                        updated_at=None,
                        metadata={"path": item.path},
                        mime_type="text/markdown",
                    ),
                    settings,
                )
                eligible += int(result.eligible)
                assets += len(result.assets)
                types.update(result.visual_types)
    print(
        f"visual_preview=ok markdown_documents={documents} eligible={eligible} "
        f"renderable_assets={assets} types={dict(sorted(types.items()))} "
        f"source_types={dict(sorted(suffixes.items()))}"
    )


if __name__ == "__main__":
    asyncio.run(main())
