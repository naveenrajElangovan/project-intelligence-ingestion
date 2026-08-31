# Ingestion learning labs

These labs call the same security scanner, document model, and chunker used by the worker.

## 1. LangChain documents and structure-aware splitting

Run:

```bash
.venv/bin/python labs/lab01_langchain_chunking.py
```

Expected: JSON lines containing stable chunk IDs, heading paths, language, evidence text, and the richer `embedding_text`.

Exercises: change the token cap; add a third heading; compare the stable IDs before and after changing only the document version.

## 2. Local Docling conversion

Install the worker dependencies, then provide a PDF, DOCX, PPTX, or XLSX smaller than the configured limits:

```bash
.venv/bin/pip install -r requirements-worker.txt
.venv/bin/python labs/lab02_docling.py ./sample.pdf
```

Expected: local-only chunks with hierarchy and page locators when Docling exposes provenance. No document content is sent to a remote parsing service.

Exercises: compare a native and scanned PDF; inspect table header preservation; try a spoofed extension and observe quarantine.

Automated coverage: `tests/test_learning_labs.py` and `tests/test_structured_chunking_security.py`.
