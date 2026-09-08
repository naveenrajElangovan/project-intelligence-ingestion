from app.projects import project_from_payload


def _payload() -> dict[str, object]:
    return {
        "projectId": "T2.0-STORE",
        "displayName": "Store",
        "githubRepositories": [],
        "jiraProjects": [],
        "confluenceSpaces": [],
        "vectorStore": {"collectionName": "project-intelligence"},
    }


def test_absent_access_configuration_preserves_existing_defaults() -> None:
    project = project_from_payload(_payload())

    assert project.source_access_rules == ()
    assert project.retrieval_profile is None


def test_access_rules_and_retrieval_profile_are_parsed() -> None:
    payload = _payload()
    payload["sourceAccessRules"] = [
        {
            "provider": "CONFLUENCE",
            "matchField": "TITLE",
            "prefix": "[STORE]",
            "accessPolicyId": "department:T2.0-STORE:STORE_OPERATIONS",
        }
    ]
    payload["retrievalProfile"] = {
        "maxChunksPerSource": 12,
        "rerankTopN": 16,
        "mixedSourceTopN": 12,
    }

    project = project_from_payload(payload)

    assert project.source_access_rules[0].match_field == "TITLE"
    assert project.source_access_rules[0].access_policy_id == (
        "department:T2.0-STORE:STORE_OPERATIONS"
    )
    assert project.retrieval_profile is not None
    assert project.retrieval_profile.max_chunks_per_source == 12
