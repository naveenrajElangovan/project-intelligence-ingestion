# Project Intelligence Ingestion — quality status

Last verified: 2026-09-16. This is the sole current quality and repository-hygiene
summary for ingestion. `CURRENT_ARCHITECTURE.md` remains the sole architecture
source of truth; `DEVELOPMENT.md` and `JIRA_INGESTION_RUNBOOK.md` are operating
guides rather than competing status reports.

This repository follows the suite's canonical
[exact-pin dependency policy](https://github.com/naveenrajElangovan/project-intelligence-rag/blob/main/docs/DEPENDENCY_POLICY.md).

## Verified baseline

- Full suite: 313 passed, 2 warnings in 58.43 seconds.
- Ruff lint: 0 findings; Ruff format: 0 unformatted files.
- Mypy: 0 errors across all 36 application source files.
- Import-linter: one contract kept, zero broken.
- Dependency integrity: editable install and `pip check` pass; deptry reports 0
  unexplained findings across the maintained source, tests, scripts, and tools.
- Tests contain no permanent `skip`, `pytest.skip`, `unittest.skip`, or `xfail`
  markers.

## Closed repository-hygiene decisions

- `pyproject.toml` is the sole dependency declaration. Runtime, development, and
  worker requirements are exact pins; the worker-only stack is the `worker`
  optional dependency group. Both Dockerfiles install from that metadata. The
  duplicated `requirements.txt` and `requirements-worker.txt` files were removed
  after a five-repository reference search.
- Deptry is enforced locally, in pre-commit, and in CI. The only narrow rules are
  `uvicorn` (a container command-line entry point) and `pytest` (correctly declared
  in the development group while tests are deliberately scanned). Redundant direct
  declarations of `cryptography`, `langchain-core`, and `torch` were removed only
  after deptry and import searches confirmed they were transitive rather than used
  directly. Direct declarations were added for Azure Core, Pydantic, Starlette,
  Pillow, Docling Core, and Hugging Face Hub because maintained code imports them.
- `.env.example` and `.env.production.example` are intentionally distinct.
  `.env.example` is the complete local-development contract, with local endpoints
  and safe developer defaults. `.env.production.example` is the deployment
  contract, with HTTPS, production hosts, managed-identity/service endpoints,
  offline model paths, malware scanning, and visual processing. Both contain only
  empty or unmistakable placeholder credentials; real `.env` files remain ignored.
- Tests mirror ownership under `tests/app/` (including `tests/app/api/`), with
  separate `tests/labs/` and `tests/scripts/` packages. Cross-test imports use the
  package-qualified paths, and the single full-suite entrypoint remains `make test`.
- Ruff and mypy ratchets and their baselines were deleted after their underlying
  counts reached zero. The sole typing boundary assertion is the inline-documented
  Azure Service Bus 7.14 aio/synchronous credential signature mismatch under
  Python 3.12 and mypy 1.20; it uses one `cast(Any, credential)`, not an ignore.

## Reproduction

```bash
.venv/bin/pytest -o addopts='' -q
make format-check lint typecheck imports PYTHON=.venv/bin/python PATH="$PWD/.venv/bin:$PATH"
.venv/bin/python -m deptry . --exclude '.venv|\.git|corpora|\.quality-runs' \
  --optional-dependencies-dev-groups dev \
  --per-rule-ignores 'DEP002=uvicorn,DEP004=pytest'
.venv/bin/python -m pip check
```
