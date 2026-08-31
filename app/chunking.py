from pathlib import Path

_LANGUAGES = {
    ".java": "java",
    ".js": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".csv": "csv",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".py": "python",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}

BINARY_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"}
SAFE_VISUAL_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def is_indexable_path(path: str) -> bool:
    return Path(path).suffix.lower() in _LANGUAGES


def is_visual_asset_path(path: str) -> bool:
    return Path(path).suffix.lower() in SAFE_VISUAL_EXTENSIONS
