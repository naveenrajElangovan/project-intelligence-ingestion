from app.service import _github_manifest_is_unchanged
from app.state import SourceManifest, _manifest


def _source_manifest(access_policy_id: str = "project:DEMO") -> SourceManifest:
    return SourceManifest(
        project_id="DEMO",
        provider="GITHUB",
        scope="repo|main",
        source_id="repository:repo:branch:main:path:app.py",
        source_type="CODE",
        title="app.py",
        source_url="https://github.example/app.py",
        version="blob-sha",
        content_hash="content-hash",
        chunk_count=1,
        last_seen_run="scan-1",
        access_policy_id=access_policy_id,
    )


def test_github_fast_skip_requires_the_same_resolved_policy() -> None:
    assert _github_manifest_is_unchanged(
        _source_manifest(),
        blob_sha="blob-sha",
        access_policy_id="project:DEMO",
        full=False,
    )
    assert not _github_manifest_is_unchanged(
        _source_manifest(),
        blob_sha="blob-sha",
        access_policy_id="department:DEMO:ENGINEERING",
        full=False,
    )


def test_github_fast_skip_stays_disabled_for_full_ingestion() -> None:
    assert not _github_manifest_is_unchanged(
        _source_manifest(),
        blob_sha="blob-sha",
        access_policy_id="project:DEMO",
        full=True,
    )


def test_legacy_table_entity_has_an_empty_policy_sentinel() -> None:
    manifest = _manifest(
        {
            "project_id": "DEMO",
            "provider": "CONFLUENCE",
            "scope": "space:demo",
            "source_id": "page:1",
        }
    )

    assert manifest.access_policy_id == ""
