"""Lab 2: run the production local Docling path against one document."""

import argparse
import json
import mimetypes
from pathlib import Path

from app.config import Settings
from app.content_security import ContentSecurityScanner
from app.models import SourceDocument
from app.structured_chunking import StructuredDocumentChunker


def build_document(path: Path) -> SourceDocument:
    return SourceDocument(
        project_id="DEMO",
        provider="LAB",
        source_id=f"file:{path.name}",
        source_type="ATTACHMENT",
        title=path.name,
        reference=path.name,
        source_url="local://lab",
        version="1",
        content="",
        content_bytes=path.read_bytes(),
        updated_at=None,
        metadata={"path": path.name},
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


def run(path: Path) -> list[dict[str, object]]:
    settings = Settings(_env_file=None)
    document = build_document(path)
    ContentSecurityScanner(settings).inspect(document)
    return [
        {"id": chunk.chunk_id[:12], "path": chunk.structure_path, "locator": chunk.locator, "text": chunk.content}
        for chunk in StructuredDocumentChunker(settings).split(document)
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("document", type=Path)
    arguments = parser.parse_args()
    for item in run(arguments.document):
        print(json.dumps(item, ensure_ascii=False))
