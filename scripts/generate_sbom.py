"""Generate a deterministic CycloneDX package inventory for the built image."""

import argparse
from importlib.metadata import distributions
import json
from pathlib import Path


def build() -> dict[str, object]:
    components = sorted(
        (
            {
                "type": "library",
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "purl": f"pkg:pypi/{distribution.metadata['Name'].lower().replace('_', '-')}@{distribution.version}",
            }
            for distribution in distributions()
            if distribution.metadata.get("Name")
        ),
        key=lambda item: (str(item["name"]).lower(), str(item["version"])),
    )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "project-intelligence"}},
        "components": components,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    arguments.output.write_text(json.dumps(build(), sort_keys=True), encoding="utf-8")

