"""PDF document tools — create, read, edit."""

from __future__ import annotations

import json
import logging
from qanot.registry import ToolRegistry
from qanot.tools.doc_helpers import resolve_doc_path, resolve_doc_path_existing

logger = logging.getLogger(__name__)


def _parse_page_range(pages_str: str, total: int) -> list[int]:
    """Parse '1-3' or '1,3,5' into zero-based page indices."""
    indices: list[int] = []
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            try:
                s, e = int(start) - 1, int(end) - 1
                indices.extend(range(max(0, s), min(total, e + 1)))
            except ValueError:
                pass
        else:
            try:
                indices.append(int(part) - 1)
            except ValueError:
                pass
    return indices


def register_pdf_tools(registry: ToolRegistry, workspace_dir: str) -> None:
    """Register PDF document tools."""

    # ── create_pdf ──
    async def create_pdf(params: dict) -> str:
        """Create a PDF document."""
        try:
            from fpdf import FPDF
        except ImportError:
            return json.dumps({"error": "fpdf2 kutubxonasi o'rnatilmagan. pip install fpdf2"})

        filename = params.get("filename", "document.pdf")
        if not filename.endswith(".pdf"):
            filename += ".pdf"

        title = params.get("title", "")
        content = params.get("content", "")
        rows = params.get("rows")

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # Use built-in font (supports basic latin)
        pdf.set_font("Helvetica", size=12)

        # Title
        if title:
            pdf.set_font("Helvetica", "B", 18)
            pdf.cell(0, 15, title, new_x="LMARGIN", new_y="NEXT", align="C")
            pdf.ln(5)
            pdf.set_font("Helvetica", size=12)

        # Content — parse line by line
        if content:
            for line in content.split("\n"):
                line = line.strip()
                if not line:
                    pdf.ln(5)
                elif line.startswith("# "):
                    pdf.set_font("Helvetica", "B", 16)
                    pdf.cell(0, 10, line[2:], new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("Helvetica", size=12)
                elif line.startswith("## "):
                    pdf.set_font("Helvetica", "B", 14)
                    pdf.cell(0, 9, line[3:], new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("Helvetica", size=12)
                elif line.startswith("- ") or line.startswith("* "):
                    pdf.cell(10)
                    pdf.cell(0, 7, f"\u2022 {line[2:]}", new_x="LMARGIN", new_y="NEXT")
                else:
                    pdf.multi_cell(0, 7, line)

        # Table
        if rows and isinstance(rows, list) and len(rows) > 0:
            pdf.ln(5)
            col_count = len(rows[0])
            col_width = (pdf.w - 20) / col_count

            # Header row
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_fill_color(37, 99, 235)
            pdf.set_text_color(255, 255, 255)
            for cell in rows[0]:
                pdf.cell(col_width, 8, str(cell), border=1, fill=True, align="C")
            pdf.ln()

            # Data rows
            pdf.set_font("Helvetica", size=10)
            pdf.set_text_color(0, 0, 0)
            for row in rows[1:]:
                for cell in row:
                    pdf.cell(col_width, 7, str(cell), border=1)
                pdf.ln()

        path, error = resolve_doc_path({"file": filename}, workspace_dir)
        if error:
            return error
        pdf.output(str(path))

        return json.dumps({
            "status": "ok",
            "file": str(path),
            "filename": filename,
            "message": f"{filename} yaratildi",
        })

    # ── read_pdf ──
    async def read_pdf(params: dict) -> str:
        """Read content from a PDF file."""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return json.dumps({"error": "PyMuPDF kutubxonasi o'rnatilmagan. pip install PyMuPDF"})

        path, error = resolve_doc_path_existing(params, workspace_dir)
        if error:
            return error

        doc = fitz.open(str(path))
        total_pages = len(doc)

        # Parse page range
        pages = params.get("pages")
        page_indices = list(range(total_pages))
        if pages:
            page_indices = _parse_page_range(pages, total_pages)

        content = []
        for i in page_indices:
            if 0 <= i < total_pages:
                page = doc[i]
                text = page.get_text()
                content.append(f"--- Sahifa {i + 1} ---\n{text}")

        doc.close()

        full_text = "\n\n".join(content)
        if len(full_text) > 50000:
            full_text = full_text[:50000] + "\n\n[Truncated \u2014 juda katta fayl]"

        return json.dumps({
            "content": full_text,
            "total_pages": total_pages,
            "pages_read": len(page_indices),
        }, ensure_ascii=False)

    # ── edit_pdf ──
    async def edit_pdf(params: dict) -> str:
        """Edit PDF — add page with text, insert text, delete page, or merge."""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return json.dumps({"error": "PyMuPDF kutubxonasi o'rnatilmagan"})

        path, error = resolve_doc_path_existing(params, workspace_dir)
        if error:
            return error

        action = params.get("action", "add_page")
        doc = fitz.open(str(path))

        if action == "add_page":
            content = params.get("content", "")
            title = params.get("title", "")
            # Add a new A4 page
            page = doc.new_page(width=595, height=842)
            y = 50
            if title:
                page.insert_text((50, y), title, fontsize=18, fontname="helv")
                y += 30
            for line in content.split("\n"):
                if y > 780:
                    page = doc.new_page(width=595, height=842)
                    y = 50
                page.insert_text((50, y), line.strip(), fontsize=11, fontname="helv")
                y += 16

        elif action == "insert_text":
            page_num = params.get("page", 1) - 1  # 1-based to 0-based
            x = params.get("x", 50)
            y_pos = params.get("y", 50)
            text = params.get("text", "")
            fontsize = params.get("fontsize", 12)
            if 0 <= page_num < len(doc):
                page = doc[page_num]
                page.insert_text((x, y_pos), text, fontsize=fontsize, fontname="helv")
            else:
                doc.close()
                return json.dumps({"error": f"Sahifa {page_num + 1} topilmadi"})

        elif action == "delete_page":
            page_num = params.get("page", 1) - 1
            if 0 <= page_num < len(doc):
                doc.delete_page(page_num)
            else:
                doc.close()
                return json.dumps({"error": f"Sahifa {page_num + 1} topilmadi"})

        elif action == "merge":
            merge_path, merge_error = resolve_doc_path_existing(
                params, workspace_dir, key="merge_file",
            )
            if merge_error:
                doc.close()
                return merge_error
            doc2 = fitz.open(str(merge_path))
            doc.insert_pdf(doc2)
            doc2.close()

        doc.save(str(path), incremental=False, deflate=True)
        doc.close()

        # Re-open to get accurate page count
        doc_check = fitz.open(str(path))
        total_pages = len(doc_check)
        doc_check.close()

        return json.dumps({
            "status": "ok",
            "message": f"{path.name} yangilandi",
            "action": action,
            "total_pages": total_pages,
        })

    # Register tools
    registry.register(
        name="create_pdf",
        description=(
            "Create a PDF document. For contracts, reports, invoices. "
            "Markdown format supported: # heading, ## subheading, - list. "
            "Use rows parameter for tables."
        ),
        parameters={
            "type": "object",
            "required": ["filename"],
            "properties": {
                "filename": {"type": "string", "description": "Fayl nomi (masalan: hisobot.pdf)"},
                "title": {"type": "string", "description": "Hujjat sarlavhasi"},
                "content": {"type": "string", "description": "Hujjat matni (Markdown format)"},
                "rows": {
                    "type": "array",
                    "description": "Jadval ma'lumotlari. Birinchi qator — sarlavha.",
                    "items": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        handler=create_pdf,
    )

    registry.register(
        name="read_pdf",
        description="Read a PDF file. Returns text content of all or selected pages.",
        parameters={
            "type": "object",
            "required": ["file"],
            "properties": {
                "file": {"type": "string", "description": "Fayl nomi yoki to'liq path"},
                "pages": {
                    "type": "string",
                    "description": "Sahifa diapazoni: '1-3' yoki '1,3,5' (ixtiyoriy, standart — hammasi)",
                },
            },
        },
        handler=read_pdf,
    )

    registry.register(
        name="edit_pdf",
        description=(
            "Edit a PDF file. Add page (add_page), insert text (insert_text), "
            "delete page (delete_page), or merge PDFs (merge)."
        ),
        parameters={
            "type": "object",
            "required": ["file"],
            "properties": {
                "file": {"type": "string", "description": "Fayl nomi yoki to'liq path"},
                "action": {
                    "type": "string",
                    "enum": ["add_page", "insert_text", "delete_page", "merge"],
                    "description": "Harakat turi: add_page, insert_text, delete_page, merge",
                },
                "title": {"type": "string", "description": "Sahifa sarlavhasi (add_page uchun)"},
                "content": {"type": "string", "description": "Sahifa matni (add_page uchun)"},
                "page": {
                    "type": "integer",
                    "description": "Sahifa raqami, 1 dan boshlanadi (insert_text/delete_page uchun)",
                },
                "x": {"type": "number", "description": "X koordinata (insert_text uchun, standart: 50)"},
                "y": {"type": "number", "description": "Y koordinata (insert_text uchun, standart: 50)"},
                "text": {"type": "string", "description": "Yoziladigan matn (insert_text uchun)"},
                "fontsize": {"type": "number", "description": "Shrift o'lchami (insert_text uchun, standart: 12)"},
                "merge_file": {"type": "string", "description": "Birlashtirilishi kerak bo'lgan PDF fayl (merge uchun)"},
            },
        },
        handler=edit_pdf,
    )

    logger.info("PDF tools registered: create_pdf, read_pdf, edit_pdf")
