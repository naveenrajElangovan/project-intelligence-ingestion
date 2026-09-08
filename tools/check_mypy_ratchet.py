"""Prevent the known type-checking backlog from growing.

This check runs mypy across the application. Existing findings are allowed up to the
recorded baseline, while the small pure modules selected in ``pyproject.toml`` already
run in strict mode. The command fails if mypy itself fails or if a new finding appears.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_ERRORS = 85
ERROR_PATTERN = re.compile(r"^.+:\d+: error:", re.MULTILINE)
STRICT_MODULES = (
    "app/access_rules.py",
    "app/chroma_collections.py",
    "app/chunking.py",
    "app/globbing.py",
    "app/logging.py",
    "app/parsing.py",
)


def _run_mypy(arguments: list[str], cache_dir: str) -> subprocess.CompletedProcess[str]:
    """Run one isolated mypy check and return all output to the caller."""
    return subprocess.run(
        [sys.executable, "-m", "mypy", *arguments, "--cache-dir", cache_dir],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        text=True,
    )


def main() -> int:
    """Run mypy, print its findings, and reject an increase above the baseline."""
    with tempfile.TemporaryDirectory(prefix="ingestion-mypy-") as cache_dir:
        completed = _run_mypy(["app", "--no-error-summary"], cache_dir)

    output = completed.stdout + completed.stderr
    error_count = len(ERROR_PATTERN.findall(output))
    if completed.returncode not in {0, 1}:
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        print(f"mypy could not complete (exit {completed.returncode}).", file=sys.stderr)
        return 2
    if completed.returncode == 1 and error_count == 0:
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        print("mypy failed without producing countable findings.", file=sys.stderr)
        return 2
    if error_count > MAX_ERRORS:
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        print(
            f"mypy ratchet failed: {error_count} findings exceed baseline {MAX_ERRORS}.",
            file=sys.stderr,
        )
        return 1

    print(f"mypy ratchet passed: {error_count}/{MAX_ERRORS} findings.")

    with tempfile.TemporaryDirectory(prefix="ingestion-mypy-strict-") as cache_dir:
        for module in STRICT_MODULES:
            strict_result = _run_mypy([module, "--strict"], cache_dir)
            if strict_result.returncode != 0:
                strict_output = strict_result.stdout + strict_result.stderr
                print(strict_output, end="" if strict_output.endswith("\n") else "\n")
                print(f"Strict mypy check failed for {module}.", file=sys.stderr)
                return 1
    print(f"Strict mypy passed for {len(STRICT_MODULES)} dependency-light modules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
