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


def test_pdf_read_data_adds_page_markers_and_progress(monkeypatch) -> None:
    async def fake_page_count(_path: str) -> int:
        return 37

    async def fake_extract(_path: str, options):
        assert options == {"firstPage": 3.0, "lastPage": 4.0}
        return {
            "success": True,
            "data": {"file": {"text": "Methods\fResults"}},
        }

    monkeypatch.setattr(pdf, "get_pdf_page_count", fake_page_count)
    monkeypatch.setattr(pdf, "extract_pdf_text", fake_extract)
    result = asyncio.run(file_read_module._read_pdf_data("paper.pdf", "3-4"))
    file = result["file"]

    assert result["type"] == "pdf_text"
    assert "--- PDF page 3 ---\nMethods" in file["text"]
    assert "--- PDF page 4 ---\nResults" in file["text"]
    assert file["pageCount"] == 37
    assert file["coverageComplete"] is False
    assert file["nextPages"] == "5-24"


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


def test_pdf_text_result_marks_page_coverage_and_next_range() -> None:
    block = FileReadTool().map_tool_result_to_tool_result_block_param(
        {
            "type": "pdf_text",
            "file": {
                "filePath": "/workspace/paper.pdf",
                "pages": "1-20",
                "pageCount": 37,
                "firstPage": 1,
                "lastPage": 20,
                "coverageComplete": False,
                "hasMore": True,
                "nextPages": "21-37",
                "textTruncated": False,
                "text": "--- PDF page 1 ---\nIntroduction",
            },
        },
        "tool-1",
    )

    assert "--- PDF page 1 ---" in block["content"]
    assert "partial PDF coverage" in block["content"]
    assert 'pages="21-37"' in block["content"]
    assert "abstract/landing page is not a substitute" in block["content"]


def test_pdf_full_coverage_result_is_explicit() -> None:
    block = FileReadTool().map_tool_result_to_tool_result_block_param(
        {
            "type": "pdf_text",
            "file": {
                "filePath": "/workspace/short-paper.pdf",
                "pages": "1-8",
                "pageCount": 8,
                "coverageComplete": True,
                "hasMore": False,
                "nextPages": None,
                "textTruncated": False,
                "text": "--- PDF page 8 ---\nLimitations",
            },
        },
        "tool-2",
    )

    assert "Full PDF text coverage is complete" in block["content"]
    assert "Limitations" in block["content"]


def test_pdf_page_coverage_is_merged_across_reads() -> None:
    state: dict = {}
    first = {
        "firstPage": 1,
        "lastPage": 7,
        "pageCount": 14,
        "coverageComplete": False,
        "textTruncated": False,
    }
    second = {
        "firstPage": 8,
        "lastPage": 14,
        "pageCount": 14,
        "coverageComplete": False,
        "textTruncated": False,
    }

    file_read_module._update_pdf_coverage(state, "/workspace/paper.pdf", first)
    assert first["cumulativePageRanges"] == ["1-7"]
    assert first["coverageComplete"] is False
    assert first["nextPages"] == "8-14"

    file_read_module._update_pdf_coverage(state, "/workspace/paper.pdf", second)
    assert second["cumulativePageRanges"] == ["1-14"]
    assert second["coverageComplete"] is True
    assert second["nextPages"] is None
