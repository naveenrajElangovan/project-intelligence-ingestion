"""Discovery for a repository that exists only on disk.

Two rules are asserted here. The walk is an allowlist of suffixes rather than a
denylist of binaries, because a repository holds far more generated and binary
shapes than source ones. And it indexes **code only**: documentation for this
platform lives in Confluence, so a README indexed from a repository would become
a second documentation source competing with the pages for the same question.
"""

import hashlib
from pathlib import Path

from scripts.ingest_local_repository import DEFAULT_EXCLUDE, _iter_files


def _repository(root: Path) -> Path:
    files = {
        "src/main/kotlin/Cart.kt": "package pos\nclass Cart { fun close() {} }\n",
        "src/main/kotlin/Shift.kt": "package pos\nclass Shift\n",
        "README.md": "# POS\n\nDocs.\n",
        "build/generated/Big.kt": "generated\n",
        ".git/config": "[core]\n",
        "node_modules/x/index.js": "module.exports={}\n",
        "project-documentation/POS-RAG-00.md": "# excluded by default\n",
        "empty.kt": "   \n",
        "notes.docx": "not really a docx",
    }
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets/logo.png").write_bytes(b"\x89PNG\r\n\x1a\n binary")
    (root / "huge.kt").write_text("x" * 1_200_000, encoding="utf-8")
    return root


def test_only_reviewable_source_is_discovered(tmp_path):
    found = sorted(item.relative_path for item in _iter_files(_repository(tmp_path), DEFAULT_EXCLUDE))

    assert found == ["src/main/kotlin/Cart.kt", "src/main/kotlin/Shift.kt"]


def test_generated_and_vendored_directories_are_skipped(tmp_path):
    found = {item.relative_path for item in _iter_files(_repository(tmp_path), DEFAULT_EXCLUDE)}

    assert not any(path.startswith(("build/", ".git/", "node_modules/")) for path in found)


def test_the_default_exclusions_actually_exclude(tmp_path):
    # This is the case fnmatch got wrong: a top-level project-documentation
    # directory. The exclusion read as though it applied and did not.
    found = {item.relative_path for item in _iter_files(_repository(tmp_path), DEFAULT_EXCLUDE)}

    assert "project-documentation/POS-RAG-00.md" not in found


def test_an_oversized_file_is_skipped(tmp_path):
    found = {item.relative_path for item in _iter_files(_repository(tmp_path), DEFAULT_EXCLUDE)}

    assert "huge.kt" not in found


def test_the_version_is_the_content_hash_so_unchanged_files_skip(tmp_path):
    root = _repository(tmp_path)
    relative = "src/main/kotlin/Cart.kt"

    item = next(entry for entry in _iter_files(root, DEFAULT_EXCLUDE) if entry.relative_path == relative)

    assert item.digest == hashlib.sha256((root / relative).read_bytes()).hexdigest()


def test_repository_prose_is_never_indexed(tmp_path):
    # The rule this enforces: documentation comes from Confluence only. A README
    # or a docs page in a repository must not become a rival source.
    root = _repository(tmp_path)
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs/architecture.md").write_text("# Architecture\n", encoding="utf-8")

    found = {item.relative_path for item in _iter_files(root, DEFAULT_EXCLUDE)}

    assert not any(path.endswith((".md", ".markdown", ".txt")) for path in found)


def test_build_configuration_is_still_indexed(tmp_path):
    # Not prose: which module depends on what is a code question.
    root = _repository(tmp_path)
    (root / "settings.gradle.kts").write_text('include(":composeApp")\n', encoding="utf-8")

    found = {item.relative_path for item in _iter_files(root, DEFAULT_EXCLUDE)}

    assert "settings.gradle.kts" in found


def test_shared_detekt_configuration_is_not_application_evidence(tmp_path):
    root = _repository(tmp_path)
    path = root / "module/config/detekt/detekt.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("complexity:\n  active: true\n", encoding="utf-8")

    found = {item.relative_path for item in _iter_files(root, DEFAULT_EXCLUDE)}

    assert "module/config/detekt/detekt.yml" not in found


def test_discovery_is_deterministic(tmp_path):
    root = _repository(tmp_path)

    first = [item.relative_path for item in _iter_files(root, DEFAULT_EXCLUDE)]
    second = [item.relative_path for item in _iter_files(root, DEFAULT_EXCLUDE)]

    assert first == second
