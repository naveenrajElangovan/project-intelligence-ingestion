"""Run Ruff without rewriting the repository's existing formatting debt.

The baseline records the exact files and rule counts that pre-date Ruff. A resolved
finding disappears naturally, while any new rule/file pair or newly unformatted file
fails the check. This lets the repository adopt Ruff now without a broad source rewrite.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "tools/ruff_baseline.json").read_text(encoding="utf-8"))
QUALITY_PATHS = ("app", "tests", "scripts", "tools")


def _ruff(arguments: list[str]) -> list[dict[str, object]]:
    """Run Ruff in machine-readable mode and fail clearly if Ruff itself breaks."""
    completed = subprocess.run(
        [sys.executable, "-m", "ruff", *arguments, "--output-format=json", *QUALITY_PATHS],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        print(completed.stdout + completed.stderr, file=sys.stderr)
        raise RuntimeError(f"Ruff could not complete (exit {completed.returncode}).")
    return json.loads(completed.stdout or "[]")


def _relative(finding: dict[str, object]) -> str:
    """Return a stable repository-relative filename from one Ruff result."""
    return str(Path(str(finding["filename"])).resolve().relative_to(ROOT))


def check_lint() -> int:
    """Reject any lint finding that exceeds the recorded file-and-rule baseline."""
    findings = _ruff(["check", "--no-cache"])
    observed = Counter(f"{_relative(item)}:{item['code']}" for item in findings)
    allowed = Counter({key: int(value) for key, value in BASELINE["lint"].items()})
    excess = observed - allowed
    if excess:
        print("Ruff lint ratchet failed; new findings:", file=sys.stderr)
        for key, count in sorted(excess.items()):
            print(f"  {key}: +{count}", file=sys.stderr)
        return 1
    print(f"Ruff lint ratchet passed: {sum(observed.values())}/{sum(allowed.values())} findings.")
    return 0


def check_format() -> int:
    """Reject an unformatted file unless that exact file is recorded in the baseline."""
    findings = _ruff(["format", "--no-cache", "--check"])
    observed = {_relative(item) for item in findings}
    allowed = set(BASELINE["format"])
    excess = sorted(observed - allowed)
    if excess:
        print("Ruff format ratchet failed; newly unformatted files:", file=sys.stderr)
        for path in excess:
            print(f"  {path}", file=sys.stderr)
        return 1
    print(f"Ruff format ratchet passed: {len(observed)}/{len(allowed)} files remain in baseline.")
    return 0


def main() -> int:
    """Choose the lint or formatting ratchet requested by the command line."""
    if len(sys.argv) != 2 or sys.argv[1] not in {"lint", "format"}:
        print("usage: check_ruff_ratchet.py {lint|format}", file=sys.stderr)
        return 2
    return check_lint() if sys.argv[1] == "lint" else check_format()


if __name__ == "__main__":
    raise SystemExit(main())
