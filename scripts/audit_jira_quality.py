"""Read-only audit of actual persisted Jira chunks, with JSON output."""

import argparse
import json
from pathlib import Path

from chromadb import HttpClient
from transformers import AutoTokenizer

from app.chroma_collections import project_collection_name, verify_project_collection
from app.config import get_settings
from app.jira_quality import audit_chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    collection = HttpClient(host=settings.chroma_host, port=settings.chroma_port).get_collection(
        project_collection_name(args.collection, args.project)
    )
    verify_project_collection(collection, args.collection, args.project)
    records, offset = [], 0
    while True:
        page = collection.get(
            where={"$and": [{"project_id": args.project}, {"provider": "JIRA"}]},
            include=["metadatas", "documents"],
            offset=offset,
            limit=100,
        )
        records.extend(
            {"id": i, "document": d, "metadata": m}
            for i, d, m in zip(page["ids"], page["documents"], page["metadatas"], strict=True)
        )
        if len(page["ids"]) < 100:
            break
        offset += len(page["ids"])
    tokenizer = AutoTokenizer.from_pretrained(settings.embedding_tokenizer)
    result = audit_chunks(records, lambda text: len(tokenizer.encode(text)))
    result.update(project_id=args.project, collection=args.collection)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["structural_quality_passed"] else 1)


if __name__ == "__main__":
    main()
