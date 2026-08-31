"""Measure the quality of what is actually in the index, not just its shape.

The storage contract answers "is every required field present". It says
nothing about whether the chunks are any good, and a collection can pass it while
retrieval performs badly. The defects that matter here are all invisible at the
contract level:

* Truncation. The embedder has 512 positions. Anything past that is dropped
  silently at embedding time, so the vector stops representing the tail of the
  chunk while chunk_text still shows it. Nothing errors.
* Fragments. A chunk of a dozen tokens cannot answer anything, but it still
  occupies one of the few reranked slots (RERANK_TOP_N), displacing evidence
  that could.
* Duplication. The same boilerplate chunked out of many documents crowds a
  result set with copies of one fact.
* Lost context. structure_path is what tells the reader which heading a passage
  came from. Empty, and a passage is unattributable.
* Missing passage prefix. E5 is asymmetric: an embedding_text without
  "passage: " sits in a different region of the space from every correctly
  prefixed neighbour, so the chunk is effectively unreachable.

Read-only. Prints aggregates plus a few short excerpts, because a distribution
tells you there is a problem and an example tells you what it is.

    python -m scripts.audit_chunk_quality --project DEMO
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
import hashlib
import statistics

from app.config import get_settings
from app.chroma_collections import project_collection_name, verify_project_collection


EMBEDDER_POSITION_LIMIT = 512
PASSAGE_PREFIX = "passage: "
EXCERPT_CHARACTERS = 110
EXAMPLES_PER_FINDING = 3


def _tokenizer(name: str):
    from transformers import AutoTokenizer

    # Local files only would be wrong here: this is a developer tool, and the
    # tokenizer is small. But the name must match what ingestion chunked with, or
    # every length below is measured against the wrong vocabulary.
    return AutoTokenizer.from_pretrained(name)


def _excerpt(value: str) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= EXCERPT_CHARACTERS:
        return collapsed
    return collapsed[:EXCERPT_CHARACTERS] + "…"


def main(
    project_id: str,
    collection_name: str,
    minimum_tokens: int,
    provider: str | None,
) -> None:
    settings = get_settings()
    tokenizer = _tokenizer(settings.embedding_tokenizer)
    from chromadb import HttpClient
    logical_collection_name = collection_name
    collection_name = project_collection_name(logical_collection_name, project_id)
    collection = HttpClient(host=settings.chroma_host, port=settings.chroma_port).get_collection(collection_name)
    verify_project_collection(collection, logical_collection_name, project_id)

    offset = 0
    lengths: list[int] = []
    per_provider_lengths: dict[str, list[int]] = defaultdict(list)
    providers: Counter[str] = Counter()
    schema_versions: Counter[str] = Counter()
    schema_by_provider: dict[str, Counter[str]] = defaultdict(Counter)
    truncated: list[tuple[str, int, str]] = []
    fragments: list[tuple[str, int, str]] = []
    invalid_prefix: list[tuple[str, str]] = []
    contextless: list[tuple[str, str]] = []
    body_hashes: dict[str, list[str]] = defaultdict(list)
    sources: set[str] = set()
    total = 0

    filters: list[dict[str, object]] = [{"project_id": {"$eq": project_id}}]
    if provider:
        filters.append({"provider": {"$eq": provider}})
    record_filter = filters[0] if len(filters) == 1 else {"$and": filters}

    while True:
        response = collection.get(
            where=record_filter,
            limit=100,
            offset=offset,
            include=["documents", "metadatas"],
        )
        ids = response.get("ids") or []
        documents = response.get("documents") or []
        metadatas = response.get("metadatas") or []
        for record_id, document, metadata in zip(ids, documents, metadatas, strict=True):
            metadata = metadata or {}
            total += 1
            chunk_text = str(document or "")
            embedding_text = str(metadata.get("embedding_text") or "")
            source_provider = str(metadata.get("provider") or "UNKNOWN")
            providers[source_provider] += 1
            # The RAG discards any record whose schema_version differs from the
            # project record's, before reranking and without raising. A collection
            # holding two versions therefore answers from a fraction of itself,
            # and nothing in the answer says so.
            record_schema = str(metadata.get("schema_version") or "absent")
            schema_versions[record_schema] += 1
            schema_by_provider[source_provider][record_schema] += 1
            sources.add(str(metadata.get("source_id") or ""))

            # The embedded string is what the model sees, so it is what gets
            # measured -- chunk_text is only what a human reads afterwards.
            length = len(tokenizer.encode(embedding_text, add_special_tokens=True))
            lengths.append(length)
            per_provider_lengths[source_provider].append(length)

            if length > EMBEDDER_POSITION_LIMIT:
                truncated.append((record_id, length, _excerpt(chunk_text)))
            if length < minimum_tokens:
                fragments.append((record_id, length, _excerpt(chunk_text)))
            prefix_count = len(re.findall(r"(?i)\bpassage:\s*", embedding_text))
            if not embedding_text.startswith(PASSAGE_PREFIX) or prefix_count != 1:
                invalid_prefix.append((record_id, _excerpt(embedding_text)))
            structure_path = metadata.get("structure_path") or []
            if not structure_path:
                contextless.append((record_id, _excerpt(chunk_text)))
            body = " ".join(chunk_text.split()).casefold()
            if body:
                body_hashes[hashlib.sha256(body.encode()).hexdigest()].append(record_id)

        offset += len(ids)
        if len(ids) < 100:
            break

    if not total:
        raise SystemExit(
            f"No records matched project {project_id} in {collection_name}"
            + (f" for provider {provider}." if provider else ".")
        )

    duplicates = {
        digest: ids for digest, ids in body_hashes.items() if len(ids) > 1
    }
    duplicate_records = sum(len(ids) - 1 for ids in duplicates.values())

    print(f"records={total} sources={len(sources)} collection={collection_name}")
    print("providers=" + ", ".join(f"{key}:{providers[key]}" for key in sorted(providers)))
    print("schema_version=" + ", ".join(f"{key}:{schema_versions[key]}" for key in sorted(schema_versions)))
    if len(schema_versions) > 1:
        print(
            "  MIXED SCHEMA VERSIONS -- records not matching the project record are "
            "dropped during retrieval, silently:"
        )
        for name in sorted(schema_by_provider):
            spread = schema_by_provider[name]
            print(f"    {name}: " + ", ".join(f"v{key}={spread[key]}" for key in sorted(spread)))
        print("  Re-ingest the stale providers: ./scripts/run_unattended_ingestion.sh --full")
    print()
    print("token length of the embedded passage")
    quantiles = statistics.quantiles(lengths, n=20) if len(lengths) > 1 else [lengths[0]]
    print(
        f"  min={min(lengths)} median={int(statistics.median(lengths))} "
        f"p95={int(quantiles[-1])} max={max(lengths)} mean={statistics.mean(lengths):.1f}"
    )
    for name in sorted(per_provider_lengths):
        values = per_provider_lengths[name]
        print(
            f"  {name}: median={int(statistics.median(values))} max={max(values)} "
            f"n={len(values)}"
        )

    findings = (
        (
            "truncated at embedding time",
            truncated,
            f"longer than the embedder's {EMBEDDER_POSITION_LIMIT} positions; the tail "
            "is not represented in the vector",
        ),
        (
            "fragments",
            fragments,
            f"under {minimum_tokens} tokens; too small to answer, but still occupies a "
            "reranked slot",
        ),
        (
            "invalid passage prefix",
            invalid_prefix,
            "asymmetric E5 requires exactly one leading 'passage: '; missing or "
            "duplicated prefixes move the chunk away from the query embedding space",
        ),
        (
            "no structure path",
            contextless,
            "no heading trail, so the passage cannot be attributed to a section",
        ),
    )
    print()
    for label, items, why in findings:
        share = 100 * len(items) / total
        print(f"{label}: {len(items)} ({share:.1f}%) -- {why}")
        for entry in items[:EXAMPLES_PER_FINDING]:
            print(f"    {entry[0]}: {entry[-1]}")
    print(
        f"duplicate bodies: {duplicate_records} extra copies across "
        f"{len(duplicates)} distinct texts"
    )
    for digest, ids in list(duplicates.items())[:EXAMPLES_PER_FINDING]:
        del digest
        print(f"    {len(ids)}x: {', '.join(ids[:3])}")

    print()
    print(
        "Structure only. Whether retrieval actually answers questions is a "
        "different measurement: evaluation/run_live_acceptance.py in the RAG repo."
    )
    # Truncation and a missing prefix are correctness failures, not style: they
    # silently remove content from the searchable space.
    if truncated or invalid_prefix:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--collection", default="project-intelligence")
    parser.add_argument("--provider")
    parser.add_argument(
        "--minimum-tokens",
        type=int,
        default=40,
        help="Below this a chunk is reported as a fragment.",
    )
    arguments = parser.parse_args()
    main(
        arguments.project,
        arguments.collection,
        arguments.minimum_tokens,
        arguments.provider,
    )
