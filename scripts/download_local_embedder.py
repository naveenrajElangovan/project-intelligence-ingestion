"""Download the query embedder once for later offline-only production use.

This model is shared by local ingestion and retrieval, so vectors
produced locally live in the same space as vectors already in the index. Running
it here is what removes the monthly provider embedding quota from the request
path.

The resolved commit is printed so it can be pinned in
`PI_INGEST_LOCAL_EMBEDDING_REVISION`, matching how the reranker is pinned.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download


MODEL = "intfloat/multilingual-e5-large"

# The repository ships the same weights four times over: safetensors, a PyTorch
# pickle, an ONNX export, and an OpenVINO export. sentence-transformers loads the
# safetensors copy, so fetching everything costs about 7 GB per download for
# nothing. Restrict to what the loader actually opens.
ALLOW_PATTERNS = [
    "config.json",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "1_Pooling/*",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--revision",
        default="main",
        help="Commit or branch to download. Pin the printed commit afterwards.",
    )
    parser.add_argument(
        "--everything",
        action="store_true",
        help="Fetch every file, including the unused ONNX and OpenVINO exports.",
    )
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL,
        revision=args.revision,
        local_dir=destination,
        allow_patterns=None if args.everything else ALLOW_PATTERNS,
    )
    print(destination)
    print(f"revision={_resolved_revision(destination)}")


def _resolved_revision(destination: Path) -> str:
    """Report the commit that was materialized, so it can be pinned."""

    for candidate in destination.glob(".cache/huggingface/download/**/*.metadata"):
        lines = candidate.read_text(encoding="utf-8").splitlines()
        if lines:
            return lines[0].strip()
    return "unknown - read it from the model page and pin it manually"


if __name__ == "__main__":
    main()
