from app.config import Settings
from app.models import SourceDocument
from app.structured_chunking import StructuredDocumentChunker, _specific_entity_key


def test_generic_application_labels_are_not_entity_keys() -> None:
    assert _specific_entity_key({}, ["BOT"]) == ""
    assert _specific_entity_key({}, ["POS"]) == ""


def test_a_single_concrete_identifier_becomes_the_entity_key() -> None:
    assert _specific_entity_key({}, ["BOT", "POS_LOGIN_EVENT"]) == "POS_LOGIN_EVENT"
    assert _specific_entity_key({"flow_id": "BOTFLOW-110"}, []) == "BOTFLOW-110"


def test_kotlin_enum_registry_entries_receive_one_concrete_entity_key_each() -> None:
    events = [f"STORE_EVENT_{index:02d}" for index in range(58)]
    content = (
        "enum class EventType {\n"
        + "\n".join(
            f'    {event}("{event}", {100 + index}, "0.1"),' for index, event in enumerate(events)
        )
        + "\n}"
    )
    document = SourceDocument(
        project_id="DEMO",
        provider="GITHUB",
        source_id="src/EventType.kt",
        source_type="CODE",
        title="EventType.kt",
        reference="src/EventType.kt",
        source_url="https://example.invalid/src/EventType.kt",
        version="abc123",
        content=content,
        updated_at=None,
        mime_type="text/x-kotlin",
        language="en",
        metadata={},
    )

    chunks = StructuredDocumentChunker(Settings(_env_file=None)).split(document)
    registry = [chunk for chunk in chunks if chunk.metadata.get("chunk_profile") == "enum-registry"]

    assert len(registry) == 58
    assert {chunk.metadata["entity_key"] for chunk in registry} == set(events)
    assert {chunk.structure_path[-1] for chunk in registry} == set(events)
    assert all(chunk.metadata["entity_id"] for chunk in registry)
