# Jira-only staged ingestion and quality verification

The Jira implementation collects full accessible issue details and produces
source-linked sections. It does not modify Jira or upload local attachments.
GitHub and Confluence collectors remain unchanged.

Jira processing is configuration-driven across connected Jira Cloud sites and
project mappings. It does not require a particular project key, issue number,
issue type, status name, or custom-field catalog. Jira projects and Confluence
spaces are separate source mappings. Jira Data Center compatibility has not
been verified. Examples below use APPLICATION_PROJECT_ID as a placeholder.

## Readiness and source verification

Use backend-managed project mappings and OAuth credentials. The grant must
include `read:jira-work`. Confirm the configured site matches the connected
cloud. A connection row alone is not proof that credentials are usable.

From the ingestion repository:

```sh
.venv/bin/python -m scripts.run_jira_ingestion --all-connected-jira --phase preflight --report /tmp/jira-preflight.json
```

This checks one issue per mapped Jira project, including its comment/history/
worklog pages and attachment downloads, without indexing. It reports missing
connections and source failures individually. Do not use a company connection
as a substitute for a different configured site.

## Canary and full staging

```sh
.venv/bin/python -m scripts.run_jira_ingestion --all-connected-jira --phase canary --report /tmp/jira-canary.json
.venv/bin/python -m scripts.run_jira_ingestion --all-connected-jira --phase full-stage --report /tmp/jira-stage.json
```

Run the full stage only after reviewing the canary. Each invocation uses a fresh
collection and manifest scope. The canary reads up to ten issues per Jira
mapping, prioritizing attachment/comment/parent examples in the first page.
Inspect its inventory to identify categories not covered by that sample.
Non-Jira chunks are copied to staging; active collections remain unchanged.
The report names the target and rollback collections. Reusing a nonempty
staging collection is rejected unless explicit verified resume is requested.
An incomplete scope never advances its cursor.

Use `--run-id` and `--state-dir` to retain an atomic source-outcome ledger.
Resume requires `--resume-stage` and the same explicit run ID, project mappings,
selection, code/configuration, schema/model, repository manifest, and unchanged
preserved non-Jira baseline. Only successful sources with matching persisted
manifest and vector hashes can be skipped. Legacy runs without this contract
cannot be resumed. Changed source revisions require a fresh stage.

A targeted canary accepts repeated `--issue-key` arguments, at most ten,
with one `--project`. Selection remains part of the resume contract. Related
issue links are retained as evidence; targeted selection does not recursively
ingest related issues and is not a completeness evaluation.

Repository-relative citations may use `--reference-manifest` and
`--approved-reference-root`. The manifest pins project, root, alias, inventory,
and file-content hashes. Missing, changed, escaped, or unapproved references
are not exempted from validation. Credential checks still examine original text.

Issue visibility and restricted comments/worklogs are excluded when their
restrictions cannot be represented safely. Skips are recorded. A missing issue
in a search result is not treated as confirmed deletion. Full staging replaces
the Jira snapshot; production incremental ingestion does not sweep absent issues.

## Evaluate actual persisted chunks

Use the target collection from the stage report:

```sh
.venv/bin/python -m scripts.audit_jira_quality --project APPLICATION_PROJECT_ID --collection TARGET_COLLECTION --out /tmp/jira-chunk-audit.json
```

From the RAG repository:

```sh
.venv/bin/python -m evaluation.build_jira_suite --project-id APPLICATION_PROJECT_ID --collection TARGET_COLLECTION --out /tmp/jira-gold.jsonl
.venv/bin/python -m evaluation.run_retrieval_eval --project-id APPLICATION_PROJECT_ID --collection TARGET_COLLECTION --suites /tmp/jira-gold.jsonl --out /tmp/jira-retrieval.jsonl --generate 40 --generation-out /tmp/jira-generation.jsonl
```

The builder derives English/Spanish questions and exact source locators from
persisted evidence. Inspect its coverage report: unavailable categories become
explicit negative controls, not invented positive examples. The generated suite
only uses project-shared evidence; restricted-policy isolation requires separate
authorized cases. Run the existing non-Jira suites against the same stage and
baseline and use the existing Ragas/Phoenix tooling for generation scoring.

Promotion requires measured completeness, zero chunk defects/leakage, exact-key
status accuracy of 100%, candidate recall >=90%, final-evidence recall >=85%,
citation correctness >=95%, grounded-answer/abstention correctness >=90%, and
non-Jira regression <=5 percentage points. `app.jira_quality.promotion_gates`
rejects missing measurements; it does not manufacture scores or promote data.

Only after all gates pass, update the project's vector collection through the
existing backend project-configuration interface. Verify non-Jira content has
not changed since the staging copy before switching; refresh the copy and rerun
regression evaluation if it has. Keep the prior collection as rollback. Then run
the existing project-scoped incremental Jira command and verify no duplicates:

```sh
.venv/bin/python -m scripts.run_ingestion --project APPLICATION_PROJECT_ID --provider JIRA
```

## Monitoring and report interpretation

The existing ingestion dashboard includes Jira read outcomes and source-item
counts. Request metrics use resource categories, never issue keys or ticket text.
Enable the existing Pushgateway configuration for short-lived run metrics.

Distinguish source-read verification, successful indexing, structural chunk
quality, and model-based RAG quality. The runner deliberately never labels an
indexed collection as quality verified. Record run/code versions, source counts,
exclusions, attachment outcomes, token distributions, vocabulary policy checks,
retrieval/generation results, failures, and promotion/rollback disposition.

The index is retrieval-oriented, not a lossless Jira backup. External links do
not imply that external GitHub contents were fetched. Credential synchronization
between a Docker backend and a native diagnostic gateway must use the matching
connection and encrypted stores; never print tokens or overwrite unrelated entries.

## Jira attachment locators and generated metadata security

Jira attachment chunks use a deterministic source/version-qualified locator.
Real parser provenance is retained in `original_locator` and `page_number`.
Unpaginated content uses normalized heading identity and a window ordinal; it
never invents pages or byte offsets. Repeated windows keep distinct locators.
The Jira-only effective chunker marker `.jira-context-v1` invalidates old Jira
manifests. Resume integrity compares the effective chunker and parser versions
as well as source version, hashes, schema, model, and dimensions.

Jira enrichment emits these fields in schema order: repository, branch, path,
file_name, issue_key, symbols, important_kwd. Within a list or tuple, source order
is preserved. Each record consists of an ASCII field name, a decimal UTF-8 byte
length, a newline, exactly those value bytes, and a newline. Values are strings;
Unicode is preserved. Embedded newlines or length-like text do not define record
boundaries. NUL and other C0/C1 control characters except tab/CR/LF are rejected.
Limits are 65,536 bytes per value, 262,144 bytes per enrichment block, and 2,048
records. Exceeding a limit fails; values are never silently truncated.

Before Jira index writes, the workflow scans original/decoded metadata values,
semantic credential key/value pairs, canonical JSON through decoded boundaries,
chunk bodies, and the assembled embedding text. Unordered sibling mappings are
not concatenated. Explicit sequences and the enrichment schema order are also
checked for adjacent and full reconstructed credential spellings. Nested
structures follow the same rule. Metadata scanning fails beyond depth 32,
8,192 text values per metadata group, or 4,000,000 UTF-8 value bytes. Complete
verified citations retain their value boundaries before entropy checks; explicit
credential patterns still examine original text. Persisted embedding text is
retained for model-input and body-integrity auditing.
