"""Regression checks for the operator-facing Chroma restore verifier."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".baselines" / "verify_restore.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_restore", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_connection_failure_is_visible_and_nonzero(tmp_path, monkeypatch, capsys) -> None:
    verifier = _load_verifier()
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"collections": {}}), encoding="utf-8")

    def unavailable():
        raise RuntimeError("Chroma heartbeat timed out")

    monkeypatch.setattr(verifier, "live", unavailable)

    assert verifier.main(str(baseline)) == 2
    output = capsys.readouterr()
    assert "Checking live Chroma contents" in output.out
    assert "RESTORE CHECK FAILED" in output.err
    assert "Chroma heartbeat timed out" in output.err
