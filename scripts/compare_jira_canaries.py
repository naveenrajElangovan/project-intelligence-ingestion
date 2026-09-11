"""Read-only semantic and security comparison of two isolated Jira canaries."""

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter
from pathlib import Path

from chromadb import HttpClient

from app.chroma_collections import project_collection_name, verify_project_collection
from app.config import get_settings
from app.content_security import _SECRET_PATTERNS, _has_high_entropy_secret, metadata_text_values


def inspect_collection(client, name, project):
    collection = client.get_collection(project_collection_name(name, project))
    verify_project_collection(collection, name, project)
    counts, sources, custom, urls, lengths, findings = Counter(), set(), set(), set(), [], []
    noise = offset = 0
    answer_kinds = {
        "DESCRIPTION",
        "REQUIREMENTS",
        "ACCEPTANCE",
        "CUSTOM_FIELD",
        "COMMENT",
        "WORKLOG",
        "REMOTE_LINK",
        "ATTACHMENT",
    }
    while True:
        page = collection.get(
            where={"provider": "JIRA"}, limit=100, offset=offset, include=["documents", "metadatas"]
        )
        for identity, text, meta in zip(
            page["ids"], page["documents"], page["metadatas"], strict=True
        ):
            source = meta["source_id"]
            sources.add(source)
            kind = meta.get("jira_chunk_kind") or meta.get("source_type")
            counts[kind] += 1
            if kind == "CURRENT":
                lengths.append(len(text))
                noise += bool(re.search(r'avatarUrls?|iconUrl|"self"\s*:', text))
            if kind == "CUSTOM_FIELD":
                custom.add((source, hashlib.sha256(text.encode()).hexdigest()))
            if kind in answer_kinds:
                urls.update(
                    (source, url.rstrip(').,"')) for url in re.findall(r"https?://[^\s<>\"]+", text)
                )
            for field, value in [
                ("document", text),
                *[(f"metadata.{k}", v) for k, v in meta.items()],
            ]:
                for scalar in metadata_text_values(
                    {field: value} if field != "document" else value
                ):
                    explicit = any(pattern.search(scalar) for pattern in _SECRET_PATTERNS)
                    entropy = _has_high_entropy_secret(scalar)
                    if explicit or entropy:
                        findings.append(
                            {
                                "chunk_id": identity,
                                "field": field,
                                "explicit_pattern": explicit,
                                "entropy_candidate": entropy,
                            }
                        )
        if len(page["ids"]) < 100:
            break
        offset += len(page["ids"])
    result = {
        "collection": name,
        "source_count": len(sources),
        "chunk_count": sum(counts.values()),
        "kinds": dict(counts),
        "current_transport_noise_chunks": noise,
        "current_characters": {
            "min": min(lengths, default=0),
            "max": max(lengths, default=0),
            "median": statistics.median(lengths) if lengths else 0,
        },
        "custom_field_chunks": len(custom),
        "answer_bearing_urls": len(urls),
        "security_findings": findings,
    }
    return result, sources, custom, urls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    client = HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    baseline = inspect_collection(client, args.baseline, args.project)
    candidate = inspect_collection(client, args.candidate, args.project)
    result = {
        "project_id": args.project,
        "baseline": baseline[0],
        "candidate": candidate[0],
        "same_sources": baseline[1] == candidate[1],
        "custom_field_text_preserved": baseline[2] == candidate[2],
        "answer_bearing_urls_preserved": baseline[3] == candidate[3],
        "rag_quality_verified": False,
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
