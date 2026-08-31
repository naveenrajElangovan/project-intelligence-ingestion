from pathlib import Path
import runpy


def test_lab_1_uses_production_chunker() -> None:
    module = runpy.run_path(str(Path(__file__).parents[1] / "labs/lab01_langchain_chunking.py"))
    result = module["run"]()
    assert result
    assert all(item["embedding_text"].startswith("passage:") for item in result)


def test_lab_2_builds_transient_binary_document(tmp_path: Path) -> None:
    module = runpy.run_path(str(Path(__file__).parents[1] / "labs/lab02_docling.py"))
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.7\n")
    document = module["build_document"](sample)
    assert document.content == ""
    assert document.content_bytes == b"%PDF-1.7\n"
    assert document.mime_type == "application/pdf"
