from __future__ import annotations

from io import BytesIO
import hashlib
import posixpath
import re
from typing import Iterable
from bs4 import BeautifulSoup

from app.config import Settings
from app.models import SourceDocument, VisualAnalysis, VisualAsset


_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_MERMAID = re.compile(r"```\s*mermaid\s*\n(.*?)```", re.I | re.S)
_UNSUPPORTED_DIAGRAM = re.compile(r"```\s*(?:plantuml|dot|graphviz)\b", re.I)
_MARKDOWN_TABLE_SEPARATOR = re.compile(
    r"(?m)^\s*\|?(?:\s*:?-{3,}:?\s*\|){1,}\s*:?-{3,}:?\s*\|?\s*$"
)
_ASCII_ARCHITECTURE = re.compile(r"(?:-->|->|=>|\+[-=]{2,}\+|\|\s{2,}\|)")
_MERMAID_EDGE = re.compile(
    r"([A-Za-z][\w-]*)(?:\s*\[([^\]]+)\])?\s*(?:-->|==>|-\.->)\s*"
    r"([A-Za-z][\w-]*)(?:\s*\[([^\]]+)\])?"
)


def analyze_markdown(document: SourceDocument, settings: Settings) -> VisualAnalysis:
    """Detect safe, locally representable Markdown visuals before chunking.

    External resources are never fetched here. Repository-relative image resolution belongs
    to the authenticated provider boundary and is intentionally represented as a reason code
    until bytes are supplied by that boundary.
    """

    value = document.content
    visual_types: set[str] = set()
    reason_codes: list[str] = []
    assets: list[VisualAsset] = []

    if _MARKDOWN_TABLE_SEPARATOR.search(value):
        visual_types.add("table")
        table_text = _first_markdown_table(value)
        if table_text:
            asset = _render_text_asset(document, "markdown_table", table_text, 1)
            if asset:
                assets.append(asset)

    image_targets = [*_MARKDOWN_IMAGE.findall(value), *_HTML_IMAGE.findall(value)]
    for target in image_targets:
        normalized = target.strip().split(maxsplit=1)[0].strip("<>\"'")
        if _unsafe_target(normalized):
            reason_codes.append("UNSAFE_MARKDOWN_IMAGE_REFERENCE")
            continue
        visual_types.add("image")
    document_path = str(document.metadata.get("path") or document.title)
    expected_local = set(resolve_markdown_asset_paths(value, document_path))
    supplied_local = {item.path for item in document.local_visuals}
    if expected_local - supplied_local:
        reason_codes.append("LOCAL_IMAGE_REQUIRES_PROVIDER_RESOLUTION")

    for local in document.local_visuals:
        try:
            from PIL import Image

            image = Image.open(BytesIO(local.content)).convert("RGB")
            if not _meaningful_image(image, settings.visual_min_area_pixels):
                reason_codes.append("INSIGNIFICANT_LOCAL_IMAGE_SKIPPED")
                continue
            output = BytesIO()
            image.save(output, format="WEBP", quality=90, method=6)
            content = output.getvalue()
            assets.append(
                VisualAsset(
                    asset_id=_asset_id(document, None, len(assets) + 1, content),
                    asset_type="image",
                    page_number=None,
                    locator=local.path,
                    caption=posixpath.basename(local.path),
                    ocr_text="",
                    content_hash=hashlib.sha256(content).hexdigest(),
                    media_type="image/webp",
                    content=content,
                )
            )
            visual_types.add("image")
        except Exception:
            reason_codes.append("LOCAL_IMAGE_DECODE_FAILED")

    for ordinal, source in enumerate(_MERMAID.findall(value), start=1):
        visual_types.add("architecture_diagram")
        asset = _render_mermaid_asset(document, source, ordinal)
        if asset:
            assets.append(asset)

    if _ASCII_ARCHITECTURE.search(value):
        visual_types.add("architecture_diagram")
        asset = _render_text_asset(document, "ascii_diagram", value, 1)
        if asset:
            assets.append(asset)

    if _UNSUPPORTED_DIAGRAM.search(value):
        reason_codes.append("UNSUPPORTED_DIAGRAM_RETAINED_AS_TEXT")

    assets = assets[: settings.visual_max_assets_per_document]
    return VisualAnalysis(
        eligible=bool(visual_types),
        visual_types=tuple(sorted(visual_types)),
        eligible_pages=(),
        assets=tuple(assets),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def resolve_markdown_asset_paths(value: str, markdown_path: str) -> tuple[str, ...]:
    """Return normalized, repository-local raster paths only."""
    base = posixpath.dirname(markdown_path.replace("\\", "/"))
    resolved: list[str] = []
    for target in [*_MARKDOWN_IMAGE.findall(value), *_HTML_IMAGE.findall(value)]:
        normalized = target.strip().split(maxsplit=1)[0].strip("<>\"'")
        if _unsafe_target(normalized):
            continue
        path = posixpath.normpath(posixpath.join(base, normalized))
        if path.startswith("../") or path == ".." or posixpath.splitext(path)[1].lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        resolved.append(path)
    return tuple(dict.fromkeys(resolved))


def analyze_docling(
    document: SourceDocument, dl_document: object, settings: Settings
) -> VisualAnalysis:
    visual_types: set[str] = set()
    pages: set[int] = set()
    assets: list[VisualAsset] = []
    reasons: list[str] = []

    try:
        from docling_core.types.doc import PictureItem, TableItem
    except ImportError:
        return VisualAnalysis(False, reason_codes=("DOCLING_VISUAL_TYPES_UNAVAILABLE",))

    for item, _level in dl_document.iterate_items(traverse_pictures=True):
        if len(assets) >= settings.visual_max_assets_per_document:
            reasons.append("VISUAL_ASSET_LIMIT_REACHED")
            break
        if not isinstance(item, (PictureItem, TableItem)):
            continue
        asset_type = "table" if isinstance(item, TableItem) else _picture_type(item)
        if asset_type in {"logo", "icon", "signature", "decorative"}:
            continue
        page = _page_number(item)
        if page is not None and page > settings.visual_max_pages_per_document:
            reasons.append("VISUAL_PAGE_LIMIT_REACHED")
            continue
        image = item.get_image(dl_document)
        if image is None or not _meaningful_image(image, settings.visual_min_area_pixels):
            continue
        payload = BytesIO()
        image.convert("RGB").save(payload, format="WEBP", quality=90, method=6)
        content = payload.getvalue()
        caption = _caption(item)
        asset_id = _asset_id(document, page, len(assets) + 1, content)
        assets.append(
            VisualAsset(
                asset_id=asset_id,
                asset_type=asset_type,
                page_number=page,
                locator=f"page:{page}" if page is not None else None,
                caption=caption,
                ocr_text="",
                content_hash=hashlib.sha256(content).hexdigest(),
                media_type="image/webp",
                content=content,
            )
        )
        visual_types.add(asset_type)
        if page is not None:
            pages.add(page)

    return VisualAnalysis(
        eligible=bool(assets),
        visual_types=tuple(sorted(visual_types)),
        eligible_pages=tuple(sorted(pages)),
        assets=tuple(assets),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def analyze_html(document: SourceDocument, settings: Settings) -> VisualAnalysis:
    soup = BeautifulSoup(document.content, "html.parser")
    visual_types: set[str] = set()
    reasons: list[str] = []
    assets: list[VisualAsset] = []
    for table in soup.find_all("table")[: settings.visual_max_assets_per_document]:
        rows = []
        for row in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            asset = _render_text_asset(document, "html_table", "\n".join(rows), len(assets) + 1)
            if asset:
                assets.append(asset)
                visual_types.add("table")
    if soup.find("img") is not None:
        visual_types.add("image")
        reasons.append("HTML_IMAGE_REQUIRES_PROVIDER_RESOLUTION")
    for macro in soup.find_all(lambda tag: getattr(tag, "name", "") in {"ac:structured-macro", "structured-macro"}):
        name = str(macro.attrs.get("ac:name") or macro.attrs.get("name") or "").lower()
        if name == "mermaid":
            asset = _render_mermaid_asset(document, macro.get_text("\n", strip=True), len(assets) + 1)
            if asset:
                assets.append(asset)
                visual_types.add("architecture_diagram")
        elif name in {"drawio", "gliffy", "plantuml", "graphviz"}:
            visual_types.add("architecture_diagram")
            reasons.append("CONFLUENCE_DIAGRAM_REQUIRES_ATTACHMENT")
    return VisualAnalysis(
        eligible=bool(visual_types),
        visual_types=tuple(sorted(visual_types)),
        assets=tuple(assets[: settings.visual_max_assets_per_document]),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _picture_type(item: object) -> str:
    values: list[str] = []
    for annotation in getattr(item, "annotations", None) or []:
        values.append(str(annotation).lower())
    label = " ".join(values)
    for kind, terms in {
        "architecture_diagram": ("diagram", "flow", "architecture", "network", "sequence"),
        "chart": ("chart", "plot", "graph"),
        "logo": ("logo",),
        "signature": ("signature",),
        "decorative": ("decorative", "background"),
    }.items():
        if any(term in label for term in terms):
            return kind
    return "image"


def _caption(item: object) -> str:
    captions: list[str] = []
    for caption in getattr(item, "captions", None) or []:
        captions.append(str(getattr(caption, "text", None) or caption))
    return " ".join(value.strip() for value in captions if value.strip())[:2000]


def _page_number(item: object) -> int | None:
    provenance = getattr(item, "prov", None) or []
    page = getattr(provenance[0], "page_no", None) if provenance else None
    return int(page) if isinstance(page, int) else None


def _meaningful_image(image: object, minimum_area: int) -> bool:
    width, height = getattr(image, "size", (0, 0))
    return int(width) * int(height) >= minimum_area and width >= 64 and height >= 64


def _unsafe_target(value: str) -> bool:
    lowered = value.lower()
    return (
        not value
        or lowered.startswith(("http://", "https://", "data:", "file:", "javascript:"))
        or value.startswith(("/", "\\"))
        or ".." in value.replace("\\", "/").split("/")
        or lowered.endswith(".svg")
    )


def _first_markdown_table(value: str) -> str:
    lines = value.splitlines()
    for index, line in enumerate(lines):
        if _MARKDOWN_TABLE_SEPARATOR.fullmatch(line):
            start = max(0, index - 1)
            end = index + 1
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            return "\n".join(lines[start:end])
    return ""


def _render_text_asset(
    document: SourceDocument, asset_type: str, value: str, ordinal: int
) -> VisualAsset | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    safe = value[:12000]
    lines = safe.splitlines()[:160] or [safe]
    font = ImageFont.load_default()
    width = min(1800, max(640, max(len(line) for line in lines) * 8 + 40))
    height = min(2400, max(180, len(lines) * 18 + 40))
    image = Image.new("RGB", (width, height), "white")
    ImageDraw.Draw(image).multiline_text((20, 20), "\n".join(lines), fill="black", font=font, spacing=4)
    output = BytesIO()
    image.save(output, format="WEBP", quality=90, method=6)
    content = output.getvalue()
    return VisualAsset(
        asset_id=_asset_id(document, None, ordinal, content),
        asset_type=asset_type,
        page_number=None,
        locator=f"visual:{ordinal}",
        caption=asset_type.replace("_", " "),
        ocr_text=safe[:4000],
        content_hash=hashlib.sha256(content).hexdigest(),
        media_type="image/webp",
        content=content,
    )


def _render_mermaid_asset(
    document: SourceDocument, value: str, ordinal: int
) -> VisualAsset | None:
    """Render a safe flowchart subset without executing Mermaid or browser code."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    edges = _MERMAID_EDGE.findall(value[:12000])
    if not edges:
        return _render_text_asset(document, "mermaid_diagram", value, ordinal)
    labels: dict[str, str] = {}
    ordered: list[str] = []
    for left, left_label, right, right_label in edges[:30]:
        for identity, label in ((left, left_label), (right, right_label)):
            if identity not in labels:
                ordered.append(identity)
            labels[identity] = (label or identity)[:64]
    vertical = bool(re.search(r"(?:flowchart|graph)\s+(?:TD|TB)", value, re.I))
    ordered = ordered[:14] if vertical else ordered[:7]
    box_w, box_h, gap = 240, 72, 70
    width = 360 if vertical else min(2200, 80 + len(ordered) * (box_w + gap))
    height = min(2200, 80 + len(ordered) * (box_h + gap)) if vertical else 240
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    positions: dict[str, tuple[int, int, int, int]] = {}
    for index, identity in enumerate(ordered):
        x = 60 if vertical else 40 + index * (box_w + gap)
        y = 40 + index * (box_h + gap) if vertical else 80
        positions[identity] = (x, y, x + box_w, y + box_h)
        draw.rounded_rectangle(positions[identity], radius=12, fill="#EAF2FF", outline="#2457A6", width=3)
        draw.text((x + 14, y + 27), labels[identity], fill="#102A43", font=font)
    for left, _left_label, right, _right_label in edges[:30]:
        if left not in positions or right not in positions:
            continue
        a, b = positions[left], positions[right]
        start = ((a[0] + a[2]) // 2, a[3]) if vertical else (a[2], (a[1] + a[3]) // 2)
        end = ((b[0] + b[2]) // 2, b[1]) if vertical else (b[0], (b[1] + b[3]) // 2)
        draw.line((start, end), fill="#52606D", width=4)
        if vertical:
            draw.polygon(((end[0], end[1]), (end[0] - 7, end[1] - 12), (end[0] + 7, end[1] - 12)), fill="#52606D")
        else:
            draw.polygon(((end[0], end[1]), (end[0] - 12, end[1] - 7), (end[0] - 12, end[1] + 7)), fill="#52606D")
    output = BytesIO()
    image.save(output, format="WEBP", quality=90, method=6)
    content = output.getvalue()
    return VisualAsset(
        asset_id=_asset_id(document, None, ordinal, content),
        asset_type="mermaid_diagram",
        page_number=None,
        locator=f"visual:{ordinal}",
        caption="Mermaid architecture diagram",
        ocr_text=value[:4000],
        content_hash=hashlib.sha256(content).hexdigest(),
        media_type="image/webp",
        content=content,
    )


def _asset_id(
    document: SourceDocument, page: int | None, ordinal: int, content: bytes
) -> str:
    parts: Iterable[str] = (
        hashlib.sha256(document.source_id.encode()).hexdigest()[:24],
        hashlib.sha256(document.version.encode()).hexdigest()[:24],
        f"{page or 0:04d}",
        f"{ordinal:03d}",
        hashlib.sha256(content).hexdigest()[:24],
    )
    return ".".join(parts)
