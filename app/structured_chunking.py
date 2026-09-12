from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable

from bs4 import BeautifulSoup
from langchain_text_splitters import (
    HTMLSemanticPreservingSplitter,
    Language,
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
    SentenceTransformersTokenTextSplitter,
)
from langchain_text_splitters.base import TextSplitter

from app.config import Settings
from app.format_analysis import EntityDecision, detect_category, detect_entity, detect_format
from app.jira_chunk_context import attachment_locator
from app.jira_chunk_context import metadata_context as jira_metadata_context
from app.models import (
    LogicalElement,
    ParsingResult,
    SourceChunk,
    SourceDocument,
    StructuredArtifact,
    VisualAnalysis,
)
from app.parsing import attachment_to_text
from app.visual import analyze_docling, analyze_html, analyze_markdown

# The embedding model has 512 positions. Anything beyond them is dropped at
# embedding time with no error, so this is the only hard limit in the module;
# every other size is a target.
_EMBEDDER_POSITION_LIMIT = 512
_TOKEN_CACHE_ENTRIES = 20_000

_CODE_LANGUAGES = {
    ".java": Language.JAVA,
    ".js": Language.JS,
    ".kt": Language.KOTLIN,
    ".kts": Language.KOTLIN,
    ".py": Language.PYTHON,
    ".ts": Language.TS,
    ".tsx": Language.TS,
}


class _SharedSentenceTransformerSplitter(SentenceTransformersTokenTextSplitter):
    """Use LangChain's splitter with the process-cached embedding model."""

    def __init__(self, model: object, *, tokens_per_chunk: int, chunk_overlap: int) -> None:
        TextSplitter.__init__(
            self,
            chunk_size=tokens_per_chunk,
            chunk_overlap=chunk_overlap,
        )
        self.model_name = str(getattr(model, "model_card_data", "local-embedding-model"))
        self._model = model
        self.tokenizer = model.tokenizer
        self._initialize_chunk_configuration(tokens_per_chunk=tokens_per_chunk)

    def split_text(self, text: str) -> list[str]:
        """Enforce the embedder's hard position limit after reconstruction.

        LangChain sizes the underlying token windows correctly, but decoding and
        joining adjacent token spans can introduce an extra token when the
        resulting text is encoded again.  The embedding model sees that second
        encoding, so validate the emitted strings and bisect any oversize window
        until every passage fits rather than relying on the nominal window size.
        """

        pending = list(super().split_text(text))
        fitted: list[str] = []
        while pending:
            window = pending.pop(0)
            if self.count_tokens(text=window) <= _EMBEDDER_POSITION_LIMIT:
                fitted.append(window)
                continue
            midpoint = max(1, len(window) // 2)
            boundary = window.rfind(" ", 0, midpoint)
            if boundary <= 0:
                boundary = window.find(" ", midpoint)
            if boundary <= 0:
                raise ValueError("An indivisible token exceeds the embedding position limit")
            pending[0:0] = [window[:boundary].strip(), window[boundary:].strip()]
        return fitted


class StructuredDocumentChunker:
    """Structure-aware chunking with an optional local Docling path for binary documents."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_model_loader: Callable[[], object] | None = None,
    ) -> None:
        self._settings = settings
        self._embedding_model_loader = embedding_model_loader
        self._embedding_model = None
        self._sentence_splitter: SentenceTransformersTokenTextSplitter | None = None
        self._token_counts: dict[str, int] = {}

    def split(self, document: SourceDocument) -> tuple[SourceChunk, ...]:
        return self.chunk(document, self.analyze(document))

    def chunk(
        self, document: SourceDocument, artifact: StructuredArtifact
    ) -> tuple[SourceChunk, ...]:
        parsed = artifact.parsing
        labels_value = document.metadata.get("labels") or ()
        labels = tuple(str(value) for value in labels_value) if isinstance(
            labels_value, (list, tuple, set)
        ) else (str(labels_value),) if labels_value else ()
        category = detect_category(document.title, document.content, labels=labels)
        chunks: list[SourceChunk] = []
        if not document.content.strip() and document.content_bytes is None:
            # Producing zero chunks and committing a manifest is the worst
            # available outcome: the counters report the source as indexed, the
            # cursor advances, and the document contributes nothing to
            # retrieval. Nobody looks again, because nothing failed. A Confluence
            # live doc whose body arrived in a representation this connector did
            # not request is exactly how that happens. Failing here counts the
            # source as failed and leaves the cursor where it was, so the next
            # run retries it.
            # A binary document carries its payload in content_bytes and is
            # converted by the parser, so an empty `content` is normal there and
            # must not be treated as a missing body.
            raise ValueError(
                f"{document.provider} {document.source_id} has an empty body; "
                "nothing to chunk"
            )
        entity_decisions = tuple(
            EntityDecision("", "CODE chunks do not infer entities from identifiers")
            if document.source_type == "CODE"
            else detect_entity(
                document.title,
                _clean(element.text),
                labels=labels,
                structure_path=element.heading_path,
            )
            for element in parsed.elements
        )
        document_entities = {
            decision.entity.casefold()
            for decision in entity_decisions
            if decision.entity.strip()
        }
        shared_across_entities = len(document_entities) > 1
        for ordinal, element in enumerate(parsed.elements):
            normalized = _clean(element.text)
            if not normalized:
                continue
            language = document.language if document.language != "und" else _detect_language(normalized)
            context = " > ".join((document.title, *element.heading_path))
            searchable_metadata = _searchable_metadata(document, element, normalized)
            entity = entity_decisions[ordinal]
            page_number = _locator_page(element.locator)
            locator = (
                attachment_locator(document, element, ordinal)
                if document.provider == "JIRA" and document.source_type == "ATTACHMENT"
                else element.locator
            )
            related_assets = tuple(
                asset
                for asset in artifact.visual.assets
                if asset.page_number == page_number
                or (asset.page_number is None and ordinal == 0)
            )
            visual_context = "\n".join(
                value
                for asset in related_assets
                for value in (
                    f"VISUAL TYPE: {asset.asset_type}",
                    f"VISUAL CAPTION: {asset.caption}" if asset.caption else "",
                    f"VISUAL LABELS: {asset.ocr_text}" if asset.ocr_text else "",
                )
                if value
            )
            embedding_text = self._fit_embedding_text(
                required=f"passage: {document.reference} {locator or ''}" if document.provider == "JIRA" else f"passage: {context}",
                optional=(
                    f"SOURCE TYPE: {document.source_type}",
                    f"REFERENCE: {document.reference}",
                    f"LOCATION: {locator or 'source'}",
                    jira_metadata_context(searchable_metadata) if document.provider == "JIRA" else _metadata_context(searchable_metadata),
                    visual_context,
                ),
                body=normalized,
            )
            if document.provider == "JIRA" and not embedding_text.endswith(normalized):
                raise ValueError("Jira embedding would truncate source evidence")
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            structure_hash = hashlib.sha256(
                json.dumps(
                    {
                        "path": element.heading_path,
                        "locator": locator,
                        "metadata": element.metadata,
                    },
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            identity = (
                f"{document.project_id}|{document.provider}|{document.source_id}|"
                f"{document.version}|{ordinal}|{digest}|{self._settings.chunker_version}"
            )
            if document.provider in {"JIRA", "CONFLUENCE"}:
                identity = f"{document.project_id}|{document.provider}|{document.source_id}|{locator}|{digest}|atlassian-v1"
            chunks.append(
                SourceChunk(
                    chunk_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                    source_id=document.source_id,
                    ordinal=ordinal,
                    content=normalized,
                    content_hash=digest,
                    structure_hash=structure_hash,
                    embedding_text=embedding_text,
                    structure_path=element.heading_path,
                    locator=locator,
                    language=language,
                    visual_eligible=bool(related_assets),
                    visual_types=tuple(sorted({asset.asset_type for asset in related_assets})),
                    visual_asset_ids=tuple(asset.asset_id for asset in related_assets),
                    page_number=page_number,
                    metadata={
                        **element.metadata,
                        **searchable_metadata,
                        **({"original_locator": element.locator or ""} if document.provider == "JIRA" and document.source_type == "ATTACHMENT" else {}),
                        "doc_category": element.metadata.get("doc_category") or category.category,
                        "entity": element.metadata.get("entity") or entity.entity,
                        "entity_key": element.metadata.get("entity_key") or entity.entity,
                        "application": entity.entity,
                        "shared_across_entities": shared_across_entities,
                        "entity_reason": element.metadata.get("entity_reason") or entity.reason,
                        "category_reason": element.metadata.get("category_reason") or category.reason,
                        "category_warning": element.metadata.get("category_warning") or category.warning,
                        "identifiers": element.metadata.get("identifiers")
                        or sorted(set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", normalized))),
                        "chunk_profile": element.metadata.get("chunk_profile") or category.category,
                        "visual_asset_types": [asset.asset_type for asset in related_assets],
                        "visual_asset_captions": [asset.caption for asset in related_assets],
                        "visual_reason_codes": list(artifact.visual.reason_codes),
                    },
                )
            )
        # Identical spans from one source add no evidence, but can consume
        # several retrieval and reranking slots. Keep the first attributed
        # occurrence; copies in other source files remain independently stored.
        unique_chunks: list[SourceChunk] = []
        seen_bodies: set[str] = set()
        for chunk in chunks:
            body_identity = chunk.content_hash
            if document.provider == "JIRA":
                body_identity += ":" + str(chunk.metadata.get("jira_chunk_kind") or "") + ":" + str(chunk.metadata.get("event_id") or "")
                if document.source_type == "ATTACHMENT":
                    body_identity += ":" + str(chunk.locator)
            if body_identity in seen_bodies:
                continue
            seen_bodies.add(body_identity)
            unique_chunks.append(chunk)
        if len({chunk.chunk_id for chunk in unique_chunks}) != len(unique_chunks):
            raise ValueError(f"Duplicate chunk ids generated for {document.source_id}")
        return tuple(unique_chunks)

    def parse(self, document: SourceDocument) -> ParsingResult:
        return self.analyze(document).parsing

    def analyze(self, document: SourceDocument) -> StructuredArtifact:
        if document.content_bytes is not None:
            return self._docling_artifact(document)
        labels_value = document.metadata.get("labels") or ()
        labels = tuple(str(value) for value in labels_value) if isinstance(
            labels_value, (list, tuple, set)
        ) else (str(labels_value),) if labels_value else ()
        category = detect_category(document.title, document.content, labels=labels)
        elements = tuple(
            LogicalElement(
                kind=str(metadata.get("kind") or document.source_type),
                text=content,
                # A passage with no heading trail cannot be attributed to
                # anything, which is what makes a citation useless to a reader
                # and a chunk unfilterable by section. Content that precedes the
                # first heading of a document has no trail of its own -- a
                # Confluence page opens with prose under a title that lives
                # outside the body -- so it inherits the document title rather
                # than being left anonymous.
                heading_path=path or (document.title,) if document.title else path,
                locator=locator,
                metadata={
                    **metadata,
                    "doc_category": category.category,
                    "category_reason": category.reason,
                    "category_warning": category.warning,
                    "identifiers": sorted(set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", content))),
                },
            )
            for content, path, locator, metadata in self._structured_values(document)
            if _clean(content)
        )
        parsing = ParsingResult(
            schema_version=self._settings.schema_version,
            parser_version=self._settings.parser_version,
            source_id=document.source_id,
            source_version=document.version,
            mime_type=document.mime_type,
            language=document.language,
            security_classification=document.security_classification,
            elements=elements,
        )
        path = str(document.metadata.get("path") or document.title)
        is_markdown = Path(path).suffix.lower() in {".md", ".markdown"} or document.mime_type == "text/markdown"
        if self._settings.visual_analysis_enabled and is_markdown:
            visual = analyze_markdown(document, self._settings)
        elif self._settings.visual_analysis_enabled and document.mime_type == "text/html":
            visual = analyze_html(document, self._settings)
        else:
            visual = VisualAnalysis(False)
        return StructuredArtifact(parsing=parsing, visual=visual)

    def _structured_values(
        self, document: SourceDocument
    ) -> Iterable[tuple[str, tuple[str, ...], str | None, dict[str, object]]]:
        path = str(document.metadata.get("path") or document.title)
        suffix = Path(path).suffix.lower()
        if suffix in _CODE_LANGUAGES:
            yield from self._code_values(document, _CODE_LANGUAGES[suffix])
            return
        if document.source_type == "ISSUE":
            yield from self._issue_values(document)
            return
        # Declared type wins where there is one; sniffing only fills the gap.
        # Without it an attachment named notes.txt that contains Markdown, or a
        # .dat file that contains CSV, fell through to the generic section path
        # and lost every heading and every table it had.
        decision = detect_format(document.content, declared=_declared_format(suffix, document))
        labels_value = document.metadata.get("labels") or ()
        labels = tuple(str(value) for value in labels_value) if isinstance(
            labels_value, (list, tuple, set)
        ) else (str(labels_value),) if labels_value else ()
        category = detect_category(document.title, document.content, labels=labels)
        if category.category == "entity-contract" and decision.kind in {"markdown", "html"}:
            entity_values = tuple(self._entity_contract_values(document, decision.kind))
            if entity_values:
                yield from entity_values
                return
        if category.category == "registry-table" and decision.kind in {"markdown", "html"}:
            registry_values = tuple(self._registry_table_values(document, decision.kind))
            if registry_values:
                yield from registry_values
                return
        if category.category == "workflow" and decision.kind in {"markdown", "html"}:
            workflow_values = tuple(self._workflow_values(document, decision.kind))
            if workflow_values:
                yield from workflow_values
                return
        if category.category == "glossary" and decision.kind in {"markdown", "html"}:
            glossary_values = tuple(self._glossary_values(document, decision.kind))
            if glossary_values:
                yield from glossary_values
                return
        if category.category == "index":
            for value in self._section_windows(
                self._entity_contract_text(document.content, decision.kind),
                min(160, self._settings.chunk_max_tokens),
                0,
            ):
                yield value, (document.title,), None, {"chunk_profile": "index"}
            return
        if decision.kind == "markdown":
            yield from self._markdown_values(document)
        elif decision.kind == "csv":
            yield from self._table_values(document)
        elif decision.kind == "html":
            yield from self._html_values(document)
        elif decision.kind in {"json", "log"}:
            yield from self._line_values(document, self._settings.table_chunk_max_tokens)
        else:
            yield from self._section_values(document, self._settings.chunk_max_tokens)

    def _entity_contract_values(self, document: SourceDocument, kind: str):
        text = self._entity_contract_text(document.content, kind)
        heading = re.compile(
            r"(?m)^#{2,4}\s+(?:[\d.]+\s+)?`?([A-Z][A-Z0-9_]+)`?\s+"
            r"(?:[-—]\s+id\s+(\d+)\s*,\s*version\s+([^\s#]+)\s*$|"
            r"\(id\s+(\d+)\))",
            re.IGNORECASE,
        )
        matches = list(heading.finditer(text))
        if matches and text[:matches[0].start()].strip():
            preamble = text[:matches[0].start()].strip()
            for value in self._section_windows(
                preamble, self._settings.chunk_max_tokens, 0
            ):
                yield value, (document.title,), "contract-support", {
                    "chunk_profile": "entity-contract-support"
                }
        for index, match in enumerate(matches):
            entity_key, conventional_id, entity_version, parenthesized_id = match.groups()
            entity_id = conventional_id or parenthesized_id
            section_heading = match.group(0).strip()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            section_body = text[match.end():end].strip()
            section = f"{section_heading}\n{section_body}".strip()
            metadata = {
                "entity_key": entity_key,
                "entity_id": entity_id,
                "chunk_profile": "entity-contract",
            }
            if entity_version:
                metadata["entity_version"] = entity_version.rstrip(".,;")
            path = (entity_key,)
            if self._count_tokens(section) <= self._settings.chunk_max_tokens:
                yield section, path, entity_key, metadata
                continue
            yield from self._entity_contract_row_groups(
                section_heading,
                section_body,
                path,
                entity_key,
                metadata,
            )

    def _entity_contract_text(self, content: str, kind: str) -> str:
        if kind != "html":
            return content
        soup = BeautifulSoup(content, "html.parser")
        blocks: list[str] = []
        for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "pre", "table"]):
            if node.name not in {"table"} and node.find_parent("table") is not None:
                continue
            if node.name == "table":
                rendered = _html_table_text(node)
                if rendered:
                    blocks.append(rendered)
                continue
            value = _clean(node.get_text(" "))
            if not value:
                continue
            if node.name and node.name.startswith("h"):
                blocks.append(f"{'#' * int(node.name[1])} {value}")
            else:
                blocks.append(value)
        return "\n".join(blocks)

    def _entity_contract_row_groups(
        self,
        section_heading: str,
        section_body: str,
        path: tuple[str, ...],
        locator: str,
        metadata: dict[str, object],
    ):
        lines = [line for line in section_body.splitlines() if line.strip()]
        table_start = next((index for index, line in enumerate(lines) if line.count("|") >= 2), None)
        if table_start is None or table_start + 2 > len(lines):
            for value in self._section_windows(
                section_body,
                self._settings.chunk_max_tokens - self._count_tokens(section_heading),
                0,
            ):
                yield f"{section_heading}\n{value}", path, locator, metadata
            return
        prose = "\n".join(lines[:table_start]).strip()
        if prose:
            for value in self._section_windows(
                prose,
                self._settings.chunk_max_tokens - self._count_tokens(section_heading),
                0,
            ):
                yield f"{section_heading}\n{value}", path, locator, metadata
        header = lines[table_start:table_start + 2]
        rows = lines[table_start + 2:]
        prefix = [section_heading, *header]
        current = list(prefix)
        for row in rows:
            candidate = "\n".join([*current, row])
            if len(current) > len(prefix) and self._count_tokens(candidate) > self._settings.chunk_max_tokens:
                yield "\n".join(current), path, locator, metadata
                current = [*prefix, row]
            else:
                current.append(row)
        if len(current) > len(prefix):
            yield "\n".join(current), path, locator, metadata

    def _registry_table_values(self, document: SourceDocument, kind: str):
        text = self._entity_contract_text(document.content, kind)
        lines = text.splitlines()
        tables: list[tuple[int, int]] = []
        cursor = 0
        while cursor < len(lines):
            if lines[cursor].count("|") < 2:
                cursor += 1
                continue
            end = cursor
            while end < len(lines) and lines[end].count("|") >= 2:
                end += 1
            if end - cursor >= 2:
                tables.append((cursor, end))
            cursor = end
        if not tables:
            return
        table_lines = {line for start, end in tables for line in range(start, end)}
        prose = "\n".join(line for index, line in enumerate(lines) if index not in table_lines).strip()
        for value in self._section_windows(prose, self._settings.chunk_max_tokens, 0):
            yield value, (document.title,), "registry-support", {
                "chunk_profile": "registry-support"
            }
        for table_number, (start, end) in enumerate(tables, start=1):
            block = [line.strip() for line in lines[start:end] if line.strip()]
            if len(block) < 2:
                continue
            caption = next(
                (
                    re.sub(r"^#{1,6}\s+", "", lines[index]).strip()
                    for index in range(start - 1, -1, -1)
                    if re.match(r"^#{1,6}\s+", lines[index])
                ),
                f"table-{table_number}",
            )
            header, rows = block[:2], block[2:]
            row_keys = tuple(
                dict.fromkeys(
                    cell
                    for row in rows
                    for cell in [row.strip().strip("|").split("|", 1)[0].strip().strip("`")]
                    if cell and not set(cell) <= {"-", ":"}
                )
            )
            metadata = {
                "chunk_profile": "registry-table",
                "table_caption": caption,
            }
            current = list(header)
            current_keys: list[str] = []
            for row in rows:
                key = row.strip().strip("|").split("|", 1)[0].strip().strip("`")
                candidate = "\n".join([*current, row])
                if len(current) > len(header) and self._count_tokens(candidate) > self._settings.table_chunk_max_tokens:
                    yield "\n".join(current), (caption,), f"table:{table_number}", {
                        **metadata, "row_keys": tuple(current_keys)
                    }
                    current, current_keys = [*header, row], [key]
                else:
                    current.append(row)
                    if key:
                        current_keys.append(key)
            if len(current) > len(header):
                yield "\n".join(current), (caption,), f"table:{table_number}", {
                    **metadata, "row_keys": tuple(current_keys)
                }
            if row_keys:
                key_text = f"{caption} keys: " + ", ".join(row_keys)
                for value in self._section_windows(
                    key_text, self._settings.chunk_max_tokens, 0
                ):
                    yield value, (caption, "keys"), f"table:{table_number}:keys", {
                        **metadata,
                        "chunk_profile": "registry-key-list",
                        "row_keys": row_keys,
                    }

    def _workflow_values(self, document: SourceDocument, kind: str):
        text = self._entity_contract_text(document.content, kind)
        heading = re.compile(
            r"(?m)^#{1,4}\s+.*?\[([A-Z][A-Z0-9]*-\d+)\].*$",
            re.IGNORECASE,
        )
        matches = list(heading.finditer(text))
        if matches and text[:matches[0].start()].strip():
            for value in self._section_windows(
                text[:matches[0].start()].strip(),
                self._settings.chunk_max_tokens,
                0,
            ):
                yield value, (document.title,), "workflow-support", {
                    "chunk_profile": "workflow-support"
                }
        for index, match in enumerate(matches):
            flow_id = match.group(1).upper()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            section = text[match.start():end].strip()
            step_count = len(re.findall(r"(?m)^\s*\d+[.)]\s+", section))
            metadata = {
                "chunk_profile": "workflow",
                "flow_id": flow_id,
                "step_count": step_count,
            }
            if self._count_tokens(section) <= self._settings.chunk_max_tokens:
                yield section, (flow_id,), flow_id, metadata
                continue

            lines = section.splitlines()
            flow_heading = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            heading_level = len(flow_heading) - len(flow_heading.lstrip("#"))
            nested_heading = re.compile(
                rf"(?m)^#{{{min(heading_level + 1, 6)},6}}\s+.+$"
            )
            nested_matches = list(nested_heading.finditer(body))
            segments: list[str] = []
            if nested_matches:
                if body[:nested_matches[0].start()].strip():
                    segments.append(body[:nested_matches[0].start()].strip())
                for segment_index, nested in enumerate(nested_matches):
                    segment_end = (
                        nested_matches[segment_index + 1].start()
                        if segment_index + 1 < len(nested_matches)
                        else len(body)
                    )
                    segments.append(body[nested.start():segment_end].strip())
            elif body:
                segments.append(body)

            available = max(
                64,
                self._settings.chunk_max_tokens - self._count_tokens(flow_heading),
            )
            part_number = 0
            for segment in segments:
                segment_heading = re.match(r"^#{1,6}\s+(.+)$", segment)
                path = (
                    (flow_id, _clean(segment_heading.group(1)))
                    if segment_heading
                    else (flow_id,)
                )
                candidate = f"{flow_heading}\n{segment}".strip()
                if self._count_tokens(candidate) <= self._settings.chunk_max_tokens:
                    part_number += 1
                    yield candidate, path, f"{flow_id}:part:{part_number}", metadata
                    continue
                if re.search(r"(?m)^\s*\d+[.)]\s+", segment):
                    raise ValueError(
                        f"Workflow {flow_id} contains a numbered sequence too large to embed atomically"
                    )
                for value in self._section_windows(segment, available, 0):
                    part_number += 1
                    yield (
                        f"{flow_heading}\n{value}",
                        path,
                        f"{flow_id}:part:{part_number}",
                        metadata,
                    )

    def _glossary_values(self, document: SourceDocument, kind: str):
        text = self._entity_contract_text(document.content, kind)
        heading = re.compile(r"(?m)^#{2,4}\s+([^\n]+)$")
        matches = list(heading.finditer(text))
        if matches:
            for index, match in enumerate(matches):
                term = _clean(match.group(1)).strip("`")
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                body = text[match.end():end].strip()
                if body:
                    yield f"{term}: {body}", (term,), term, {
                        "chunk_profile": "glossary", "term": term
                    }
            return
        for line in text.splitlines():
            match = re.match(r"^\s*([^:]{2,80}):\s+(.+)$", line)
            if match:
                term, definition = (_clean(value) for value in match.groups())
                yield f"{term}: {definition}", (term,), term, {
                    "chunk_profile": "glossary", "term": term
                }

    def _docling_artifact(self, document: SourceDocument) -> StructuredArtifact:
        try:
            from docling.chunking import HybridChunker
            from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
            from docling.datamodel.base_models import DocumentStream, InputFormat
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions,
                TesseractCliOcrOptions,
            )
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
            from transformers import AutoTokenizer

            embedding_tokenizer = AutoTokenizer.from_pretrained(
                self._settings.embedding_tokenizer, local_files_only=True
            )
            # Docling receives the explicit chunk budget below.  Raise the
            # tokenizer inspection limit so long source documents are not
            # reported as model inputs before Docling splits them.
            embedding_tokenizer.model_max_length = max(
                int(getattr(embedding_tokenizer, "model_max_length", 0) or 0),
                1_000_000,
            )
            tokenizer = HuggingFaceTokenizer(
                tokenizer=embedding_tokenizer,
                max_tokens=self._settings.chunk_max_tokens,
            )
            artifacts_path = Path(self._settings.docling_artifacts_path) if self._settings.docling_artifacts_path else None
            pdf_options = PdfPipelineOptions(
                artifacts_path=artifacts_path,
                enable_remote_services=False,
                allow_external_plugins=False,
                document_timeout=self._settings.docling_timeout_seconds,
                do_ocr=True,
                generate_page_images=self._settings.visual_analysis_enabled,
                generate_picture_images=self._settings.visual_analysis_enabled,
                generate_table_images=self._settings.visual_analysis_enabled,
                do_picture_classification=self._settings.visual_analysis_enabled,
                ocr_options=TesseractCliOcrOptions(lang=["eng", "spa"]),
                accelerator_options=AcceleratorOptions(
                    num_threads=2, device=AcceleratorDevice.CPU
                ),
            )
            converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)}
            )
            converted = converter.convert(
                DocumentStream(name=document.title, stream=BytesIO(document.content_bytes or b"")),
                max_file_size=self._settings.max_attachment_bytes,
                max_num_pages=self._settings.docling_max_pages,
            ).document
            chunker = HybridChunker(tokenizer=tokenizer, merge_peers=True)
            elements: list[LogicalElement] = []
            for chunk in chunker.chunk(dl_doc=converted):
                headings = tuple(
                    str(value)
                    for value in (getattr(chunk.meta, "headings", None) or [])
                    if value
                )
                captions = tuple(
                    str(value)
                    for value in (getattr(chunk.meta, "captions", None) or [])
                    if value
                )
                locator = _docling_locator(chunk)
                elements.append(
                    LogicalElement(
                        kind=document.source_type,
                        text=chunk.text,
                        heading_path=headings,
                        locator=locator,
                        metadata={"captions": list(captions)},
                    )
                )
            visual = (
                analyze_docling(document, converted, self._settings)
                if self._settings.visual_analysis_enabled
                else VisualAnalysis(False)
            )
            if not elements and visual.assets:
                elements.extend(
                    LogicalElement(
                        kind="VISUAL",
                        text=asset.caption or f"{asset.asset_type.replace('_', ' ')} on {asset.locator or 'document'}",
                        locator=asset.locator,
                        metadata={"kind": "VISUAL"},
                    )
                    for asset in visual.assets
                )
            return StructuredArtifact(
                parsing=ParsingResult(
                    schema_version=self._settings.schema_version,
                    parser_version=self._settings.parser_version,
                    source_id=document.source_id,
                    source_version=document.version,
                    mime_type=document.mime_type,
                    language=document.language,
                    security_classification=document.security_classification,
                    elements=tuple(elements),
                    warnings=visual.reason_codes,
                ),
                visual=visual,
            )
        except ImportError:
            pass
        fallback = attachment_to_text(
            document.content_bytes or b"", document.mime_type, document.title
        )
        if fallback is None:
            raise RuntimeError(
                "Docling worker dependencies are required for this document format."
            )
        fallback_document = SourceDocument(
            project_id=document.project_id,
            provider=document.provider,
            source_id=document.source_id,
            source_type=document.source_type,
            title=document.title,
            reference=document.reference,
            source_url=document.source_url,
            version=document.version,
            content=fallback,
            updated_at=document.updated_at,
            metadata=document.metadata,
            mime_type="text/plain",
            language=document.language,
            security_classification=document.security_classification,
        )
        elements = tuple(
            LogicalElement(
                kind=document.source_type,
                text=content,
                heading_path=path,
                locator=locator,
                metadata=metadata,
            )
            for content, path, locator, metadata in self._section_values(
                fallback_document, self._settings.chunk_max_tokens
            )
        )
        return StructuredArtifact(
            parsing=ParsingResult(
                schema_version=self._settings.schema_version,
                parser_version=self._settings.parser_version,
                source_id=document.source_id,
                source_version=document.version,
                mime_type=document.mime_type,
                language=document.language,
                security_classification=document.security_classification,
                elements=elements,
                warnings=("DOCLING_UNAVAILABLE_TEXT_FALLBACK",),
            ),
            visual=VisualAnalysis(False, reason_codes=("DOCLING_UNAVAILABLE",)),
        )

    def _markdown_values(self, document: SourceDocument):
        splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")],
            strip_headers=False,
        )
        for part in splitter.split_text(document.content):
            path = tuple(str(part.metadata.get(key)) for key in ("h1", "h2", "h3", "h4") if part.metadata.get(key))
            for value in self._section_windows(part.page_content, self._settings.chunk_max_tokens, self._settings.chunk_overlap_tokens):
                yield value, path, None, {}

    def _code_values(self, document: SourceDocument, language: Language):
        splitter = RecursiveCharacterTextSplitter(
            separators=RecursiveCharacterTextSplitter.get_separators_for_language(language),
            chunk_size=self._settings.code_chunk_max_tokens,
            chunk_overlap=min(
                self._settings.chunk_overlap_tokens,
                self._settings.code_chunk_max_tokens // 4,
            ),
            length_function=self._count_tokens,
            keep_separator=True,
        )
        file_name = Path(str(document.metadata.get("path") or document.title)).name
        values = self._merge_code_fragments(
            splitter.split_text(document.content),
            self._settings.code_chunk_max_tokens,
        )
        for value in values:
            symbols = _code_symbols(value)
            symbol = symbols[0] if symbols else None
            heading_path = (file_name, *symbols[:2]) if file_name else tuple(symbols[:2])
            yield value, heading_path, symbol, {
                "symbol": symbol or "",
                "symbols": list(symbols),
                "file_name": file_name,
            }


    def _merge_code_fragments(
        self, values: list[str], maximum_tokens: int
    ) -> list[str]:
        """Attach annotation/package/import-only splitter fragments to real code."""

        merged: list[str] = []
        pending: list[str] = []
        for value in values:
            cleaned = value.strip()
            if not cleaned:
                continue
            if self._low_information_code_fragment(cleaned):
                pending.append(cleaned)
                continue
            prefix = "\n".join(pending)
            candidate = f"{prefix}\n{cleaned}" if prefix else cleaned
            if self._count_tokens(candidate) <= maximum_tokens:
                merged.append(candidate)
            else:
                merged.extend(pending)
                merged.append(cleaned)
            pending = []
        if pending:
            tail = "\n".join(pending)
            if merged and self._count_tokens(f"{merged[-1]}\n{tail}") <= maximum_tokens:
                merged[-1] = f"{merged[-1]}\n{tail}"
            else:
                merged.append(tail)
        return merged

    def _low_information_code_fragment(self, value: str) -> bool:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if not lines:
            return True
        structural = all(
            line.startswith(("@", "package ", "import ", "//", "/*", "*", "*/"))
            for line in lines
        )
        return structural or (
            len(value) < 48 and len(re.findall(r"[A-Za-z_]\w*", value)) <= 3
        )

    def _table_values(self, document: SourceDocument):
        lines = [line for line in document.content.splitlines() if line.strip()]
        if not lines:
            return
        for index, value in enumerate(
            self._table_windows(lines, self._settings.table_chunk_max_tokens)
        ):
            yield value, (str(document.metadata.get("sheet") or "table"),), f"rows:{index + 1}", {}

    def _html_values(self, document: SourceDocument):
        soup = BeautifulSoup(document.content, "html.parser")
        boundary = "PI_SEMANTIC_BLOCK_BOUNDARY"
        for node in list(soup.find_all(["table", "ul", "ol", "pre"])):
            if node.find_parent(["table", "ul", "ol", "pre"]) is not None:
                continue
            marker = soup.new_tag("p")
            marker.string = boundary
            node.insert_after(marker)
        splitter = HTMLSemanticPreservingSplitter(
            headers_to_split_on=[
                ("h1", "h1"),
                ("h2", "h2"),
                ("h3", "h3"),
                ("h4", "h4"),
            ],
            elements_to_preserve=["table", "ul", "ol", "pre", "code"],
            max_chunk_size=max(256, self._settings.chunk_max_tokens * 4),
            preserve_parent_metadata=True,
            custom_handlers={
                "table": _html_table_markdown,
                "ul": lambda node: _html_list_text(node, ordered=False),
                "ol": lambda node: _html_list_text(node, ordered=True),
                "pre": _html_pre_text,
                "code": lambda node: f"`{_clean(node.get_text(' '))}`",
            },
        )
        for part in splitter.split_text(str(soup)):
            content = part.page_content.replace(boundary, "\n\n").strip()
            path = tuple(
                str(part.metadata[key])
                for key in ("h1", "h2", "h3", "h4")
                if part.metadata.get(key)
            )
            for value in self._section_windows(
                content,
                self._settings.chunk_max_tokens,
                self._settings.chunk_overlap_tokens,
            ):
                yield value, path, None, {}

    def _line_values(self, document: SourceDocument, max_tokens: int):
        # No header to repeat here, but the budget is still a token budget: a
        # word-counted window of log or JSON lines overshot it badly, because
        # punctuation and identifiers dominate.
        for index, value in enumerate(self._packed_lines(document.content.splitlines(), max_tokens)):
            yield value, (), f"lines:{index + 1}", {}

    def _packed_lines(self, lines: Iterable[str], maximum: int) -> list[str]:
        """Group whole lines up to a token budget, never splitting a line."""

        windows: list[str] = []
        current: list[str] = []
        cost = 0
        for line in lines:
            if not line.strip():
                continue
            line_cost = self._count_tokens(line)
            if current and cost + line_cost > maximum:
                windows.append("\n".join(current))
                current, cost = [], 0
            current.append(line)
            cost += line_cost
            if line_cost >= maximum:
                # A single line at or over the budget is emitted alone rather
                # than dragging a neighbour over the limit with it.
                windows.append("\n".join(current))
                current, cost = [], 0
        if current:
            windows.append("\n".join(current))
        return windows

    def _issue_values(self, document: SourceDocument):
        sections = document.metadata.get("_jira_sections")
        if isinstance(sections, list):
            for section in sections:
                kind = section["kind"]
                maximum = min(self._settings.chunk_max_tokens, 300 if kind in {"COMMENT", "CHANGELOG", "WORKLOG", "CUSTOM_FIELD", "RELATIONSHIP"} else 420)
                for index, value in enumerate(self._prose_windows(section["text"], maximum, self._settings.chunk_overlap_tokens)):
                    yield value, (document.reference, kind, section["locator"]), f"{section['locator']}:{index + 1}", {
                        "kind": "JIRA_" + kind, "jira_chunk_kind": kind,
                        **{k: v for k, v in section.items() if k not in {"kind", "text", "locator"}},
                    }
            return
        parts = re.split(r"(?m)^## ([^\n]+)\n", document.content)
        context = " · ".join(
            value
            for value in (
                str(document.metadata.get("issue_key") or document.reference),
                str(document.metadata.get("status") or ""),
            )
            if value
        )
        if len(parts) == 1:
            yield from self._section_values(document, self._settings.chunk_max_tokens)
            return
        for title, content in zip(parts[1::2], parts[2::2]):
            maximum = (
                self._settings.table_chunk_max_tokens
                if title.lower() == "comments"
                else self._settings.chunk_max_tokens
            )
            for index, value in enumerate(
                self._prose_windows(content, maximum, self._settings.chunk_overlap_tokens)
            ):
                yield value, (context, title), f"{title.lower()}:{index + 1}", {
                    "kind": f"JIRA_{title.upper().replace(' ', '_')}"
                }

    def _section_values(self, document: SourceDocument, max_tokens: int):
        for index, value in enumerate(self._prose_windows(document.content, max_tokens, self._settings.chunk_overlap_tokens)):
            yield value, (), f"section:{index + 1}", {}

    def _fit_embedding_text(
        self, *, required: str, optional: tuple[str, ...], body: str
    ) -> str:
        """Assemble the embedded passage without ever truncating the body.

        This previously took the first `chunk_max_tokens` window of
        prefix + enrichment + body and discarded the remainder, so whenever the
        enrichment header pushed the total over the budget the *end of the
        passage* was dropped from the vector while chunk_text kept it. Measured on
        the event-contract document, one chunk in 96 lost 62 trailing words from
        its vector -- and with the real tokenizer, which costs more per word on
        identifier-dense text, more would.

        Enrichment exists to help retrieval find the passage. Losing the passage
        to make room for it inverts the purpose, so the order of sacrifice is:
        drop optional enrichment lines from the least useful first, and only if
        the body alone still exceeds the embedder's positions is anything cut
        from the body -- which is then genuinely unavoidable and is reported by
        scripts/audit_chunk_quality.py.
        """

        # SentenceTransformersTokenTextSplitter.count_tokens includes the model's
        # start and stop tokens, so the limit is the model position count itself.
        limit = min(_EMBEDDER_POSITION_LIMIT, self._settings.embedding_position_limit)
        lines = [line for line in optional if line and line.strip()]
        while True:
            candidate = "\n".join([required, *lines, body]) if lines else f"{required}\n{body}"
            if self._count_tokens(candidate) <= limit or not lines:
                break
            # Least specific first: the metadata block and visual context repeat
            # information available elsewhere, while SOURCE TYPE and REFERENCE are
            # what a filtered query matches on.
            lines.pop()
        if self._count_tokens(candidate) <= limit:
            return candidate
        # The body alone does not fit. Keep the required context and take as much
        # of the body as the model can actually read.
        head = self._token_windows(body, max(limit - self._count_tokens(required) - 1, 1), 0)
        return f"{required}\n{head[0]}" if head else required

    def _count_tokens(self, value: str) -> int:
        """Token count from the sentence-transformer splitter, including specials.

        Every size limit in this module is expressed in embedding tokens, so
        measuring anything in words silently changes the limit: identifier-dense
        text such as POS_CLOSE_SHIFT_REQUEST_EVENT costs several tokens per word,
        and a window sized by words overshoots the embedder's 512 positions and
        is truncated with no error. The cache matters because table packing asks
        for the same row length repeatedly.
        """

        cached = self._token_counts.get(value)
        if cached is not None:
            return cached
        count = self._token_splitter().count_tokens(text=value)
        if len(self._token_counts) < _TOKEN_CACHE_ENTRIES:
            self._token_counts[value] = count
        return count

    def _token_splitter(
        self,
        *,
        tokens_per_chunk: int = 510,
        chunk_overlap: int = 0,
    ) -> SentenceTransformersTokenTextSplitter:
        if (
            tokens_per_chunk == 510
            and chunk_overlap == 0
            and self._sentence_splitter is not None
        ):
            return self._sentence_splitter
        if self._embedding_model is None:
            if self._embedding_model_loader is not None:
                self._embedding_model = self._embedding_model_loader()
            else:
                # Unit tools and one-off chunk audits do not construct the vector
                # store. They still use the real pinned tokenizer, but do not load
                # a second copy of the 2.2 GB transformer just to count tokens.
                from types import SimpleNamespace

                from transformers import AutoTokenizer

                tokenizer = AutoTokenizer.from_pretrained(
                    self._settings.embedding_tokenizer,
                    local_files_only=True,
                )
                self._embedding_model = SimpleNamespace(
                    tokenizer=tokenizer,
                    max_seq_length=_EMBEDDER_POSITION_LIMIT,
                    model_card_data="local-embedding-tokenizer",
                )
        splitter = _SharedSentenceTransformerSplitter(
            self._embedding_model,
            tokens_per_chunk=tokens_per_chunk,
            chunk_overlap=chunk_overlap,
        )
        if tokens_per_chunk == 510 and chunk_overlap == 0:
            self._sentence_splitter = splitter
        return splitter

    def _section_windows(self, value: str, maximum: int, overlap: int) -> list[str]:
        """Window a section without cutting a table off from its header row.

        _token_windows slides over character offsets, so an oversized section
        containing a table is split mid-table and every window after the first
        loses the header. The rows survive as evidence but stop meaning anything:
        "116 | POS_CLOSE_SHIFT_REQUEST | shift" cannot be read without knowing
        the columns. The CSV path already solved this by repeating the header
        into each window, so table blocks are routed through the same helper and
        only prose is windowed by tokens.
        """

        windows: list[str] = []
        for is_table, lines in _blocks(value):
            if is_table:
                windows.extend(self._table_windows(lines, maximum))
                continue
            text = "\n".join(lines).strip()
            if text:
                windows.extend(self._prose_windows(text, maximum, overlap))
        return [window for window in windows if window.strip()]

    def _table_windows(self, lines: list[str], maximum: int) -> list[str]:
        """Pack table rows into windows that never split a row or lose a header.

        Sized in embedding tokens, not words. The previous helper counted words,
        so a table packed to a 300-word budget could reach roughly 450 tokens of
        identifier-dense content and, once the header was repeated, exceed the
        embedder's 512 positions -- truncating the last rows out of the vector
        while leaving them visible in chunk_text.

        Three guarantees, in priority order: a row is never split across windows;
        every window carries the header (and the Markdown separator, or the
        repeat stops parsing as a table); and a window never exceeds the budget
        unless one indivisible row does, which is reported by widening rather than
        hidden by truncation.
        """

        header_lines, body = _table_header(lines)
        if not body:
            return ["\n".join(lines).strip()]
        header = "\n".join(header_lines)
        header_cost = self._count_tokens(header) if header else 0
        # The header is non-negotiable: a row without its column names is not
        # evidence. So the budget bends to fit at least one row per window, and
        # the configured limit is treated as a target rather than a hard wall.
        available = max(maximum - header_cost, 1)

        windows: list[str] = []
        current: list[str] = []
        current_cost = 0
        for row in body:
            row_cost = self._count_tokens(row)
            # Only an indivisible row that cannot fit the embedder's positions
            # even on its own is split by column. Overshooting the configured
            # target is acceptable; overshooting the model's 512 positions is
            # not, because that truncates silently.
            if header_cost + row_cost > _EMBEDDER_POSITION_LIMIT:
                if current:
                    windows.append(_join(header, current))
                    current, current_cost = [], 0
                windows.extend(
                    _wide_row_windows(
                        header_lines,
                        row,
                        max(_EMBEDDER_POSITION_LIMIT - header_cost, 1),
                        self._count_tokens,
                    )
                )
                continue
            if current and current_cost + row_cost > available:
                windows.append(_join(header, current))
                current, current_cost = [], 0
            current.append(row)
            current_cost += row_cost
        if current:
            windows.append(_join(header, current))
        return windows

    def _prose_windows(self, value: str, maximum: int, overlap: int) -> list[str]:
        """Split prose on the largest natural boundary that fits.

        _token_windows cuts at a token offset, so it lands mid-sentence and often
        mid-word: "the shift is closed when the ca" is a real chunk boundary it
        can produce. LangChain's RecursiveCharacterTextSplitter tries paragraph,
        then line, then sentence, then word separators in order and only falls
        back to a hard cut when nothing fits, which is exactly the behaviour
        wanted here -- and it is measured with the embedding tokenizer rather than
        characters, so the budget still means what it says.
        """

        if not value.strip():
            return []
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=maximum,
            chunk_overlap=min(overlap, max(0, maximum // 4)),
            length_function=self._count_tokens,
            separators=["\n\n", "\n", ". ", "; ", ", ", " ", ""],
            keep_separator=True,
        )
        windows: list[str] = []
        for window in splitter.split_text(value):
            stripped = window.strip()
            if not stripped:
                continue
            if self._count_tokens(stripped) <= maximum:
                windows.append(stripped)
                continue
            windows.extend(self._token_windows(stripped, maximum, overlap))
        return windows

    def _token_windows(self, value: str, maximum: int, overlap: int) -> list[str]:
        # The splitter strips the two special-token ids before windowing. Reserve
        # them explicitly so count_tokens(text=window) can never exceed maximum.
        content_tokens = max(1, maximum - 2)
        content_overlap = min(overlap, max(0, content_tokens - 1))
        return [
            item.strip()
            for item in self._token_splitter(
                tokens_per_chunk=content_tokens,
                chunk_overlap=content_overlap,
            ).split_text(value)
            if item.strip()
        ]



def _html_list_text(node, *, ordered: bool) -> str:
    items = [
        _clean(item.get_text(" "))
        for item in node.find_all("li", recursive=False)
        if _clean(item.get_text(" "))
    ]
    return "\n".join(
        f"{index}. {value}" if ordered else f"- {value}"
        for index, value in enumerate(items, start=1)
    )


def _html_pre_text(node) -> str:
    value = node.get_text("\n").strip()
    return f"```\n{value}\n```" if value else ""


def _html_table_text(table) -> str:
    """Render an HTML table row-wise, keeping cell boundaries.

    get_text() joins every cell with a space, which destroys the only thing a
    table carries: which value belongs to which column. A payload-field table
    became an undifferentiated run of words, so a question about one field
    retrieved the whole table and could not be answered from it. Pipes match how
    the same content is chunked when it arrives as Markdown, so a page and its
    source document produce comparable evidence.
    """

    rows: list[str] = []
    for row in table.find_all("tr"):
        cells = [
            " ".join(cell.get_text(" ").split())
            for cell in row.find_all(["th", "td"])
        ]
        if any(cells):
            rows.append(" | ".join(cells))
    if not rows:
        # A table with no rows still may carry text in a caption or stray cell.
        return " ".join(table.get_text(" ").split())
    markdown_rows = [f"| {row} |" for row in rows]
    separator = "| " + " | ".join("---" for _ in rows[0].split(" | ")) + " |"
    return "\n".join([markdown_rows[0], separator, *markdown_rows[1:]])


def _html_table_markdown(table) -> str:
    """Render preserved HTML tables as parseable Markdown evidence."""

    return _html_table_text(table)




def _declared_format(suffix: str, document: SourceDocument) -> str | None:
    """Map an authoritative extension or MIME type onto a routing decision."""

    if suffix in {".md", ".markdown"} or document.mime_type == "text/markdown":
        return "markdown"
    if suffix in {".csv", ".tsv"} or document.mime_type in {
        "text/csv",
        "text/tab-separated-values",
    }:
        return "csv"
    if suffix in {".htm", ".html"} or document.mime_type == "text/html":
        return "html"
    if suffix in {".json", ".xml", ".yaml", ".yml"}:
        return "json"
    if suffix == ".log":
        return "log"
    return None


def _blocks(value: str) -> list[tuple[bool, list[str]]]:
    """Group consecutive lines into table and non-table runs."""

    blocks: list[tuple[bool, list[str]]] = []
    for line in value.splitlines():
        is_row = line.count("|") >= 2
        if blocks and blocks[-1][0] == is_row:
            blocks[-1][1].append(line)
        else:
            blocks.append((is_row, [line]))
    # A lone row is not a table: it is a sentence that happens to contain pipes.
    return [
        (is_row and len(lines) > 1, lines) for is_row, lines in blocks
    ]


def _table_header(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split a table into its header lines and its data rows."""

    if not lines:
        return [], []
    separator = len(lines) > 1 and set(lines[1].replace("|", "").strip()) <= {"-", ":", " "}
    body_start = 2 if separator else 1
    return lines[:body_start], lines[body_start:]


def _join(header: str, rows: list[str]) -> str:
    return "\n".join(([header] if header else []) + rows).strip()


def _wide_row_windows(
    header_lines: list[str], row: str, available: int, count_tokens
) -> list[str]:
    """Split one oversized row by columns, keeping each part labelled.

    A row that cannot fit even alone would otherwise be truncated at embedding
    time, losing its last columns with no signal. Splitting on cell boundaries
    and carrying the matching header cells keeps every value retrievable and
    still says which column it came from -- which is the whole point of a table.
    """

    header_cells = _cells(header_lines[0]) if header_lines else []
    row_cells = _cells(row)
    if len(row_cells) < 2:
        # Nothing to split on. Emit as-is; the auditor reports the length.
        return [_join("\n".join(header_lines), [row])]

    windows: list[str] = []
    group_headers: list[str] = []
    group_cells: list[str] = []
    cost = 0
    for index, cell in enumerate(row_cells):
        label = header_cells[index] if index < len(header_cells) else f"column {index + 1}"
        piece_cost = count_tokens(f"{label} | {cell}")
        if group_cells and cost + piece_cost > available:
            windows.append(_column_group(group_headers, group_cells))
            group_headers, group_cells, cost = [], [], 0
        group_headers.append(label)
        group_cells.append(cell)
        cost += piece_cost
    if group_cells:
        windows.append(_column_group(group_headers, group_cells))
    return windows


def _column_group(headers: list[str], cells: list[str]) -> str:
    return " | ".join(headers) + "\n" + " | ".join(cells)


def _cells(row: str) -> list[str]:
    stripped = row.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _whitespace_windows(value: str, maximum: int, overlap: int) -> list[str]:
    tokens = re.findall(r"\S+", value)
    if not tokens:
        return []
    step = max(1, maximum - overlap)
    return [" ".join(tokens[start : start + maximum]) for start in range(0, len(tokens), step)]


def _docling_locator(chunk: object) -> str | None:
    meta = getattr(chunk, "meta", None)
    for item in getattr(meta, "doc_items", []) or []:
        provenance = getattr(item, "prov", None) or []
        if provenance:
            page = getattr(provenance[0], "page_no", None)
            if page is not None:
                return f"page:{page}"
    return None


def _locator_page(locator: str | None) -> int | None:
    match = re.fullmatch(r"page:(\d+)", locator or "")
    return int(match.group(1)) if match else None


def _code_symbol(value: str) -> str | None:
    return next(iter(_code_symbols(value)), None)


def _code_symbols(value: str) -> tuple[str, ...]:
    modifiers = (
        r"(?:(?:public|private|protected|internal|open|abstract|sealed|data|enum|"
        r"annotation|value|inline|tailrec|suspend|operator|infix|override|actual|"
        r"expect|final|external)\s+)*"
    )
    return tuple(dict.fromkeys(
        match.group(1)
        for match in re.finditer(
        rf"(?m)^\s*(?:async\s+)?{modifiers}"
        r"(?:class|interface|object|def|fun|function)\s+([A-Za-z_][A-Za-z0-9_]*)",
        value,
        )
    ))


_KEYWORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:#-]{2,}")


def _searchable_metadata(
    document: SourceDocument, element: LogicalElement, content: str
) -> dict[str, object]:
    """Build generic searchable fields without changing visible evidence text."""

    source_metadata = document.metadata
    path = str(source_metadata.get("path") or document.title or "")
    repository = str(source_metadata.get("repository") or "")
    branch = str(source_metadata.get("branch") or "")
    issue_key = str(source_metadata.get("issue_key") or "")
    project_key = str(source_metadata.get("project_key") or "")
    symbol = str(element.metadata.get("symbol") or "")
    element_symbols = element.metadata.get("symbols")
    if isinstance(element_symbols, (list, tuple, set)):
        element_symbol_values = tuple(str(item) for item in element_symbols)
    elif element_symbols:
        element_symbol_values = (str(element_symbols),)
    else:
        element_symbol_values = ()
    symbols = tuple(
        dict.fromkeys(
            value
            for value in (
                symbol,
                *element_symbol_values,
                *_code_symbols(content),
            )
            if value
        )
    )
    keyword_inputs = (
        document.title,
        document.reference,
        document.source_type,
        path,
        repository,
        branch,
        issue_key,
        project_key,
        *element.heading_path,
        element.locator or "",
        *symbols,
        content,
    )
    keywords: list[str] = []
    seen: set[str] = set()
    for value in keyword_inputs:
        for token in _KEYWORD_RE.findall(str(value)):
            normalized = token.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            keywords.append(token)
            if len(keywords) >= 32:
                break
        if len(keywords) >= 32:
            break
    return {
        "repository": repository,
        "branch": branch,
        "path": path,
        "file_name": Path(path).name if path else "",
        "issue_key": issue_key,
        "project_key": project_key,
        "symbol": symbol,
        "symbols": list(symbols),
        # Jira already carries structured issue context and routing vocabulary.
        # Synthetic keyword fragments add no source evidence and can form an
        # artificial credential when the unchanged security scanner checks
        # ordered metadata. Keep every original source value/body; omit only
        # this optional, newly generated augmentation for Jira.
        "important_kwd": [] if document.provider.upper() == "JIRA" else keywords,
        "chunk_kind": element.kind,
        "chunk_char_count": len(content),
        "chunk_token_count": len(re.findall(r"\S+", content)),
    }


def _metadata_context(metadata: dict[str, object]) -> str:
    values = []
    for label, key in (
        ("REPOSITORY", "repository"),
        ("BRANCH", "branch"),
        ("PATH", "path"),
        ("FILE", "file_name"),
        ("ISSUE", "issue_key"),
        ("SYMBOLS", "symbols"),
        ("KEYWORDS", "important_kwd"),
    ):
        value = metadata.get(key)
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value if item)
        if value:
            values.append(f"{label}: {value}")
    return "\n".join(values)


def _detect_language(value: str) -> str:
    words = re.findall(r"[a-záéíóúñ]+", value.lower())
    spanish = sum(
        words.count(word)
        for word in ("el", "la", "los", "las", "de", "del", "que", "para", "con", "como", "se")
    )
    english = sum(
        words.count(word)
        for word in ("the", "a", "of", "that", "for", "with", "how", "is")
    )
    if spanish > english:
        return "es"
    if english > spanish:
        return "en"
    return "mixed" if re.search(r"[áéíóúñ¿¡]", value.lower()) else "und"


def _clean(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
