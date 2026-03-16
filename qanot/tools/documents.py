"""Document tools — Word, Excel, PDF, PowerPoint."""

from __future__ import annotations

from qanot.tools.docx import register_docx_tools
from qanot.tools.xlsx import register_xlsx_tools
from qanot.tools.pdf import register_pdf_tools
from qanot.tools.pptx_tools import register_pptx_tools


def register_document_tools(registry, workspace_dir: str) -> None:
    """Register all document tools."""
    register_docx_tools(registry, workspace_dir)
    register_xlsx_tools(registry, workspace_dir)
    register_pdf_tools(registry, workspace_dir)
    register_pptx_tools(registry, workspace_dir)
