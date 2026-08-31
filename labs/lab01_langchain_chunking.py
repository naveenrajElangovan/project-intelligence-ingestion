"""Lab 1: inspect the production LangChain-based chunk representation."""

import json

from app.config import Settings
from app.models import SourceDocument
from app.structured_chunking import StructuredDocumentChunker


def run() -> list[dict[str, object]]:
    document = SourceDocument(
        project_id="DEMO",
        provider="CONFLUENCE",
        source_id="lab:architecture",
        source_type="PAGE",
        title="Arquitectura / Architecture",
        reference="LAB-1",
        source_url="https://example.invalid/lab",
        version="1",
        content=(
            "# Retrieval\n\n## Authorization\n\n"
            "The backend derives project:DEMO. El documento nunca puede cambiar el filtro.\n\n"
            "## Citations\n\nEvery factual claim cites authorized evidence."
        ),
        updated_at=None,
        mime_type="text/markdown",
        language="mixed",
    )
    chunks = StructuredDocumentChunker(Settings(_env_file=None, chunk_max_tokens=24)).split(document)
    return [
        {
            "id": chunk.chunk_id[:12],
            "path": chunk.structure_path,
            "language": chunk.language,
            "chunk_text": chunk.content,
            "embedding_text": chunk.embedding_text,
        }
        for chunk in chunks
    ]


if __name__ == "__main__":
    for item in run():
        print(json.dumps(item, ensure_ascii=False))
