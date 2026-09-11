from app.jira_quality import audit_chunks, promotion_gates


def test_empty_or_partial_quality_cannot_be_promoted():
    assert not audit_chunks([], lambda _: 0)["structural_quality_passed"]
    assert not promotion_gates({"structural_quality_passed": True}, {})["passed"]


def test_audit_reports_missing_context_and_truncation():
    result = audit_chunks(
        [
            {
                "id": "1",
                "document": "complete source",
                "metadata": {"provider": "JIRA", "embedding_text": "passage: incomplete"},
            }
        ],
        lambda _: 513,
    )
    reasons = result["defects"][0]["reasons"]
    assert "body_not_fully_embedded" in reasons
    assert "embedding_overflow" in reasons
    assert "missing_access_policy_id" in reasons


def test_all_measured_gates_are_required():
    scores = {
        "source_completeness": 1.0,
        "evaluation_case_count": 40,
        "gold_resolution": 1.0,
        "exact_key_status_accuracy": 1.0,
        "candidate_recall": 0.9,
        "final_evidence_recall": 0.85,
        "citation_correctness": 0.95,
        "grounded_answer_accuracy": 0.9,
        "cross_project_leakage": 0,
        "access_policy_leakage": 0,
        "unexplained_collection_failures": 0,
        "non_jira_regression": 0.01,
    }
    assert promotion_gates({"structural_quality_passed": True}, scores)["passed"]
    scores["citation_correctness"] = 0.94
    assert promotion_gates({"structural_quality_passed": True}, scores)["failed_gates"] == [
        "citation_correctness"
    ]


def test_storage_generations_cannot_hide_duplicate_evidence():
    records = [
        {
            "id": storage_id,
            "document": "fact",
            "metadata": {
                "provider": "JIRA",
                "canonical_chunk_id": "same-event",
                "embedding_text": "passage: fact",
            },
        }
        for storage_id in ("old-generation", "new-generation")
    ]
    audit = audit_chunks(records, lambda _: 3)
    assert "duplicate_identity" in audit["defects"][1]["reasons"]
