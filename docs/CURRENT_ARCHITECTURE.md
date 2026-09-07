# Project Intelligence Ingestion — Current Architecture

This document presents the production architecture of the Project Intelligence ingestion platform. It explains how approved GitHub, Jira, Confluence, and attachment content moves from source discovery through security inspection, parsing, chunking, embedding, vector persistence, and operational state management.

The architecture is built around four principles: the backend remains the authority for project mappings and Atlassian credentials; ingestion is the only writer to the retrieval corpus; vector writes complete before operational state is committed; and partial or failed scans never become evidence of deletion.

### Diagram color guide

- **Blue** identifies API boundaries, entry points, and externally initiated work.
- **Purple** identifies ingestion processing and transformation stages.
- **Green** identifies successful outputs, committed state, and persisted data.
- **Amber** identifies decisions, controls, and conditional paths.
- **Orange** identifies external providers and dependent platforms.
- **Red** identifies rejection, failure, quarantine, and retry outcomes.

Every diagram uses a transparent canvas and pure-white text for labels, connectors, notes, messages, and entity attributes. Saturated color fills preserve contrast in dark-mode Markdown viewers.

## 1. Deployment / system context

This diagram establishes the runtime boundary of the ingestion platform. A lightweight HTTP image receives health checks, manual ingestion requests, and verified GitHub webhooks, while a separate worker image contains the heavier parsing, OCR, malware-scanning, and pinned-model dependencies.

The ingestion service retrieves project configuration from the backend control plane, accesses GitHub with installation tokens, reaches Atlassian only through the backend proxy, stores operational state in Azure Table, and writes project-isolated records to Chroma. Queueing decouples webhook admission from document processing when Azure Service Bus is configured.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#2563EB","primaryTextColor":"#FFFFFF","primaryBorderColor":"#2563EB","lineColor":"#E2E8F0","edgeLabelBackground":"transparent","secondaryColor":"#7C3AED","secondaryTextColor":"#FFFFFF","tertiaryColor":"#059669","tertiaryTextColor":"#FFFFFF","clusterBkg":"#1F2937","clusterBorder":"#94A3B8","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .edgeLabel { color: #FFFFFF !important; background-color: transparent !important; }"}}%%
flowchart TB
    subgraph Clients
        GHW["GitHub App<br/>webhook: merged PR"]
        OPS["Operator / scheduler<br/>manual trigger"]
    end

    subgraph API_Image["HTTP image (Dockerfile) — requirements.txt, CPU torch, no Docling/OCR/ClamAV"]
        MAIN["app/main.py<br/>FastAPI"]
        R1["GET /health"]
        R2["GET /ready"]
        R3["POST /v1/webhooks/github"]
        R4["POST /v1/projects/{id}/ingestions"]
        MW["SecurityHeaders + RequestSizeLimit<br/>webhook_max_body_bytes = 2 MB"]
        SVC["app/service.py<br/>IngestionService"]
    end

    subgraph Worker_Image["Worker image (Dockerfile.worker) — Docling, Tesseract, ClamAV, pinned models"]
        RW["scripts/run_worker.py"]
        VER["verify_model_artifacts<br/>/opt/model-checksums.json"]
        ENVOFF["HF_HUB_OFFLINE=1<br/>TRANSFORMERS_OFFLINE=1<br/>DOCLING_ARTIFACTS_PATH=/opt/docling-models"]
    end

    SB[("Azure Service Bus<br/>queue: pi-document-parsing")]
    CP["Backend control plane<br/>PI_INGEST_CONTROL_PLANE_URL"]
    ATL["api.atlassian.com<br/>Jira + Confluence"]
    GHAPI["api.github.com"]
    TBL[("Azure Table<br/>piingestionstate")]
    CHR[("Chroma HttpClient<br/>chroma_host:chroma_port")]

    GHW --> MW --> MAIN
    OPS --> MW
    MAIN --> R1 & R2 & R3 & R4
    R3 --> SVC
    R4 --> SVC
    R3 -. "enqueue IngestionJob<br/>when service_bus_namespace set" .-> SB
    SB --> RW --> SVC
    RW --> VER
    SVC --> CP
    CP -->|"token held by backend"| ATL
    SVC -->|"installation token"| GHAPI
    SVC --> TBL
    SVC --> CHR

    classDef entry fill:#2563EB,stroke:#2563EB,color:#FFFFFF,stroke-width:2px;
    classDef service fill:#7C3AED,stroke:#7C3AED,color:#FFFFFF,stroke-width:2px;
    classDef external fill:#C2410C,stroke:#EA580C,color:#FFFFFF,stroke-width:2px;
    classDef data fill:#059669,stroke:#059669,color:#FFFFFF,stroke-width:2px;
    class GHW,OPS entry;
    class MAIN,R1,R2,R3,R4,MW,SVC,RW,VER,ENVOFF service;
    class CP,ATL,GHAPI external;
    class SB,TBL,CHR data;
```

**Presentation takeaway:** the split-image design keeps the public HTTP surface small while concentrating expensive and security-sensitive document processing in an isolated worker runtime.

## 2. Scope orchestration

This diagram shows how one project/provider scope is coordinated from configuration lookup through lease release. Each scope is protected by a renewable lease, uses an overlap-adjusted cursor for incremental reads, and processes each discovered document through the same bounded workflow.

Cursor advancement and deletion reconciliation are deliberately conservative. Any failure prevents both actions, and a full scan must also meet the deletion safety floor before missing manifests can be treated as deleted.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#2563EB","primaryTextColor":"#FFFFFF","primaryBorderColor":"#2563EB","lineColor":"#E2E8F0","edgeLabelBackground":"transparent","secondaryColor":"#7C3AED","secondaryTextColor":"#FFFFFF","tertiaryColor":"#059669","tertiaryTextColor":"#FFFFFF","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .edgeLabel { color: #FFFFFF !important; background-color: transparent !important; }"}}%%
flowchart TD
    A["ingest_project(projectId, providers, full)"] --> B["control plane GET<br/>/v1/internal/ingestion/projects/{id}"]
    B -->|404| B404["return: project not found"]
    B --> C["build scopes<br/>project|provider|scope"]
    C --> D{"acquire lease<br/>__lease__, scope_lease_seconds=900"}
    D -->|"conflict 409 / valid owner"| D2["skip scope"]
    D -->|"acquired / expired etag takeover"| E{"full run?"}
    E -->|no| F["cursor = get_cursor() − incremental_overlap_minutes(5)"]
    E -->|yes| G["cursor = None (full scan)"]
    F --> H["provider document iterator"]
    G --> H
    H --> I["per document: LangGraph workflow"]
    I --> J{"any failure?<br/>max_document_failures_per_scope=5"}
    J -->|yes| K["do NOT advance cursor<br/>do NOT reconcile deletions"]
    J -->|no| L{"full scan?"}
    L -->|yes| M{"discovered >= deletion_floor_documents?"}
    M -->|no| K
    M -->|yes| N["delete manifests where last_seen_run != scan_id"]
    L -->|no| O["advance cursor"]
    N --> O
    O --> P["rebuild __vocabulary__ record"]
    P --> Q["release lease"]
    K --> Q

    classDef entry fill:#2563EB,stroke:#2563EB,color:#FFFFFF,stroke-width:2px;
    classDef process fill:#7C3AED,stroke:#7C3AED,color:#FFFFFF,stroke-width:2px;
    classDef decision fill:#B45309,stroke:#D97706,color:#FFFFFF,stroke-width:2px;
    classDef success fill:#059669,stroke:#059669,color:#FFFFFF,stroke-width:2px;
    classDef stop fill:#DC2626,stroke:#DC2626,color:#FFFFFF,stroke-width:2px;
    class A,B,C entry;
    class F,G,H,I,N,O,P,Q process;
    class D,E,J,L,M decision;
    class D2,B404,K stop;
```

**Presentation takeaway:** a scope advances only when discovery and document processing are clean, making retries safe and preventing accidental mass deletion.

## 3. Document workflow state machine

Every source document travels through a small LangGraph state machine. The inspect state determines whether the document can be skipped, must be deleted, or needs complete reprocessing; changed content then moves through analysis, splitting, vector writing, and finally state commit.

The ordering is the transaction boundary: Chroma is updated and verified before the manifest is saved. A failure in security analysis, parsing, embedding, or vector persistence therefore leaves the prior committed manifest intact and makes the document eligible for a safe retry.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#7C3AED","primaryTextColor":"#FFFFFF","primaryBorderColor":"#7C3AED","lineColor":"#E2E8F0","secondaryColor":"#2563EB","secondaryTextColor":"#FFFFFF","tertiaryColor":"#059669","tertiaryTextColor":"#FFFFFF","noteBkgColor":"#B45309","noteBorderColor":"#D97706","noteTextColor":"#FFFFFF","labelColor":"#FFFFFF","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .stateLabel { color: #FFFFFF !important; fill: #FFFFFF !important; } .transition text, .edgeLabel text { fill: #FFFFFF !important; } .transition { color: #FFFFFF !important; }"}}%%
stateDiagram-v2
    [*] --> inspect
    inspect --> commit : SKIP (all 6 versions match)
    inspect --> write : DELETE (source vanished)
    inspect --> analyze : INDEX (new / changed / forced)
    analyze --> split
    split --> write
    write --> commit
    commit --> [*]

    note right of inspect
      compares manifest vs document:
      version, content_hash,
      parser_version (docling-visual-v2),
      chunker_version (semantic-token-entity-metadata-v9),
      embedding_profile, schema_version (3)
    end note

    note right of analyze
      ContentSecurityScanner.inspect()
      -> credential_sensitive flag
      Docling analyze under
      docling_timeout_seconds=300
      docling_max_pages=500
      docling_max_concurrency=2
    end note

    note right of write
      1 query prior storage ids
      2 embed passages
      3 upsert
      4 verify by id
      5 delete prior_ids - new_ids
    end note

    note right of commit
      ONLY after Chroma succeeds:
      save manifest / mark deleted /
      touch last_seen_run = scan_id
    end note
```

**Presentation takeaway:** the workflow behaves like a controlled two-system transaction—vector state first, operational state second.

## 4. Chunking decision tree

This decision tree explains how documents are routed into format- and domain-specific chunkers. Code, Jira issues, entity contracts, registries, workflows, glossaries, Markdown, tables, HTML, structured logs, and plain text each use a strategy designed to preserve their most useful semantic boundaries.

All routes converge on the same token safety guard, within-source deduplication, metadata enrichment, and identity generation. Each chunk retains verbatim evidence for citation while creating a separate contextual `embedding_text` for retrieval quality.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#7C3AED","primaryTextColor":"#FFFFFF","primaryBorderColor":"#7C3AED","lineColor":"#E2E8F0","edgeLabelBackground":"transparent","secondaryColor":"#2563EB","secondaryTextColor":"#FFFFFF","tertiaryColor":"#059669","tertiaryTextColor":"#FFFFFF","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .edgeLabel { color: #FFFFFF !important; background-color: transparent !important; }"}}%%
flowchart TD
    D["SourceDocument<br/>(content, mime, title, extension)"] --> R{"route"}

    R -->|".java .js .kt .py .ts .tsx"| C1["_code_values()<br/>RecursiveCharacterTextSplitter per language<br/>code_chunk_max_tokens = 350"]
    R -->|"provider JIRA, source_type ISSUE"| C2["_issue_values()<br/>summary / description / comments"]
    R -->|"entity contract markers<br/>bot-rag-01/03/04, ## ENTITY_KEY"| C3["_entity_contract_values()<br/>header + body preserved"]
    R -->|"registry table<br/>bot-rag-02 or rows > 20"| C4["_registry_table_values()<br/>table_chunk_max_tokens = 300<br/>headers repeated per chunk"]
    R -->|"workflow title or [FLOW-N]"| C5["_workflow_values()<br/>numbered steps + part numbers"]
    R -->|"glossary title or ## TERM:"| C6["_glossary_values()<br/>one chunk per term"]
    R -->|".md .markdown"| C7["_markdown_values()<br/>MarkdownHeaderTextSplitter"]
    R -->|"CSV / delimited"| C8["_table_values()"]
    R -->|"HTML"| C9["_html_values()<br/>HTMLSemanticPreservingSplitter"]
    R -->|"JSON / logs"| C10["_line_values()"]
    R -->|"plain text"| C11["_section_values()<br/>chunk_max_tokens = 420<br/>overlap = 40"]

    C1 & C2 & C3 & C4 & C5 & C6 & C7 & C8 & C9 & C10 & C11 --> G["_SharedSentenceTransformerSplitter<br/>hard 512-token bisect guard"]
    G --> DDUP["dedupe identical content hashes<br/>within the same source"]
    DDUP --> META["attach metadata:<br/>doc_category, entity_key, application,<br/>identifiers[], chunk_profile,<br/>visual_asset_types/captions/reason_codes"]
    META --> TXT["build two texts"]
    TXT --> T1["content = verbatim evidence<br/>(stored as Chroma document)"]
    TXT --> T2["embedding_text = hierarchy + reference +<br/>locator + metadata + evidence<br/>(this is what gets embedded)"]
    META --> CID["chunk_id = sha256(<br/>project|provider|source_id|version|<br/>ordinal|digest|chunker_version)"]

    classDef source fill:#2563EB,stroke:#2563EB,color:#FFFFFF,stroke-width:2px;
    classDef decision fill:#B45309,stroke:#D97706,color:#FFFFFF,stroke-width:2px;
    classDef route fill:#7C3AED,stroke:#7C3AED,color:#FFFFFF,stroke-width:1.5px;
    classDef normalize fill:#0F766E,stroke:#0F766E,color:#FFFFFF,stroke-width:2px;
    classDef output fill:#059669,stroke:#059669,color:#FFFFFF,stroke-width:2px;
    class D source;
    class R decision;
    class C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11 route;
    class G,DDUP,META,TXT normalize;
    class T1,T2,CID output;
```

**Presentation takeaway:** retrieval context is enriched without changing the evidence that downstream answers cite.

## 5. Embedding path

This diagram covers local passage embedding and its compatibility controls. The embedding model is loaded once per process from pinned local artifacts, forced into offline and non-remote-code operation, and protected by locks around model loading and inference.

Passage prefixes are normalized, content is encoded in bounded batches, and every vector is dimension-checked before Chroma receives it. Query prefixing belongs to the RAG service, which must use a compatible model and dimension contract.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#2563EB","primaryTextColor":"#FFFFFF","primaryBorderColor":"#2563EB","lineColor":"#E2E8F0","edgeLabelBackground":"transparent","secondaryColor":"#7C3AED","secondaryTextColor":"#FFFFFF","tertiaryColor":"#059669","tertiaryTextColor":"#FFFFFF","clusterBkg":"#1F2937","clusterBorder":"#94A3B8","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .edgeLabel { color: #FFFFFF !important; background-color: transparent !important; }"}}%%
flowchart LR
    subgraph Config
        K1["local_embedding_model = intfloat/multilingual-e5-large"]
        K2["embedding_dimensions = 1024 (384|768|1024 only)"]
        K3["local_embedding_device = cpu"]
        K4["local_embedding_batch_size = 16 (1..64)"]
        K5["local_embedding_path / revision"]
        K6["embedding_position_limit = 512"]
    end

    subgraph Loader["_load_embedding_model (process cache + lock)"]
        L1["SentenceTransformer(<br/>local_files_only=True,<br/>trust_remote_code=False)"]
        L2["model.max_seq_length = 512"]
    end

    IN["chunk.embedding_text[]"] --> P["_ensure_passage_prefix<br/>regex strips repeats,<br/>adds exactly one 'passage: '"]
    P --> B["batch loop<br/>step = batch_size"]
    B --> E["model.encode(<br/>normalize_embeddings=True,<br/>convert_to_numpy=True)<br/>under _INFERENCE_LOCK"]
    E --> V{"len(vector) == embedding_dimensions?"}
    V -->|no| ERR["ValueError:<br/>passage and query vectors must<br/>come from the same model"]
    V -->|yes| OUT["float vectors -> Chroma upsert"]

    Config --> Loader --> E

    Q["'query: ' prefix"] -.->|"owned by RAG service, NOT here"| X["not implemented in this repo"]

    classDef config fill:#2563EB,stroke:#2563EB,color:#FFFFFF,stroke-width:1.5px;
    classDef process fill:#7C3AED,stroke:#7C3AED,color:#FFFFFF,stroke-width:2px;
    classDef decision fill:#B45309,stroke:#D97706,color:#FFFFFF,stroke-width:2px;
    classDef success fill:#059669,stroke:#059669,color:#FFFFFF,stroke-width:2px;
    classDef failure fill:#DC2626,stroke:#DC2626,color:#FFFFFF,stroke-width:2px;
    classDef external fill:#C2410C,stroke:#EA580C,color:#FFFFFF,stroke-width:2px;
    class K1,K2,K3,K4,K5,K6,L1,L2 config;
    class IN,P,B,E process;
    class V decision;
    class OUT success;
    class ERR failure;
    class Q,X external;
```

**Presentation takeaway:** ingestion and retrieval share an explicit embedding contract, while runtime loading remains deterministic and offline.

## 6. Atlassian — Confluence flow

This sequence shows Confluence content moving through the backend-owned Atlassian proxy. Ingestion never handles Atlassian refresh tokens directly; it sends an allowlisted target to the control plane, and the backend performs the authenticated call against the configured cloud tenant.

Pages are paginated, optionally restricted to configured root-page trees, and normalized from storage HTML or Atlas Document Format. Attachments are emitted as separate versioned source documents and remain subject to the downstream size and security gates.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryTextColor":"#FFFFFF","lineColor":"#E2E8F0","actorBkg":"#2563EB","actorBorder":"#2563EB","actorTextColor":"#FFFFFF","actorLineColor":"#2563EB","signalColor":"#E2E8F0","signalTextColor":"#FFFFFF","labelBoxBkgColor":"#7C3AED","labelBoxBorderColor":"#7C3AED","labelTextColor":"#FFFFFF","loopTextColor":"#FFFFFF","noteBkgColor":"#B45309","noteBorderColor":"#D97706","noteTextColor":"#FFFFFF","activationBkgColor":"#059669","activationBorderColor":"#059669","sequenceNumberColor":"#FFFFFF","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .messageText, .loopText, .labelText { fill: #FFFFFF !important; }"}}%%
sequenceDiagram
    autonumber
    participant SVC as IngestionService
    participant AC as AtlassianSourceClient
    participant CP as BackendControlPlaneClient
    participant BE as Backend /atlassian proxy
    participant CF as api.atlassian.com/ex/confluence/{cloudId}

    SVC->>AC: confluence_documents(mapping, updated_since)
    loop until _links.next is absent
        AC->>CP: atlassian_get(target=/wiki/api/v2/pages,<br/>space-id, status=current,<br/>body-format=storage, limit=source_page_size(100))
        CP->>BE: GET /v1/internal/ingestion/projects/{id}/atlassian<br/>Authorization: Bearer CONTROL_PLANE_API_KEY
        BE->>CF: authenticated request (backend owns the token)
        CF-->>BE: pages[] + _links.next
        BE-->>CP: passthrough JSON
        CP-->>AC: httpx.Response (3 retries, backoff 2^n on 5xx/transport)
        opt storage body empty (live doc)
            AC->>CP: same page, body-format=atlas_doc_format
            CP-->>AC: ADF payload
            AC->>AC: _atlas_doc_text() -> Markdown,<br/>tables rendered pipe-delimited
        end
        opt mapping.root_page_ids present
            AC->>AC: walk parent chain; drop pages outside the tree
        end
        AC-->>SVC: SourceDocument(source_type=PAGE)
        AC->>CP: target=/wiki/api/v2/pages/{pageId}/attachments
        AC->>CP: target = downloadLink (absolute-resolved)
        AC-->>SVC: SourceDocument(source_type=ATTACHMENT)<br/>capped at max_attachment_bytes = 25 MB
    end
```

**Presentation takeaway:** the backend remains the credential boundary while ingestion owns pagination, normalization, scope filtering, and attachment discovery.

## 7. Atlassian — Jira flow

This sequence describes incremental Jira discovery. The client builds scope-bound JQL with a five-minute overlap, requests a stable ascending order, and follows Jira’s token-based pagination rather than offset pagination.

Each issue becomes a structured source containing summary, description, and comments. Supported attachments become independent source documents so they can be scanned, versioned, chunked, indexed, retried, or quarantined without rewriting the parent issue unnecessarily.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryTextColor":"#FFFFFF","lineColor":"#E2E8F0","actorBkg":"#2563EB","actorBorder":"#2563EB","actorTextColor":"#FFFFFF","actorLineColor":"#2563EB","signalColor":"#E2E8F0","signalTextColor":"#FFFFFF","labelBoxBkgColor":"#7C3AED","labelBoxBorderColor":"#7C3AED","labelTextColor":"#FFFFFF","loopTextColor":"#FFFFFF","noteBkgColor":"#B45309","noteBorderColor":"#D97706","noteTextColor":"#FFFFFF","activationBkgColor":"#059669","activationBorderColor":"#059669","sequenceNumberColor":"#FFFFFF","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .messageText, .loopText, .labelText { fill: #FFFFFF !important; }"}}%%
sequenceDiagram
    autonumber
    participant SVC as IngestionService
    participant AC as AtlassianSourceClient
    participant BE as Backend /atlassian proxy
    participant JR as api.atlassian.com/ex/jira/{cloudId}

    SVC->>AC: jira_documents(mapping, updated_since)
    AC->>AC: build JQL<br/>project = "KEY"<br/>[+ updated >= "YYYY-MM-DD HH:MM" (cursor - 5 min)]<br/>ORDER BY updated ASC, key ASC
    loop while nextPageToken
        AC->>BE: target=/rest/api/3/search/jql<br/>maxResults=source_page_size(100)<br/>fields=summary,description,issuetype,status,priority,<br/>assignee,reporter,created,updated,resolutiondate,<br/>duedate,labels,components,parent,comment,attachment
        BE->>JR: authenticated request
        JR-->>BE: issues[] + nextPageToken
        BE-->>AC: passthrough JSON
        AC->>AC: _jira_issue() -> source_id "issue:{id|key}"<br/>body: ## Issue summary / ## Description / ## Comments
        AC-->>SVC: SourceDocument(source_type=ISSUE)
        loop fields.attachment[]
            AC->>BE: target = attachment.content
            AC-->>SVC: SourceDocument(source_type=ATTACHMENT)
        end
    end
    Note over AC,JR: pagination is token-based (nextPageToken),<br/>not startAt offsets
```

**Presentation takeaway:** overlap plus stable token pagination favors completeness at cursor boundaries, while manifest checks remove duplicate processing.

## 8. GitHub authentication and discovery

This sequence presents the GitHub App trust flow and the two discovery modes. The client signs a short-lived JWT, resolves the repository installation, exchanges it for an installation token, and caches that token only within its safe validity window.

Full scans enumerate a complete branch tree and reject truncated responses. Merged-PR runs inspect only changed paths. Both modes enforce size, include/exclude, sensitive-path, and extension controls before downloading content; Markdown image references are limited to safe repository-relative assets.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryTextColor":"#FFFFFF","lineColor":"#E2E8F0","actorBkg":"#2563EB","actorBorder":"#2563EB","actorTextColor":"#FFFFFF","actorLineColor":"#2563EB","signalColor":"#E2E8F0","signalTextColor":"#FFFFFF","labelBoxBkgColor":"#7C3AED","labelBoxBorderColor":"#7C3AED","labelTextColor":"#FFFFFF","loopTextColor":"#FFFFFF","noteBkgColor":"#B45309","noteBorderColor":"#D97706","noteTextColor":"#FFFFFF","activationBkgColor":"#059669","activationBorderColor":"#059669","sequenceNumberColor":"#FFFFFF","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .messageText, .loopText, .labelText { fill: #FFFFFF !important; }"}}%%
sequenceDiagram
    autonumber
    participant SVC as GitHubSourceClient
    participant GH as api.github.com

    SVC->>SVC: sign RS256 JWT<br/>iat = now-30s, exp = now+9min, iss = github_app_id<br/>(key from github_private_key_base64)
    SVC->>GH: GET /repos/{owner}/{repo}/installation (Bearer JWT)
    GH-->>SVC: installation_id
    SVC->>GH: POST /app/installations/{id}/access_tokens
    GH-->>SVC: installation token (cached ~50 min)

    alt full scan
        SVC->>GH: GET /repos/{o}/{r}/branches/{branch} -> head sha
        SVC->>GH: GET /repos/{o}/{r}/git/trees/{sha}?recursive=1
        GH-->>SVC: tree (error raised if truncated)
    else merged-PR incremental
        SVC->>GH: GET /repos/{o}/{r}/pulls/{n}/files?per_page=100&page=N
        GH-->>SVC: added | modified | removed | renamed + previous_path
    end

    SVC->>SVC: filter: size <= github_max_file_bytes (1 MB)<br/>includePaths / excludePaths globs<br/>block .env* .pem .key .p12 .pfx<br/>block node_modules vendor .gradle build dist secrets
    alt .pdf .docx .pptx .xlsx
        SVC->>GH: blob API (base64 binary)
    else text
        SVC->>GH: GET /repos/.../contents/{path}?ref={commitSha}
    end
    opt .md files
        SVC->>SVC: resolve_markdown_asset_paths()<br/>repo-relative only; external URLs -> UNSAFE_MARKDOWN_IMAGE_REFERENCE
    end
```

**Presentation takeaway:** GitHub access is repository-scoped and short-lived, while discovery is optimized for freshness without weakening full-scan correctness.

## 9. Webhook admission path

This decision path shows every gate applied before a GitHub webhook can trigger ingestion. The API limits request size, validates the SHA-256 HMAC in constant time, accepts only merged pull-request events, resolves the repository through the control plane, and confirms the target branch and project setting.

When Service Bus is configured, the endpoint queues an identifier-only job and returns immediately. The synchronous path is retained for environments without the queue, but it follows the same project and branch admission rules.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#2563EB","primaryTextColor":"#FFFFFF","primaryBorderColor":"#2563EB","lineColor":"#E2E8F0","edgeLabelBackground":"transparent","secondaryColor":"#7C3AED","secondaryTextColor":"#FFFFFF","tertiaryColor":"#059669","tertiaryTextColor":"#FFFFFF","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .edgeLabel { color: #FFFFFF !important; background-color: transparent !important; }"}}%%
flowchart TD
    W["POST /v1/webhooks/github"] --> S1{"body <= 2 MB?"}
    S1 -->|no| E413["413"]
    S1 --> S2{"X-Hub-Signature-256 ==<br/>sha256 HMAC(secret, body)?<br/>compare_digest"}
    S2 -->|no| E401["401"]
    S2 --> S3{"X-GitHub-Event == pull_request?"}
    S3 -->|no| IGN["accepted=false, reason"]
    S3 --> S4{"action == closed AND merged == true?"}
    S4 -->|no| IGN
    S4 --> S5["control plane<br/>GET /v1/internal/ingestion/github-project?owner&repository"]
    S5 -->|404| IGN
    S5 --> S6{"branch in indexedBranches<br/>AND githubMergedPrEnabled?"}
    S6 -->|no| IGN
    S6 --> S7{"service_bus_namespace configured?"}
    S7 -->|yes| ENQ["enqueue IngestionJob<br/>message_id = X-GitHub-Delivery<br/>correlation_id = uuid<br/>return 200 immediately"]
    S7 -->|no| SYNC["process changed files inline<br/>return counts"]

    classDef entry fill:#2563EB,stroke:#2563EB,color:#FFFFFF,stroke-width:2px;
    classDef decision fill:#B45309,stroke:#D97706,color:#FFFFFF,stroke-width:2px;
    classDef process fill:#7C3AED,stroke:#7C3AED,color:#FFFFFF,stroke-width:2px;
    classDef success fill:#059669,stroke:#059669,color:#FFFFFF,stroke-width:2px;
    classDef rejected fill:#DC2626,stroke:#DC2626,color:#FFFFFF,stroke-width:2px;
    class W entry;
    class S1,S2,S3,S4,S6,S7 decision;
    class S5 process;
    class ENQ,SYNC success;
    class E413,E401,IGN rejected;
```

**Presentation takeaway:** untrusted webhook traffic cannot select an arbitrary project, repository, branch, event type, or ingestion route.

## 10. Asynchronous job lifecycle

This sequence follows an identifier-only ingestion job from webhook admission through completion, retry, or dead-lettering. Queue messages carry project and source identity plus tracing identifiers, but never document bodies, provider credentials, or application secrets.

At startup the worker verifies pinned model checksums. Each message reloads current project configuration through `IngestionService`; successful work is completed, transient failures are abandoned for redelivery, and the fifth failed delivery is dead-lettered with a safe reason and error type.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryTextColor":"#FFFFFF","lineColor":"#E2E8F0","actorBkg":"#2563EB","actorBorder":"#2563EB","actorTextColor":"#FFFFFF","actorLineColor":"#2563EB","signalColor":"#E2E8F0","signalTextColor":"#FFFFFF","labelBoxBkgColor":"#7C3AED","labelBoxBorderColor":"#7C3AED","labelTextColor":"#FFFFFF","loopTextColor":"#FFFFFF","noteBkgColor":"#B45309","noteBorderColor":"#D97706","noteTextColor":"#FFFFFF","activationBkgColor":"#059669","activationBorderColor":"#059669","sequenceNumberColor":"#FFFFFF","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .messageText, .loopText, .labelText { fill: #FFFFFF !important; }"}}%%
sequenceDiagram
    participant API as Webhook handler
    participant SB as Service Bus queue
    participant W as scripts/run_worker.py
    participant SVC as IngestionService

    API->>SB: ServiceBusMessage(IngestionJob.to_json())<br/>{projectId, provider, sourceId, sourceVersion,<br/>trigger, deliveryId, correlationId}<br/>no secrets, no bodies
    Note over W: startup: verify pinned model checksums
    W->>SB: receiver(max_wait_time=30,<br/>prefetch=docling_max_concurrency=2)
    SB-->>W: message
    W->>SVC: ingest_project(projectId, (provider,), full=False)
    alt success
        W->>SB: complete_message()
    else failure and delivery_count < 5
        W->>SB: abandon_message() (redelivery)
    else failure and delivery_count >= 5
        W->>SB: dead_letter_message(reason=INGESTION_FAILED,<br/>description=type(error).__name__)
    end
```

**Presentation takeaway:** asynchronous delivery provides bounded retries and operational isolation without putting sensitive content into the messaging layer.

## 11. Vector writes and Chroma layout

This diagram defines the generation-safe vector transaction and the physical collection contract. Before writing, ingestion reads all prior storage IDs for the exact project, access policy, provider, and source. It then embeds and upserts the complete new generation, verifies every expected ID, and only afterward deletes obsolete IDs.

Project-specific collection identity and required record metadata enforce isolation and traceability. The reserved vocabulary record summarizes observed corpus metadata for bounded query understanding and is excluded from ordinary evidence retrieval.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#7C3AED","primaryTextColor":"#FFFFFF","primaryBorderColor":"#7C3AED","lineColor":"#E2E8F0","edgeLabelBackground":"transparent","secondaryColor":"#2563EB","secondaryTextColor":"#FFFFFF","tertiaryColor":"#059669","tertiaryTextColor":"#FFFFFF","clusterBkg":"#1F2937","clusterBorder":"#94A3B8","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .edgeLabel { color: #FFFFFF !important; background-color: transparent !important; }"}}%%
flowchart TD
    CH["SourceChunk[]"] --> Q1["collection.get(where = $and[<br/>project_id, access_policy_id,<br/>provider, source_id])<br/>-> prior_ids"]
    Q1 --> EM["embed_passages(embedding_text[])"]
    EM --> IDS["storage_id = sha256(<br/>project|provider|source_id|ordinal)"]
    IDS --> UP["collection.upsert(ids, embeddings,<br/>documents=chunk.content, metadatas)"]
    UP --> VF["collection.get(ids=storage_ids, include=[])<br/>verify written"]
    VF --> DEL["collection.delete(prior_ids − storage_ids)"]
    DEL --> OK["commit node may now write manifest"]

    UP -.->|"transient: ConnectionError/Timeout/OSError<br/>or 408,429,500,502,503,504"| RT["_with_retry: 3 attempts,<br/>sleep 0.25 * 2^n"]

    subgraph Collection["collection = base[:48]-project[:32]-sha256(projectId)[:12]"]
        M1["metadata: hnsw:space=cosine,<br/>project_id, logical_collection"]
        M2["per-record: canonical_chunk_id, project_id,<br/>access_policy_id=project:{id}, provider,<br/>source_id, source_type, source_version,<br/>title, reference, source_url, chunk_ordinal,<br/>content_hash, structure_hash,<br/>structure_path/root/leaf, locator, language,<br/>visual_* , page_number, mime_type,<br/>security_classification, schema_version,<br/>embedding_model, embedding_text"]
        M3["reserved __vocabulary__ record<br/>provider=INGESTION, source_type=SYSTEM<br/>entities, doc_categories, providers,<br/>source_types, code_extensions, languages"]
    end

    OK --> M3

    classDef source fill:#2563EB,stroke:#2563EB,color:#FFFFFF,stroke-width:2px;
    classDef process fill:#7C3AED,stroke:#7C3AED,color:#FFFFFF,stroke-width:2px;
    classDef verify fill:#B45309,stroke:#D97706,color:#FFFFFF,stroke-width:2px;
    classDef success fill:#059669,stroke:#059669,color:#FFFFFF,stroke-width:2px;
    classDef retry fill:#DC2626,stroke:#DC2626,color:#FFFFFF,stroke-width:2px;
    classDef metadata fill:#0F766E,stroke:#0F766E,color:#FFFFFF,stroke-width:1.5px;
    class CH source;
    class Q1,EM,IDS,UP,DEL process;
    class VF verify;
    class OK success;
    class RT retry;
    class M1,M2,M3 metadata;
```

**Presentation takeaway:** a new source generation becomes authoritative only after the complete vector set is written and verified.

## 12. Azure Table state model

This entity model describes the operational records stored for each project/provider scope. The partition key hashes the scope identity, while fixed row keys identify the cursor and lease. Source manifests and quarantine records use hashed source identities so the table can scale without storing content bodies.

Manifests are compatibility fences as well as progress records: they retain source version and content hash alongside parser, chunker, embedding, and schema versions. The cursor advances only after a clean scope, and an expired lease can be taken over safely using entity tags.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#2563EB","primaryTextColor":"#FFFFFF","primaryBorderColor":"#2563EB","lineColor":"#E2E8F0","mainBkg":"#1D4ED8","nodeBorder":"#2563EB","secondaryColor":"#059669","secondaryTextColor":"#FFFFFF","tertiaryColor":"#B45309","tertiaryTextColor":"#FFFFFF","attributeBackgroundColorOdd":"#1E3A8A","attributeBackgroundColorEven":"#312E81","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .entityBox, .attributeBoxOdd, .attributeBoxEven { stroke: #2563EB !important; } .entityLabel, .attributeBoxOdd text, .attributeBoxEven text, .relationshipLabel { fill: #FFFFFF !important; color: #FFFFFF !important; }"}}%%
erDiagram
    PARTITION ||--o{ MANIFEST : contains
    PARTITION ||--|| CURSOR : has
    PARTITION ||--o| LEASE : has
    PARTITION ||--o{ QUARANTINE : has

    PARTITION {
        string PartitionKey "sha256(projectId|PROVIDER|scope)"
    }

    CURSOR {
        string RowKey "__cursor__"
        string cursor "ISO8601; advances only on clean scope"
    }

    LEASE {
        string RowKey "__lease__"
        string owner
        string expires_at "now + scope_lease_seconds(900); etag takeover on expiry"
    }

    QUARANTINE {
        string RowKey "__quarantine__{sourceHash}"
        string reason_code "BLOCKED_FILE_TYPE|FILE_TOO_LARGE|MALWARE_DETECTED|..."
        string source_id
        string scan_id
        string recorded_at
    }

    MANIFEST {
        string RowKey "sha256(source_id)"
        string version
        string content_hash
        int chunk_count
        string last_seen_run "scan_id"
        bool deleted
        string parser_version
        string chunker_version
        string embedding_profile
        string schema_version
    }
```

**Presentation takeaway:** Azure Table stores coordination and compatibility state—not source bodies, embeddings, or credentials.

## 13. Security gate ordering

This diagram presents the ordered security boundary applied before parsing, chunking, embedding, or vector creation. Cheap deterministic checks run first: blocked extensions, size limits, magic bytes, archive structure, macro presence, encryption, and executable signatures.

Optional ClamAV scanning and bounded secret detection then classify accepted content. Credential-sensitive documents are not silently discarded; they are explicitly marked with the project-authorized sensitive classification so retrieval policy can treat them appropriately.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#B45309","primaryTextColor":"#FFFFFF","primaryBorderColor":"#D97706","lineColor":"#E2E8F0","edgeLabelBackground":"transparent","secondaryColor":"#7C3AED","secondaryTextColor":"#FFFFFF","tertiaryColor":"#059669","tertiaryTextColor":"#FFFFFF","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .edgeLabel { color: #FFFFFF !important; background-color: transparent !important; }"}}%%
flowchart TD
    IN["document bytes / text"] --> S1{"extension in blocklist?<br/>.exe .dll .zip .jar .rar .docm .xlsm"}
    S1 -->|yes| QT["QuarantinedDocument<br/>BLOCKED_FILE_TYPE"]
    S1 --> S2{"size <= max_attachment_bytes (25 MB)?"}
    S2 -->|no| QT2["FILE_TOO_LARGE"]
    S2 --> S3["magic-byte / structure validation<br/>%PDF-, PK zip dir,<br/>block vbaproject.bin macros,<br/>block /Encrypt PDFs,<br/>block MZ and 0x7fELF"]
    S3 --> S4{"enable_malware_scan?"}
    S4 -->|yes| S5["ClamAV zINSTREAM over clamav_socket<br/>64 KB frames -> FOUND = MALWARE_DETECTED"]
    S4 -->|no| S6
    S5 --> S6["secret detection on first 2 MB:<br/>BEGIN PRIVATE KEY, gh[opsu]_*, AKIA*,<br/>api_key/client_secret/password assignments,<br/>Shannon entropy >= 4.7 with a digit"]
    S6 --> S7["local visuals: size cap +<br/>PNG/JPEG/WEBP magic + ClamAV"]
    S7 --> OUT{"credential_sensitive?"}
    OUT -->|yes| MARK["metadata.credential_sensitive = true<br/>security_classification =<br/>PROJECT_AUTHORIZED_SENSITIVE"]
    OUT -->|no| PASS["proceed to split"]
    MARK --> PASS

    classDef entry fill:#2563EB,stroke:#2563EB,color:#FFFFFF,stroke-width:2px;
    classDef decision fill:#B45309,stroke:#D97706,color:#FFFFFF,stroke-width:2px;
    classDef inspect fill:#7C3AED,stroke:#7C3AED,color:#FFFFFF,stroke-width:2px;
    classDef rejected fill:#DC2626,stroke:#DC2626,color:#FFFFFF,stroke-width:2px;
    classDef sensitive fill:#C2410C,stroke:#EA580C,color:#FFFFFF,stroke-width:2px;
    classDef success fill:#059669,stroke:#059669,color:#FFFFFF,stroke-width:2px;
    class IN entry;
    class S1,S2,S4,OUT decision;
    class S3,S5,S6,S7 inspect;
    class QT,QT2 rejected;
    class MARK sensitive;
    class PASS success;
```

**Presentation takeaway:** unsafe content stops before vectorization, while accepted sensitive content remains visibly classified and project-scoped.

## 14. Version fences that force reprocessing

This final diagram explains the six compatibility checks that determine whether an existing source can be skipped. Two checks describe the source itself—provider version and normalized content hash—while four describe the processing contract: parser, chunker, schema, and embedding model.

All six values must match the stored manifest. Any difference triggers complete re-analysis, re-chunking, re-embedding, and upsert so the corpus never mixes incompatible processing generations under one committed manifest.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"transparent","textColor":"#FFFFFF","primaryColor":"#2563EB","primaryTextColor":"#FFFFFF","primaryBorderColor":"#2563EB","lineColor":"#E2E8F0","edgeLabelBackground":"transparent","secondaryColor":"#7C3AED","secondaryTextColor":"#FFFFFF","tertiaryColor":"#059669","tertiaryTextColor":"#FFFFFF","clusterBkg":"#1F2937","clusterBorder":"#94A3B8","fontFamily":"Inter, Arial, sans-serif"},"themeCSS":"svg { background-color: transparent; } .edgeLabel { color: #FFFFFF !important; background-color: transparent !important; }"}}%%
flowchart LR
    subgraph Settings
        PV["parser_version<br/>docling-visual-v2"]
        CV["chunker_version<br/>semantic-token-entity-metadata-v9"]
        SV["schema_version = 3"]
        EM["embedding_model<br/>from project vectorStore mapping"]
    end

    subgraph Source
        DV["document.version<br/>blob sha | issue updated | page version"]
        DH["document.content_hash"]
    end

    PV & CV & SV & EM & DV & DH --> CMP{"all 6 equal to<br/>stored manifest?"}
    CMP -->|yes| SKIP["SKIP — chunks stay as the<br/>old chunker produced them"]
    CMP -->|no| REIDX["full re-analyze, re-chunk,<br/>re-embed, re-upsert"]

    classDef setting fill:#7C3AED,stroke:#7C3AED,color:#FFFFFF,stroke-width:2px;
    classDef source fill:#2563EB,stroke:#2563EB,color:#FFFFFF,stroke-width:2px;
    classDef decision fill:#B45309,stroke:#D97706,color:#FFFFFF,stroke-width:2px;
    classDef success fill:#059669,stroke:#059669,color:#FFFFFF,stroke-width:2px;
    classDef reprocess fill:#C2410C,stroke:#EA580C,color:#FFFFFF,stroke-width:2px;
    class PV,CV,SV,EM setting;
    class DV,DH source;
    class CMP decision;
    class SKIP success;
    class REIDX reprocess;
```

**Presentation takeaway:** compatibility is explicit and deterministic; no record is silently reused after a processing-contract change.
