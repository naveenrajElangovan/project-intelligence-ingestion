"""Reconstruct a project's provider mappings from the ingestion state table.

Why this can work at all: every manifest row records the scope it was written
under, and IngestionService builds those scopes out of the mapping itself --
"owner/repo|branch" for GitHub, "siteUrl|projectKey" for Jira,
"siteUrl|spaceId" for Confluence. So the table is a partial, independent copy of
the control-plane mapping, and it lives in Azure Table Storage rather than Azure
SQL, which is why it is still readable when the SQL database is paused.

What it CANNOT recover, because it was never written here: includePaths,
excludePaths, the Confluence spaceKey, rootPageIds, and the ingestion schedule.
Those exist only in the control-plane row. Omitting them means defaults apply --
notably no path exclusions -- so review the output before trusting it.

    python -m scripts.recover_project_mappings --project DEMO
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import json

from app.config import get_settings
from app.state import AzureTableManifestStore


async def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    arguments = parser.parse_args()

    settings = get_settings()
    store = AzureTableManifestStore.from_settings(settings)

    found = await store.scan_scopes(arguments.project)

    repositories: dict[str, set[str]] = defaultdict(set)
    for scope in sorted(found.get("GITHUB", ())):
        full_name, _, branch = scope.partition("|")
        if "/" in full_name:
            repositories[full_name].add(branch)

    github = [
        {
            "owner": full_name.split("/", 1)[0],
            "repository": full_name.split("/", 1)[1],
            "indexedBranches": sorted(branches),
            "includePaths": [],
            "excludePaths": ["**/*.docx", "**/project-documentation/**"],
        }
        for full_name, branches in sorted(repositories.items())
    ]
    jira = []
    for scope in sorted(found.get("JIRA", ())):
        site_url, _, project_key = scope.partition("|")
        if site_url and project_key:
            jira.append({"siteUrl": site_url, "projectKey": project_key})
    confluence = []
    for scope in sorted(found.get("CONFLUENCE", ())):
        site_url, _, space_id = scope.partition("|")
        if site_url and space_id:
            confluence.append(
                {
                    "siteUrl": site_url,
                    "spaceKey": "",
                    "spaceId": space_id,
                    "rootPageIds": [],
                }
            )

    print(
        json.dumps(
            {
                "githubRepositories": github,
                "jiraProjects": jira,
                "confluenceSpaces": confluence,
            },
            indent=2,
        )
    )
    if not found:
        print(
            "\nNo manifest rows matched. Either the project id differs or the "
            "state table was purged for every provider.",
        )
    else:
        print(
            "\nRecovered from scopes only. includePaths, spaceKey, rootPageIds and "
            "the ingestion schedule are not stored here -- set them yourself. The "
            "excludePaths above are the documented intent, not a recovered value.",
        )


if __name__ == "__main__":
    asyncio.run(_run())
