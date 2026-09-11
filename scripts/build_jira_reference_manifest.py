"""Pin authorized local documentation references without ingesting their content."""

import argparse
import json
from pathlib import Path

from app.jira_run import atomic_json
from app.source_references import build_reference_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    manifest = build_reference_manifest(
        root, args.project, args.alias, (root / "docs").rglob("*.md")
    )
    atomic_json(args.out, manifest)
    print(
        json.dumps(
            {
                "manifest_hash": manifest["manifest_hash"],
                "source_revision": manifest["source_revision"],
                "files": len(manifest["files"]),
                "path": str(args.out),
            }
        )
    )


if __name__ == "__main__":
    main()
