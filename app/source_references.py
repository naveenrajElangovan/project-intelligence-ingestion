"""Pinned, operator-authorized repository references; never inferred allowlists."""

import hashlib
import json
import re
import unicodedata
from pathlib import Path


class ReferenceValidationError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def build_reference_manifest(root, project_id, alias, files):
    root = Path(root).resolve(strict=True)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", alias):
        raise ReferenceValidationError("Invalid repository alias")
    records = {}
    for file in sorted(files):
        file = Path(file)
        resolved = file.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ReferenceValidationError("Reference escapes the authorized root")
        if resolved.stat().st_size > 4_000_000:
            raise ReferenceValidationError("Reference exceeds the manifest file-size limit")
        relative = unicodedata.normalize("NFC", file.relative_to(root).as_posix())
        records[relative] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if not records or len(records) > 10000:
        raise ReferenceValidationError("Invalid reference manifest inventory size")
    manifest = {
        "schema_version": 1,
        "project_id": project_id,
        "alias": alias,
        "root": str(root),
        "source_revision": "sha256:" + digest(records),
        "files": records,
    }
    return {**manifest, "manifest_hash": digest(manifest)}


class RepositoryReferences:
    def __init__(self, manifest_paths=(), approved_roots=()):
        approved = {Path(root).resolve(strict=True) for root in approved_roots}
        self.manifests = []
        aliases = set()
        for path in manifest_paths:
            path = Path(path)
            if path.stat().st_size > 4_000_000:
                raise ReferenceValidationError("Reference manifest exceeds its size limit")
            value = json.loads(path.read_text())
            recorded_hash = value.pop("manifest_hash", None)
            if recorded_hash != digest(value) or value.get("schema_version") != 1:
                raise ReferenceValidationError("Reference manifest hash/schema mismatch")
            root = Path(value["root"]).resolve(strict=True)
            alias = value["alias"]
            if root not in approved or not re.fullmatch(r"[A-Za-z0-9_-]+", alias):
                raise ReferenceValidationError("Unapproved reference root or alias")
            files = value["files"]
            if (
                not files
                or len(files) > 10000
                or value["source_revision"] != "sha256:" + digest(files)
            ):
                raise ReferenceValidationError("Reference source revision mismatch")
            if alias in aliases:
                raise ReferenceValidationError("Duplicate repository reference alias")
            aliases.add(alias)
            pattern = re.compile(r"(?<![\w./%:@-])" + re.escape(alias) + r"/[^\s`<>\[\]()\"']+")
            self.manifests.append((value, root, pattern, recorded_hash))

    @property
    def fingerprint(self):
        return digest(sorted(item[3] for item in self.manifests))

    def normalize(self, text, project_id):
        for manifest, root, pattern, _ in self.manifests:

            def substitute(match):
                raw = match.group()
                candidate = unicodedata.normalize("NFC", raw.rstrip(".,;"))
                if manifest["project_id"] != project_id:
                    raise ReferenceValidationError("Reference project is not authorized")
                if not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)+", candidate) or any(
                    part in {".", ".."} for part in candidate.split("/")
                ):
                    raise ReferenceValidationError("Invalid complete repository reference")
                relative = candidate.split("/", 1)[1]
                expected = manifest["files"].get(relative)
                if not expected:
                    raise ReferenceValidationError("Reference is absent from the pinned manifest")
                try:
                    target = (root / relative).resolve(strict=True)
                except (OSError, RuntimeError) as error:
                    raise ReferenceValidationError("Reference target is unavailable") from error
                if (
                    not target.is_relative_to(root)
                    or not target.is_file()
                    or target.stat().st_size > 4_000_000
                ):
                    raise ReferenceValidationError(
                        "Reference target is outside its allowed boundary"
                    )
                if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                    raise ReferenceValidationError(
                        "Reference content differs from its pinned revision"
                    )
                return candidate.replace("/", " ") + raw[len(raw.rstrip(".,;")) :]

            text = pattern.sub(substitute, text)
        return text
