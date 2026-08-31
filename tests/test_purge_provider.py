"""A purge that misses the cursor is worse than no purge: the corpus is gone from
the index but the connector believes it is already up to date."""

import asyncio

from app.projects import (
    ConfluenceMapping,
    IngestionProject,
    VectorStoreRoute,
    RepositoryMapping,
)
from app.state import AzureTableManifestStore
from scripts.purge_provider import _scopes


class FakeTable:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted: list[tuple[str, str]] = []

    def query_entities(self, query, select=None):
        assert "PartitionKey eq" in query
        return [{"RowKey": row} for row in self.rows]

    def delete_entity(self, partition, row_key):
        self.deleted.append((partition, row_key))


def _store(rows):
    store = AzureTableManifestStore.__new__(AzureTableManifestStore)
    table = FakeTable(rows)
    store._table = table
    return store, table


def test_purge_scope_removes_cursor_and_quarantine_rows():
    store, table = _store(["abc", "__cursor__", "__quarantine__def"])
    assert asyncio.run(store.purge_scope("DEMO", "CONFLUENCE", "site|1")) == 3
    assert sorted(row for _partition, row in table.deleted) == [
        "__cursor__",
        "__quarantine__def",
        "abc",
    ]


def test_purge_scope_on_empty_partition_is_a_no_op():
    store, table = _store([])
    assert asyncio.run(store.purge_scope("DEMO", "CONFLUENCE", "site|1")) == 0
    assert table.deleted == []


def _project():
    return IngestionProject(
        project_id="DEMO",
        display_name="DEMO",
        repositories=(
            RepositoryMapping("acme", "repo", ("main", "release"), (), ()),
        ),
        jira_projects=(),
        confluence_spaces=(
            ConfluenceMapping(site_url="https://s.atlassian.net", space_key="Example", space_id="99"),
        ),
        vector_store=VectorStoreRoute(collection_name="project-intelligence", text_field="chunk_text"),
    )


def test_confluence_scope_matches_the_service_identity():
    assert _scopes(_project(), "CONFLUENCE") == ("https://s.atlassian.net|99",)


def test_github_scope_covers_every_indexed_branch():
    assert _scopes(_project(), "GITHUB") == ("acme/repo|main", "acme/repo|release")


def test_absent_mapping_yields_no_scopes_so_the_cli_refuses():
    assert _scopes(_project(), "JIRA") == ()


def test_local_has_no_scopes_because_it_has_no_mapping():
    # Local ingestion is discovered from a directory, so there is nothing in the
    # project record to derive a scope from. It is always the orphan path.
    assert _scopes(_project(), "LOCAL") == ()


def test_an_absent_mapping_still_permits_deleting_orphaned_vectors():
    """Removing a repository from the project record leaves its vectors behind.

    Nothing rediscovers them, so no run overwrites or deletes them, and they keep
    consuming a share of every query's candidates -- silently, if they were
    written under an older schema and are being discarded. Refusing to act when
    the mapping is gone left exactly that case unfixable.
    """

    from app.projects import IngestionProject, VectorStoreRoute

    orphaned = IngestionProject(
        project_id="DEMO",
        display_name="DEMO",
        repositories=(),
        jira_projects=(),
        confluence_spaces=(),
        vector_store=VectorStoreRoute(
            collection_name="project-intelligence", text_field="chunk_text"
        ),
    )

    # No scopes to purge, which is the signal to delete vectors only rather than
    # to give up.
    assert _scopes(orphaned, "GITHUB") == ()
    assert _scopes(orphaned, "JIRA") == ()
