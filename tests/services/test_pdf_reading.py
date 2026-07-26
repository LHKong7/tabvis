from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import tabvis.agent.tools.file_read_tool as file_read_module
from tabvis.agent.tools.file_read_tool import FileReadInput, FileReadTool
from tabvis.tool import ToolResult, ToolUseContext
from tabvis.utils import pdf


def test_pdftotext_receives_the_requested_page_range(monkeypatch) -> None:
    captured: list[str] = []

    async def fake_exec(file: str, args: list[str], options):
        assert file == "pdftotext"
        captured.extend(args)
        return {"code": 0, "stdout": "selected text", "stderr": ""}

    monkeypatch.setattr(pdf, "exec_file_no_throw", fake_exec)
    monkeypatch.setattr(pdf.shutil, "which", lambda name: f"/usr/bin/{name}")
    result = asyncio.run(
        pdf.extract_pdf_text("report.pdf", {"firstPage": 3, "lastPage": 7})
    )
    assert result["success"] is True
    assert captured == ["-layout", "-f", "3", "-l", "7", "report.pdf", "-"]
    assert result["data"]["file"]["text"] == "selected text"


def test_pdf_text_falls_back_to_declared_pypdf_dependency(monkeypatch) -> None:
    class Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class Reader:
        is_encrypted = False

        def __init__(self, path: str) -> None:
            assert path == "report.pdf"
            self.pages = [Page("one"), Page("two"), Page("three")]

    monkeypatch.setattr(pdf.shutil, "which", lambda _name: None)
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    result = asyncio.run(
        pdf.extract_pdf_text("report.pdf", {"firstPage": 2, "lastPage": 3})
    )
    assert result["success"] is True
    assert result["data"]["file"]["text"] == "two\n\f\nthree"
    assert asyncio.run(pdf.get_pdf_page_count("report.pdf")) == 3


def test_read_sniffs_pdf_magic_when_dynamic_url_file_has_no_pdf_suffix(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "get_pdf.cfm"
    path.write_bytes(b"%PDF-1.7\nfake")

    async def fake_pdf_reader(file_path: str, pages: str | None):
        assert file_path == str(path)
        assert pages == "1-2"
        return {"type": "pdf_text", "file": {"filePath": file_path, "text": "ok"}}

    monkeypatch.setattr(file_read_module, "_read_pdf_data", fake_pdf_reader)
    result: ToolResult = asyncio.run(
        FileReadTool().call(
            FileReadInput(file_path=str(path), pages="1-2"),
            ToolUseContext(),
        )
    )
    assert result.data["type"] == "pdf_text"
