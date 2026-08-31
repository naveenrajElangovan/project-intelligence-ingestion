# Project Intelligence Ingestion

Independent secure ingestion data plane for GitHub, Jira, Confluence, and structured attachments.
It uses a sandboxed local Docling worker, structure-aware LangChain splitting, LangGraph workflow
execution, Azure Table versioned manifests, and Chroma integrated multilingual embeddings.

It does not authenticate mobile users, authorize project access, query Azure SQL directly, perform
RAG retrieval, or call an LLM.

## Required execution order

```text
1. Backend control plane healthy
2. Verify project/source/Atlassian mapping through backend internal API
3. Ingestion HTTP service or job starts
4. Full bootstrap once, then incremental runs
5. RAG reads the resulting Chroma records later
```

Backend must be running first because ingestion gets all project-specific configuration from the
backend and uses its Atlassian gateway. RAG is not required for ingestion.

## Data ownership

- Backend/Azure SQL: projects, source mappings, schedules, Chroma routing, and provider connection metadata.
- Backend/Key Vault or local encrypted store: Atlassian access and refresh tokens.
- Ingestion/Azure Table: source versions, hashes, scan markers, leases, and delta cursors.
- Ingestion/Chroma: searchable chunks and embeddings.
- Ingestion process memory: transient source bodies before chunking/upsert.

Source bodies are never stored in Azure SQL or Azure Table. Each Chroma record contains
`project_id` and `access_policy_id=project:<projectId>` for RAG filtering.

## Incremental flow

```text
backend project/source mapping
  -> provider cursor or GitHub tree/blob SHA
  -> compare Azure Table manifest
  -> unchanged: update scan marker and skip embedding
  -> changed/forced: security scan, Docling/structure parse, and tokenizer-aware LangChain split
  -> LangGraph write step replaces only that source in Chroma
  -> commit manifest/cursor only after successful vector writes
  -> full reconciliation removes vectors for deleted sources
```

GitHub merged-PR webhooks process changed/deleted files. Daily and manual runs process GitHub,
Jira, and Confluence together unless a provider is explicitly selected for diagnosis. Routine runs
are incremental; `--full` is for bootstrap and reconciliation.

## Documentation

- [Enterprise security, privacy, and AI data architecture](../project-intelligence-backend/PROJECT_INTELLIGENCE_SECURITY_ARCHITECTURE.md)
- [Current ingestion architecture](docs/CURRENT_ARCHITECTURE.md)
- [Current backend architecture](../project-intelligence-backend/docs/CURRENT_ARCHITECTURE.md)
- [Current RAG architecture](../project-intelligence-rag/docs/CURRENT_ARCHITECTURE.md)
- [Executable ingestion labs](labs/README.md)

## Configuration ownership

All ingestion settings use the `PI_INGEST_` prefix. Put development values only in:

```text
<workspace>/project-intelligence-ingestion/.env
```

Infrastructure settings/secrets include backend control-plane URL/key, GitHub App identity,
webhook secret, Chroma key, Azure Table endpoint/name/identity, size limits, cursor overlap, and
chunking parameters.

Do not put project IDs, repository names, branches, Jira keys, Confluence spaces, Chroma hosts or
namespaces, or schedules in this file. Those are returned by backend from Azure SQL.

The paired development key must match:

```text
ingestion PI_INGEST_CONTROL_PLANE_API_KEY
       ==
backend   PI_INGESTION_INTERNAL_API_KEY
```

`.env.example` and `.env.production.example` use the same key names. Production injects the same
contract through the deployment platform/Key Vault and uses managed identity where supported.

# Development execution

## Recommended single command

After both repositories have their `.env` files and virtual environments configured once, run:

```bash
cd <workspace>/project-intelligence-ingestion
./scripts/run_unattended_ingestion.sh
```

This is the supported local entry point for a normal all-provider incremental run. It requires no
manual health checks: the launcher calls the Backend's unified macOS startup script, which validates
Azure SQL identity and firewall access, starts or reuses MongoDB and RAG, starts Backend when it is
down, restores the encrypted local Atlassian credential store when switching from Docker to native
Backend, and waits for Backend `/ready`. Ingestion starts only after that command succeeds.

With no arguments the launcher runs project `DEMO` across GitHub, Jira, and Confluence. It also
forwards normal ingestion options while supplying the current local project default:

```bash
./scripts/run_unattended_ingestion.sh --full
./scripts/run_unattended_ingestion.sh --provider JIRA
./scripts/run_unattended_ingestion.sh --project OTHER_PROJECT
```

`--full` is only for initial bootstrap or explicit reconciliation. Routine executions should use
the no-argument incremental command.

## Optional manual two-terminal workflow

Use this only for component diagnosis. Backend starts first.

## Terminal 1: start and verify backend

```bash
cd <workspace>/project-intelligence-backend
./scripts/start_dev_api.sh

curl --fail-with-body http://localhost:8001/health
docker compose ps
docker compose logs --tail=150 api
```

Do not continue until backend health succeeds. Full backend setup is in the
[backend README](../project-intelligence-backend/README.md#development-execution).

## Terminal 2: create the ingestion environment

```bash
cd <workspace>/project-intelligence-ingestion
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
test -f .env || cp .env.example .env
```

Update `.env` with real development infrastructure values/secrets. Never commit it.

For local development, authenticate Azure CLI once so `DefaultAzureCredential` can access Azure
Table from the host process. The cached session is reused by later ingestion runs; this command is
not required before every run:

```bash
az login --tenant 11111111-1111-4111-8111-111111111111
az account show --output table
```

The signed-in developer identity needs `Storage Table Data Contributor` on only the
`piingestionstate` storage account. The backend uses its own Azure SQL authentication separately.

Production and scheduled Azure runs do not use developer login. Attach the existing user-assigned
managed identity `pi-ingestion-runtime` (client ID
`44444444-4444-4444-8444-444444444444`) to the ingestion Container App/Job and set
`PI_INGEST_STATE_MANAGED_IDENTITY_CLIENT_ID` to that client ID. The identity already has
`Storage Table Data Contributor` scoped only to the `piingestionstate` storage account, so
`DefaultAzureCredential` obtains tokens without a secret or interactive authentication.

For a fully unattended process running outside Azure, use a dedicated service principal with a
certificate or workload-identity federation and inject its standard `AZURE_*` variables through
the host/CI secret store. Do not reuse the Entra frontend application secret and do not commit the
credential to this repository's `.env` files.

## Run ingestion tests

```bash
cd <workspace>/project-intelligence-ingestion
.venv/bin/python -m pytest -q
```

## Create or verify empty infrastructure surfaces

These commands are idempotent and do not ingest source records:

```bash
cd <workspace>/project-intelligence-ingestion
.venv/bin/python -m scripts.create_state_table
# The configured Chroma collection is created automatically on first ingestion.
```

Use them only after the ingestion `.env` contains the real Azure Table endpoint and Chroma key.

## Verify dependencies before ingestion

```bash
cd <workspace>/project-intelligence-ingestion

.venv/bin/python -m scripts.preflight control-plane
.venv/bin/python -m scripts.preflight table
.venv/bin/python -m scripts.preflight github
.venv/bin/python -m scripts.preflight chroma
.venv/bin/python -m scripts.preflight blob
.venv/bin/python -m scripts.preflight visual-models
```

The control-plane result must show the expected project and `atlassian_connected=True` for Jira and
Confluence ingestion.

You can also inspect the mapping without exposing secrets:

```bash
set -a
source .env
set +a
curl --fail-with-body --silent --show-error \
  http://localhost:8001/v1/internal/ingestion/projects/DEMO \
  -H "Authorization: Bearer $PI_INGEST_CONTROL_PLANE_API_KEY" \
  | python3 -m json.tool
```

## Start the ingestion HTTP service

Required for GitHub webhooks and manual HTTP triggers. A direct CLI run can operate without this
server.

```bash
cd <workspace>/project-intelligence-ingestion
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

In another terminal:

```bash
curl --fail-with-body http://localhost:8002/health
curl --fail-with-body http://localhost:8002/ready
```

`/health` verifies the ingestion process. `/ready` also verifies backend control-plane access.

## First real bootstrap

Run once for a new project/index, or for an explicit full reconciliation:

```bash
cd <workspace>/project-intelligence-ingestion
.venv/bin/python -m scripts.run_ingestion --project DEMO --full
```

This writes real configured GitHub, Jira, and Confluence content to Chroma and creates real
manifests/cursors in Azure Table. It does not insert project or user rows into Azure SQL.

## Normal incremental run

```bash
./scripts/run_unattended_ingestion.sh
```

This is the normal manual/daily command. It starts or reuses Backend dependencies, requests all
three providers, and embeds only changes.

## Provider-specific diagnostic runs

```bash
.venv/bin/python -m scripts.run_ingestion --project DEMO --provider GITHUB
.venv/bin/python -m scripts.run_ingestion --project DEMO --provider JIRA
.venv/bin/python -m scripts.run_ingestion --project DEMO --provider CONFLUENCE
```

Use these for diagnosis/recovery, not as the normal all-provider schedule.

## Manual HTTP trigger

Development loopback may omit the internal trigger key according to current development policy:

```bash
curl --fail-with-body \
  http://localhost:8002/v1/projects/DEMO/ingestions \
  -H 'Content-Type: application/json' \
  -d '{"providers":["GITHUB","JIRA","CONFLUENCE"],"full":false}'
```

Production always requires the ingestion internal service credential/private identity.

## Development logs and shutdown

- Ingestion foreground logs appear in Terminal 2.
- Backend logs: `docker compose logs -f api` from the backend repository.
- Stop ingestion with `Ctrl+C`.
- Stop backend/RAG with `docker compose down` from the backend repository.

Stopping local processes does not remove Azure Table state or Chroma vectors.

# Production execution

Production normally deploys the same ingestion image in three modes:

1. HTTPS webhook/manual-trigger service running the Dockerfile default command;
2. scheduled Azure Container Apps Job overriding the command with `scripts.run_ingestion`;
3. optional queue worker for merged PRs and large partitions.

## 1. Production prerequisites

- Backend is deployed and healthy on a private control-plane URL.
- `pi-ingestion-runtime` has `Storage Table Data Contributor` only on `piingestionstate`.
- GitHub App is installed, permissions are approved, and the stable webhook URL is configured.
- Atlassian connection exists in backend and refresh-token automation is healthy.
- Chroma index exists and project routing is stored in Azure SQL through backend.
- Production secrets are in Key Vault/deployment secret references.
- Ingestion image is scanned, non-root, immutable, and pushed to the registry.

## 2. Production configuration

Inject every key from `.env.production.example`; do not create a second variable naming scheme.
Important production values:

- `PI_INGEST_ENVIRONMENT=production`;
- exact `PI_INGEST_ALLOWED_HOSTS`, HTTPS on, API docs off;
- private `PI_INGEST_CONTROL_PLANE_URL`;
- transitional control-plane key or managed-identity workload authentication;
- GitHub App ID/private key and webhook secret from Key Vault;
- Chroma write key from Key Vault;
- Azure Table endpoint/name and `pi-ingestion-runtime` client ID;
- internal manual-trigger credential of at least 32 characters.

Project/source mappings still come from backend and must not be copied into production variables.

## 3. Build the production image

From this repository:

```bash
cd <workspace>/project-intelligence-ingestion
docker build \
  --tag <registry>/project-intelligence-ingestion:<immutable-release-tag> \
  .
```

Push it through the approved deployment pipeline. The backend production Compose file can also
build all three sibling images for a single-host validation.

## 4. Deploy only after backend is ready

Deployment order:

1. Deploy RAG if required by the application.
2. Deploy backend and verify its health/internal project endpoint.
3. Deploy ingestion webhook service and verify `/health` and `/ready`.
4. Deploy the scheduled job/queue worker using the same image/environment.
5. Enable the GitHub webhook and schedule only after readiness passes.

Production webhook endpoint:

```text
POST https://<ingestion-host>/v1/webhooks/github
```

The webhook handler should validate HMAC/delivery ID, enqueue work, and return quickly. Large
ingestion must not execute synchronously in the public webhook request.

## 5. Scheduled and manual job commands

The container working directory is `/app`. Override the image command as follows.

First bootstrap/reconciliation:

```bash
python -m scripts.run_ingestion --project DEMO --full
```

Normal daily incremental job:

```bash
python -m scripts.run_ingestion --project DEMO
```

Schedule the incremental command at project-configured midnight. Azure Container Apps cron uses
UTC, so convert `America/Mexico_City` using timezone rules rather than hard-coding a permanent UTC
offset.

Provider-only recovery:

```bash
python -m scripts.run_ingestion --project DEMO --provider GITHUB
python -m scripts.run_ingestion --project DEMO --provider JIRA
python -m scripts.run_ingestion --project DEMO --provider CONFLUENCE
```

## 6. Production verification

Before enabling the recurring schedule, confirm:

- `/health` and `/ready` succeed;
- control-plane mapping returns the expected sources and Atlassian connection;
- GitHub webhook ping/delivery returns success;
- full bootstrap writes the expected namespace and Azure Table manifests;
- a second incremental run reports unchanged content and no unnecessary chunk writes;
- changed Jira/Confluence content updates only affected documents;
- a merged PR updates/deletes only affected GitHub files;
- failed provider/vector writes do not advance the cursor;
- no source bodies, OAuth tokens, private keys, or API keys appear in logs.

## 7. Production operation and rollback

- Monitor discovered/indexed/unchanged/deleted/failed/chunk counts and cursor age.
- Respect provider rate limits and dead-letter repeated failures.
- Pause schedules/webhooks before backend control-plane maintenance.
- Roll back to the previous immutable ingestion image when application code fails.
- Preserve Azure Table state during image rollback.
- For an incompatible embedding migration, switch to the documented prior Chroma collection rather
  than deleting the only populated namespace.
- Run `--full` only when an intentional reconciliation or migration requires it.
