"""Create or verify the immutable checksum manifest for local model artifacts."""

import argparse
import hashlib
import json
from pathlib import Path


def inventory(roots: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for root in roots:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = f"{root.name}/{path.relative_to(root)}"
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            values[relative] = digest.hexdigest()
    return values


def verify(manifest: Path, roots: list[Path]) -> None:
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    if inventory(roots) != expected:
        raise RuntimeError("Local model artifact checksum verification failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("roots", nargs="+", type=Path)
    arguments = parser.parse_args()
    if arguments.write:
        arguments.manifest.write_text(
            json.dumps(inventory(arguments.roots), sort_keys=True), encoding="utf-8"
        )
    else:
        verify(arguments.manifest, arguments.roots)
