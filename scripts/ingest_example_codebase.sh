#!/usr/bin/env bash
set -euo pipefail

# Index all local Kotlin applications and libraries as CODE evidence for DEMO.
# Run after the Chroma container and ingestion service are available.
ROOT="<workspace>/Example"
PROJECT="${1:-DEMO}"
REPOSITORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$REPOSITORY/.venv/bin/python}"
for name in auth-kotlin-lib inventory-demo-app-kotlin design-toolkit-kotlin-lib navigation-kotlin-lib odm-kotlin-lib checkout-demo-app-kotlin store-core-kotlin store-lib-payment-codi-kotlin ui-tools-kotlin-lib; do
  "$PYTHON" -m scripts.ingest_local_repository --project "$PROJECT" --path "$ROOT/$name" --name "$name" --branch local --full
done
