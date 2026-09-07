#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
BACKEND_DIRECTORY="$(cd "${PROJECT_DIRECTORY}/../project-intelligence-backend" && pwd)"

if [[ ! -x "${PROJECT_DIRECTORY}/.venv/bin/python" ]]; then
  echo "The ingestion virtual environment is missing. Create it and install the project first." >&2
  exit 1
fi
if [[ ! -f "${PROJECT_DIRECTORY}/.env" ]]; then
  echo "${PROJECT_DIRECTORY}/.env is missing. Configure ingestion before running it." >&2
  exit 1
fi

# This is the single local entry point for backend dependencies plus ingestion.
# The backend launcher is idempotent: it reuses healthy services, starts missing
# ones, restores the encrypted provider credential store when switching from the
# Docker backend to the native backend, and returns only after /ready succeeds.
"${BACKEND_DIRECTORY}/scripts/prepare_and_start_local_app.sh"

cd "${PROJECT_DIRECTORY}"

# The runner is intentionally project-scoped. DEMO is the current local default,
# while an explicit --project remains available for another configured project.
project_supplied=false
for argument in "$@"; do
  if [[ "${argument}" == "--project" || "${argument}" == --project=* ]]; then
    project_supplied=true
    break
  fi
done
if [[ "${project_supplied}" == "false" ]]; then
  set -- --project DEMO "$@"
fi

# Logged as well as printed. A full run takes minutes and its failure detail --
# a provider traceback, a per-document error, the counters -- previously existed
# only in terminal scrollback, so an interrupted or closed window lost the one
# record of what happened. `exec` is dropped deliberately: the pipeline needs a
# shell left to read PIPESTATUS, so the script keeps the runner's exit code
# rather than tee's.
RUNTIME_DIRECTORY="${PROJECT_DIRECTORY}/.run"
mkdir -p "${RUNTIME_DIRECTORY}"
LOG_FILE="${RUNTIME_DIRECTORY}/ingestion.log"
{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) run_ingestion $*"
} >>"${LOG_FILE}"
set -o pipefail
PI_INGEST_METRICS_PUSHGATEWAY_URL="${PI_INGEST_METRICS_PUSHGATEWAY_URL:-http://127.0.0.1:9091}" \
  .venv/bin/python -u -m scripts.run_ingestion "$@" 2>&1 | tee -a "${LOG_FILE}"
status="${PIPESTATUS[0]}"
echo "=== exit ${status}" >>"${LOG_FILE}"
if (( status != 0 )); then
  echo "Ingestion failed. Full output: ${LOG_FILE}" >&2
fi
exit "${status}"
