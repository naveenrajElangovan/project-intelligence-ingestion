#!/usr/bin/env bash
set -euo pipefail

# One entry point for "forget everything Confluence and index it again".
#
# It exists because the three-step version was easy to get wrong in two ways:
# the purge step was run on the system interpreter, where no dependency is
# installed, and the stack launcher aborted the whole sequence when the running
# RAG was older than the code on disk. Both are handled here.
#
#   ./scripts/run_confluence_reset.sh                 # plan only, deletes nothing
#   ./scripts/run_confluence_reset.sh --yes           # purge, then full re-index
#   ./scripts/run_confluence_reset.sh --yes --project OTHER

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
BACKEND_DIRECTORY="$(cd "${PROJECT_DIRECTORY}/../project-intelligence-backend" && pwd)"
PYTHON="${PROJECT_DIRECTORY}/.venv/bin/python"

PROJECT="DEMO"
CONFIRMED=false
while (($#)); do
  case "$1" in
    --yes) CONFIRMED=true; shift ;;
    --project) PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --project=*) PROJECT="${1#*=}"; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -x "${PYTHON}" ]]; then
  echo "The ingestion virtual environment is missing at .venv." >&2
  exit 1
fi
if [[ ! -f "${PROJECT_DIRECTORY}/.env" ]]; then
  echo "${PROJECT_DIRECTORY}/.env is missing. Configure ingestion before running it." >&2
  exit 1
fi

# Idempotent, and now self-healing: it restarts a stale RAG or backend rather
# than refusing, so this sequence is no longer interrupted halfway.
"${BACKEND_DIRECTORY}/scripts/prepare_and_start_local_app.sh"

cd "${PROJECT_DIRECTORY}"

# Preflight before anything destructive. Purging and then failing to re-index
# leaves the collection empty, which is strictly worse than not starting: the
# previous run deleted the Confluence vectors and then stopped on a missing
# Atlassian gateway, so the corpus went from stale to absent.
echo "== Preflight"
"${PYTHON}" - "${PROJECT}" <<'PREFLIGHT'
import asyncio, sys

from app.config import get_settings
from app.control_plane import BackendControlPlaneClient

project_id = sys.argv[1]
project = asyncio.run(BackendControlPlaneClient(get_settings()).get(project_id))
if project is None:
    raise SystemExit(f"Project {project_id} is not configured in the control plane.")
if not project.confluence_spaces:
    raise SystemExit(f"Project {project_id} has no Confluence space mapping.")
if project.atlassian is None:
    raise SystemExit(
        "No Atlassian gateway for this project: integration_connections has no "
        "ATLASSIAN row, so cloudId and resourceUrl are unknown and Confluence "
        "cannot be read.\n"
        "Create it once, then re-run this script:\n"
        "  cd ../project-intelligence-backend && .venv/bin/python -m "
        "scripts.start_atlassian_connect --project " + project_id +
        " --user-object-id <entra oid> --tenant-id <entra tid>"
    )
print(f"Atlassian gateway present (cloud {project.atlassian.cloud_id}).")
PREFLIGHT

echo "== Purge plan (nothing is deleted yet)"
PI_PURGE_PLAN_ONLY_NOTICE="$([[ "${CONFIRMED}" == "true" ]] && echo false || echo true)" \
  "${PYTHON}" -m scripts.purge_provider --project "${PROJECT}" --provider CONFLUENCE

if [[ "${CONFIRMED}" != "true" ]]; then
  echo
  echo "Plan only. Re-run with --yes to purge and re-index."
  exit 0
fi

echo
echo "== Purging Confluence state for ${PROJECT}"
"${PYTHON}" -m scripts.purge_provider --project "${PROJECT}" --provider CONFLUENCE --yes

echo
echo "== Full Confluence ingestion for ${PROJECT}"
# Vectors and manifests are both gone, so --full here is a first-time index
# rather than a reconciliation.
"${PYTHON}" -m scripts.run_ingestion --project "${PROJECT}" --provider CONFLUENCE --full

echo
echo "== Chroma chunk audit"
TARGET="$("${PYTHON}" - "${PROJECT}" <<'RESOLVE'
import asyncio, sys
from app.config import get_settings
from app.control_plane import BackendControlPlaneClient

project = asyncio.run(BackendControlPlaneClient(get_settings()).get(sys.argv[1]))
if project is None:
    raise SystemExit(f"Project {sys.argv[1]} is not configured in the control plane.")
print(project.vector_store.collection_name)
RESOLVE
)"
"${PYTHON}" -m scripts.audit_chunk_quality \
  --project "${PROJECT}" \
  --collection "${TARGET}" \
  --provider CONFLUENCE
