# Local development environment

The maintained local deployment is **development**. Backend, ingestion, RAG,
and the desktop client use the same backend on `http://127.0.0.1:8001`.
There is no separate running production deployment. The backend's default
`docker-compose.yml` explicitly selects development for its application services.

Each application project has one active vector collection selected by its backend
project record. Ingestion and retrieval must both follow that mapping. Collection
names containing `stage` describe an earlier evaluation snapshot; they do not
create a second application environment. Once selected as the active development
collection, that collection is mutable development data. Keep old collections as
rollback snapshots until their deletion is explicitly requested.

Use the backend repository's `scripts/prepare_and_start_local_app.sh` for a code
rebuild and startup. After a machine restart, start Docker Desktop and the RAG
repository's `scripts/start_accelerator_macos.sh` if the configured host accelerator
is enabled. Existing containers retain their configuration. Never commit `.env`,
provider credentials, model files, local databases, or authentication caches.

Development still enforces project membership, source access policies, and
citations. Activating an index for development does not certify unfinished RAG
quality gates as passed. Historical production/release templates and evaluation
utilities are retained for reference, but are not additional running environments.
