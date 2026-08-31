"""Local passage embedding for Chroma upserts.

E5 is asymmetric. This service owns only the document side and therefore only the
`passage: ` prefix; the RAG service owns `query: `. Keeping the two prefixes in
separate services is deliberate - mixing them silently degrades recall in a way
no test would catch.
"""

from __future__ import annotations

import threading
import re
from pathlib import Path
from typing import Any

from app.config import Settings


_PASSAGE_PREFIX = "passage: "
_PASSAGE_PREFIX_PATTERN = re.compile(r"^(?:\s*passage:\s*)+", re.IGNORECASE)

# E5 was trained at 512 positions. Chunk budgets are well under this, so passages
# are not expected to truncate; the cap is set explicitly so a future chunker
# change cannot silently start losing text.
_MAX_SEQUENCE_LENGTH = 512

_MODELS: dict[tuple[str, str], Any] = {}
_MODELS_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()


class LocalPassageEmbedder:
    """Embeds chunk passages with the pinned local E5 model."""

    def __init__(
        self,
        model: str,
        *,
        device: str,
        model_path: str = "",
        revision: str = "",
        dimensions: int = 1024,
        batch_size: int = 16,
    ) -> None:
        self._model_ref = model_path or model
        self._device = device
        self._revision = revision
        self._dimensions = dimensions
        self._batch_size = max(1, batch_size)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def sentence_transformer(self):
        """Return the process-cached model used for both counting and embedding."""

        return _load_embedding_model(self._model_ref, self._device, self._revision)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self.sentence_transformer()
        prefixed = [_ensure_passage_prefix(value) for value in texts]
        vectors: list[list[float]] = []
        with _INFERENCE_LOCK:
            for start in range(0, len(prefixed), self._batch_size):
                batch = prefixed[start : start + self._batch_size]
                encoded = model.encode(
                    batch,
                    batch_size=len(batch),
                    normalize_embeddings=True,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                )
                vectors.extend([float(value) for value in row] for row in encoded)
        for vector in vectors:
            if len(vector) != self._dimensions:
                raise ValueError(
                    "The local embedding model produced "
                    f"{len(vector)} dimensions but the index expects "
                    f"{self._dimensions}. Passage and query vectors must come from "
                    "the same model."
                )
        return vectors


def _ensure_passage_prefix(value: str) -> str:
    """Return E5 passage text with one, and only one, leading prefix."""

    body = _PASSAGE_PREFIX_PATTERN.sub("", value).lstrip()
    return _PASSAGE_PREFIX + body


def build_passage_embedder(settings: Settings) -> LocalPassageEmbedder:
    """Build the mandatory local passage embedder."""
    return LocalPassageEmbedder(
        settings.local_embedding_model,
        device=settings.local_embedding_device,
        model_path=settings.local_embedding_path,
        revision=settings.local_embedding_revision,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.local_embedding_batch_size,
    )


def _load_embedding_model(model_ref: str, device: str, revision: str):
    expanded = Path(model_ref).expanduser()
    resolved = str(expanded) if expanded.exists() else model_ref
    key = (resolved + "@" + revision, device)
    with _MODELS_LOCK:
        if key in _MODELS:
            return _MODELS[key]
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(
                "Local embedding requires the sentence-transformers runtime."
            ) from error
        model = SentenceTransformer(
            resolved,
            device=device,
            trust_remote_code=False,
            local_files_only=True,
            revision=None if Path(resolved).exists() else revision,
        )
        model.max_seq_length = _MAX_SEQUENCE_LENGTH
        _MODELS[key] = model
        return model
