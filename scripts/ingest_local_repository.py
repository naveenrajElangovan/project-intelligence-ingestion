"""Ingest a repository from a local directory instead of the GitHub API.

Why this exists. The GitHub connector needs a GitHub App installation and a
reachable remote. When the code is only on disk -- a clone that was never pushed,
a repository the App is not installed on, or an air-gapped machine -- there is no
way to give the RAG any code evidence at all, and every implementation question
degrades to what the documentation happens to say.

This is deliberately not a new provider. It builds SourceDocuments the same shape
the GitHub connector builds and hands each one to the same
DocumentIngestionWorkflow, so the security scan, format routing, chunking, local
embedding, Chroma write and manifest commit are byte-for-byte the paths a real
GitHub ingestion takes. Only discovery differs. That means:

* incremental runs work -- the version is the content hash, so an unchanged file
  is SKIPped exactly as a matching blob SHA would be;
* nothing new has to be added to the control-plane contract, the provider enum,
  or the ingestion API;
* the records are indistinguishable to retrieval, which filters on `source_type`
  and never on `provider`.

`provider` is recorded as LOCAL so the audit and the contract validator can tell
these apart from App-sourced records, and `source_url` is a file:// URL, which is
honest about where the evidence came from rather than fabricating a github.com
link that would 404 for anyone who clicked it.

    python -m scripts.ingest_local_repository --project DEMO \\
        --path ~/Desktop/Example/checkout-demo-app-kotlin --full
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import mimetypes
from pathlib import Path
import sys
import uuid

from app.config import get_settings
from app.content_security import QuarantinedDocument
from app.globbing import matches_any
from app.control_plane import BackendControlPlaneClient
from app.dependencies import get_document_workflow
from app.models import SourceDocument


# Directories that never carry reviewable source. Walking them wastes minutes on
# a Kotlin repository and fills the index with generated noise.
SKIP_DIRECTORIES = {
    ".git", ".gradle", ".idea", ".venv", "venv", "env", "__pycache__", "node_modules",
    "build", "out", "dist", ".dart_tool", ".kotlin", "DerivedData", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".terraform",
}

# Code and build configuration only. An allowlist rather than a denylist: a
# repository holds far more binary and generated shapes than source ones, and a
# missed binary reaches the security scanner as a surprise.
#
# Prose suffixes are deliberately absent. Documentation for this platform lives
# in Confluence and nowhere else, so a repository README or docs/ page indexed
# from here would become a second, independently-versioned documentation source
# competing with the pages for the same question -- and whichever scored higher
# would be cited. Enforcing that at discovery is stronger than an exclusion list,
# because there is no pattern to get wrong and nothing to keep in sync.
SOURCE_SUFFIXES = {
    ".kt", ".kts", ".java", ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
    ".swift", ".m", ".mm", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php",
    ".sh", ".bash", ".zsh", ".sql", ".gradle", ".toml", ".yaml", ".yml", ".json",
    ".xml", ".properties", ".proto", ".graphql", ".tf",
}

# Suffixes a caller might expect to be indexed, refused on purpose, so a dry run
# can explain the absence rather than leave it looking like a bug.
PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc", ".docx", ".pdf"}

DEFAULT_EXCLUDE = (
    "**/project-documentation/**",
    "**/config/detekt/**",
)
MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class Discovered:
    relative_path: str
    text: str
    digest: str


def _iter_files(root: Path, excludes: tuple[str, ...]) -> list[Discovered]:
    found: list[Discovered] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        if matches_any(relative, excludes):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if len(raw) > MAX_FILE_BYTES:
            # A megabyte of one file is a generated artefact or a vendored blob,
            # not something a reader will ever be pointed at.
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Not text after all, whatever the suffix claimed.
            continue
        if not text.strip():
            continue
        found.append(
            Discovered(
                relative_path=relative,
                text=text,
                # Stands in for a blob SHA: same content means same version means
                # SKIP on the next run.
                digest=hashlib.sha256(raw).hexdigest(),
            )
        )
    return found



def _count_prose(root: Path) -> int:
    """How many prose files were passed over, so the omission is visible."""

    return sum(
        1
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in PROSE_SUFFIXES
        and not any(part in SKIP_DIRECTORIES for part in path.relative_to(root).parts)
    )


async def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument(
        "--name",
        default="",
        help="Repository label recorded on every record. Defaults to the directory name.",
    )
    parser.add_argument("--branch", default="local")
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="Glob to skip, repeatable. Defaults to the documented corpus exclusions.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Re-chunk and rewrite every file even when its manifest matches.",
    )
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    root = arguments.path.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"{root} is not a directory.")
    name = arguments.name or root.name
    excludes = tuple(arguments.exclude) if arguments.exclude is not None else DEFAULT_EXCLUDE

    settings = get_settings()
    project = await BackendControlPlaneClient(settings).get(arguments.project)
    if project is None:
        raise SystemExit(f"Project {arguments.project} is not configured in the control plane.")

    files = _iter_files(root, excludes)
    if not files:
        raise SystemExit(f"No indexable source files found under {root}.")
    prose = _count_prose(root)
    print(f"discovered {len(files)} code files under {root}")
    if prose:
        print(
            f"skipped {prose} prose files (.md, .txt and similar): documentation "
            "comes from Confluence, and a second copy here would compete with it"
        )
    if arguments.dry_run:
        for item in files[:20]:
            print(f"  {item.relative_path}")
        if len(files) > 20:
            print(f"  … and {len(files) - 20} more")
        return

    scope = f"{name}|{arguments.branch}"
    scan_id = str(uuid.uuid4())
    workflow = get_document_workflow()
    indexed = unchanged = quarantined = failed = chunks = 0

    for item in files:
        document = SourceDocument(
            project_id=project.project_id,
            provider="LOCAL",
            source_id=f"local:{name}:{arguments.branch}:{item.relative_path}",
            # CODE is what the RAG's implementation and code-assisted routes
            # filter on. A Markdown file in a repository is documentation about
            # the code and belongs to the same scope.
            source_type="CODE",
            title=item.relative_path,
            reference=f"{name}:{arguments.branch}:{item.relative_path}",
            source_url=(root / item.relative_path).as_uri(),
            version=item.digest,
            content=item.text,
            updated_at=None,
            metadata={
                "repository": name,
                "branch": arguments.branch,
                "path": item.relative_path,
                "origin": "local-directory",
            },
            mime_type=mimetypes.guess_type(item.relative_path)[0] or "text/plain",
        )
        try:
            rule_arguments = (
                {"source_access_rules": project.source_access_rules}
                if project.source_access_rules
                else {}
            )
            result = await workflow.run(
                document,
                scope,
                scan_id,
                project.vector_store,
                force=arguments.full,
                **rule_arguments,
            )
        except QuarantinedDocument as error:
            quarantined += 1
            print(
                f"  quarantined {item.relative_path}: {error}",
                file=sys.stderr,
            )
            continue
        except Exception as error:  # noqa: BLE001 - one bad file must not end the run
            failed += 1
            print(f"  failed {item.relative_path}: {type(error).__name__}: {error}", file=sys.stderr)
            continue
        if result.operation == "UNCHANGED":
            unchanged += 1
            continue
        indexed += 1
        chunks += result.chunks_written

    print(
        f"provider=LOCAL scope={scope} indexed={indexed} unchanged={unchanged} "
        f"quarantined={quarantined} failed={failed} chunksWritten={chunks}"
    )
    # No cursor is written: discovery here is a directory walk, not a
    # time-ordered feed, so there is no watermark that would mean anything.
    if failed:
        raise SystemExit(1)
    # Provider scopes refresh this record in IngestionService. Local-directory
    # ingestion bypasses that service by design, so it must publish the same
    # project-wide vocabulary explicitly after all vector writes succeed.
    await workflow.refresh_project_vocabulary(project.vector_store, project.project_id)


if __name__ == "__main__":
    asyncio.run(_run())
