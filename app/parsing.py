from io import BytesIO

from bs4 import BeautifulSoup
from pypdf import PdfReader


def html_to_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def pdf_to_text(value: bytes) -> str:
    reader = PdfReader(BytesIO(value))
    return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()


def attachment_to_text(value: bytes, media_type: str, filename: str) -> str | None:
    normalized = media_type.split(";", 1)[0].strip().lower()
    if normalized == "application/pdf" or filename.lower().endswith(".pdf"):
        return pdf_to_text(value)
    if normalized.startswith("text/") or filename.lower().endswith(
        (".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml")
    ):
        return value.decode("utf-8", errors="replace")
    return None
