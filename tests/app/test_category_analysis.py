from app.format_analysis import detect_category, detect_entity


def test_title_conventions_win_over_incidental_flow_references() -> None:
    assert detect_category("BOT-RAG-00 Master Index", "").category == "index"
    assert detect_category("BOT-RAG-01 Product Features", "[BOTFLOW-050]").category == "narrative"
    assert detect_category("BOT-RAG-02 Business Workflows", "").category == "workflow"
    assert detect_category("BOT-RAG-03 Architecture", "[BOTFLOW-050]").category == "narrative"


def test_event_contract_and_authoritative_label_are_auditable() -> None:
    inferred = detect_category("Example Retail Event and Integration Contract", "")
    labelled = detect_category(
        "Master Index",
        "",
        labels=("pi-category:entity-contract",),
    )

    assert inferred.category == "entity-contract"
    assert labelled.category == "entity-contract"
    assert labelled.reason == "Confluence pi-category label"
    assert "disagrees" in labelled.warning


def test_entity_uses_label_then_title_then_structural_identifier() -> None:
    assert detect_entity("Anything", "", labels=("pi-entity:Atlas",)).entity == "atlas"
    assert detect_entity("Nova-RAG-02 Workflows", "").entity == "nova"
    assert detect_entity("Contract", "NOVA_PAYMENT - id 42").entity == "nova"
    assert detect_entity("Master Index", "ordinary narrative").entity == ""
