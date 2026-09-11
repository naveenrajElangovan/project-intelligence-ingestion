"""Deterministic Jira chunk gates; model scores must be supplied by real RAG runs."""

import statistics
from collections import Counter


def audit_chunks(records, count_tokens):
    """records contain id, document, and metadata from a Chroma collection."""
    defects = []
    lengths = []
    languages = Counter()
    kinds = Counter()
    sources = set()
    identities = set()
    for record in records:
        metadata = record["metadata"]
        if metadata.get("provider") != "JIRA":
            continue
        identity = metadata.get("canonical_chunk_id") or record["id"]
        text = record["document"]
        embedding = str(metadata.get("embedding_text") or "")
        count = count_tokens(embedding)
        lengths.append(count)
        languages[str(metadata.get("language") or "und")] += 1
        kinds[str(metadata.get("jira_chunk_kind") or metadata.get("source_type") or "unknown")] += 1
        sources.add(metadata.get("source_id"))
        reasons = []
        if identity in identities:
            reasons.append("duplicate_identity")
        identities.add(identity)
        if not embedding.startswith("passage: "):
            reasons.append("missing_passage_prefix")
        if not embedding.endswith(text):
            reasons.append("body_not_fully_embedded")
        if count > 512:
            reasons.append("embedding_overflow")
        for field in (
            "project_id",
            "access_policy_id",
            "source_id",
            "source_url",
            "issue_key",
            "locator",
            "cloud_id",
        ):
            if not metadata.get(field):
                reasons.append("missing_" + field)
        if not text.strip():
            reasons.append("empty_chunk")
        if reasons:
            defects.append({"chunk_id": identity, "reasons": reasons})
    return {
        "jira_chunks": len(lengths),
        "jira_sources": len(sources),
        "kinds": dict(kinds),
        "languages": dict(languages),
        "embedding_tokens": {
            "min": min(lengths, default=0),
            "max": max(lengths, default=0),
            "median": statistics.median(lengths) if lengths else 0,
        },
        "defects": defects,
        "structural_quality_passed": bool(lengths) and not defects,
        "rag_quality_verified": False,
    }


def promotion_gates(audit, scores):
    thresholds = {
        "source_completeness": 1.0,
        "gold_resolution": 1.0,
        "exact_key_status_accuracy": 1.0,
        "candidate_recall": 0.90,
        "final_evidence_recall": 0.85,
        "citation_correctness": 0.95,
        "grounded_answer_accuracy": 0.90,
    }
    failures = []
    cases = scores.get("evaluation_case_count")
    if not isinstance(cases, int) or cases < 40:
        failures.append("evaluation_case_count")
    if not audit.get("structural_quality_passed"):
        failures.append("chunk_quality")
    for metric, threshold in thresholds.items():
        value = scores.get(metric)
        if not isinstance(value, (int, float)) or not threshold <= value <= 1:
            failures.append(metric)
    for metric in (
        "cross_project_leakage",
        "access_policy_leakage",
        "unexplained_collection_failures",
    ):
        if scores.get(metric) != 0:
            failures.append(metric)
    regression = scores.get("non_jira_regression")
    if not isinstance(regression, (int, float)) or not 0 <= regression <= 0.05:
        failures.append("non_jira_regression")
    return {"passed": not failures, "failed_gates": failures}
