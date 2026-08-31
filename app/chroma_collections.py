"""Deterministic project collection routing and connection validation."""

import hashlib
import re


def project_collection_name(base_name: str, project_id: str) -> str:
    base = _slug(base_name)
    project = _slug(project_id)
    if not base or not project:
        raise ValueError("A logical collection name and project id are required.")
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:12]
    return f"{base[:48]}-{project[:32]}-{digest}"


def project_collection_metadata(base_name: str, project_id: str) -> dict[str, str]:
    return {
        "hnsw:space": "cosine",
        "project_id": project_id,
        "logical_collection": base_name,
    }


def verify_project_collection(collection: object, base_name: str, project_id: str) -> None:
    expected_name = project_collection_name(base_name, project_id)
    if str(getattr(collection, "name", "")) != expected_name:
        raise RuntimeError("Chroma returned the wrong physical project collection.")
    metadata = getattr(collection, "metadata", None) or {}
    if metadata.get("project_id") != project_id:
        raise RuntimeError("Chroma collection project identity does not match the request.")
    if metadata.get("logical_collection") != base_name:
        raise RuntimeError("Chroma collection logical route does not match the request.")
    if metadata.get("hnsw:space") != "cosine":
        raise RuntimeError("Chroma collection must use cosine distance; reindex is required.")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-_")
