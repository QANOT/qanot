"""RAG tool definitions — agent-facing tools for document indexing and retrieval."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from qanot.agent import ToolRegistry

if TYPE_CHECKING:
    from qanot.rag.engine import RAGEngine

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".csv", ".pdf"}


def _extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF file using PyMuPDF."""
    import fitz  # PyMuPDF

    with fitz.open(str(path)) as doc:
        pages = [text for page in doc if (text := page.get_text()).strip()]
    return "\n\n".join(pages)


def register_rag_tools(
    registry: ToolRegistry,
    engine: "RAGEngine",
    workspace_dir: str,
    get_user_id: Callable[[], str],
) -> None:
    """Register RAG document management tools."""

    ws = Path(workspace_dir)
    ws_resolved = ws.resolve()

    def _resolve_path(path: str) -> Path:
        """Resolve a path relative to workspace_dir, preventing traversal."""
        p = Path(path)
        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (ws / p).resolve()
        try:
            resolved.relative_to(ws_resolved)
        except ValueError:
            raise ValueError(
                f"Path '{path}' resolves outside workspace directory"
            )
        return resolved

    # ── rag_index ──
    async def rag_index(params: dict) -> str:
        path_str = params.get("path", "").strip()
        name = params.get("name", "").strip()

        if not path_str:
            return json.dumps({"error": "path is required"})

        try:
            full_path = _resolve_path(path_str)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        if not full_path.exists():
            return json.dumps({"error": f"File not found: {path_str}"})

        if not full_path.is_file():
            return json.dumps({"error": f"Not a file: {path_str}"})

        suffix = full_path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            return json.dumps({
                "error": f"Unsupported file type: {suffix}. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            })

        try:
            if suffix == ".pdf":
                content = _extract_pdf_text(full_path)
            else:
                content = full_path.read_text(encoding="utf-8")
        except ImportError:
            return json.dumps({
                "error": "PDF support requires PyMuPDF: pip install PyMuPDF"
            })
        except UnicodeDecodeError:
            return json.dumps({"error": f"Cannot read file as text: {path_str}"})
        except OSError as e:
            return json.dumps({"error": f"Failed to read file: {e}"})

        if not content.strip():
            return json.dumps({"error": "File is empty"})

        source = name or full_path.name
        user_id = get_user_id()

        # Delete existing chunks for this source before re-indexing
        engine.store.delete_source(source)

        chunk_ids = await engine.ingest(
            content,
            source=source,
            user_id=user_id,
            metadata={"file": full_path.name, "path": str(full_path)},
        )

        logger.info("RAG indexed %s (%d chunks)", source, len(chunk_ids))
        return json.dumps({
            "indexed": True,
            "source": source,
            "chunks": len(chunk_ids),
        })

    registry.register(
        name="rag_index",
        description="Faylni RAG indeksiga qo'shish. Workspace ichidagi .txt, .md, .csv, .pdf fayllarni qo'llab-quvvatlaydi.",
        parameters={
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Fayl yo'li (workspace ichida yoki absolyut)"},
                "name": {"type": "string", "description": "Ko'rsatiladigan nom (default: fayl nomi)"},
            },
        },
        handler=rag_index,
        category="rag",
    )

    # ── rag_search ──
    async def rag_search(params: dict) -> str:
        query = params.get("query", "").strip()
        top_k = params.get("top_k", 5)

        if not query:
            return json.dumps({"error": "query is required"})

        if len(query) > 10000:
            return json.dumps({"error": "query too long (max 10000 characters)"})

        if not isinstance(top_k, int) or top_k < 1:
            top_k = 5
        elif top_k > 100:
            top_k = 100

        user_id = get_user_id()

        rag_result = await engine.query(
            query,
            top_k=top_k,
            user_id=user_id or None,
        )

        results = [
            {
                "text": r.text,
                "source": r.metadata.get("source", ""),
                "score": round(r.score, 4),
            }
            for r in rag_result.results
        ]

        if not results:
            return json.dumps({"message": "Hech narsa topilmadi", "query": query})

        return json.dumps(results, ensure_ascii=False, indent=2)

    registry.register(
        name="rag_search",
        description="Indekslangan hujjatlardan qidirish (semantik + kalit so'z).",
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Qidiruv so'rovi"},
                "top_k": {"type": "integer", "description": "Natijalar soni (default: 5)"},
            },
        },
        handler=rag_search,
        category="rag",
    )

    # ── rag_list ──
    async def rag_list(params: dict) -> str:
        sources = engine.list_sources()
        if not sources:
            return json.dumps({"message": "Hech qanday hujjat indekslanmagan"})
        return json.dumps(sources, ensure_ascii=False, indent=2)

    registry.register(
        name="rag_list",
        description="Indekslangan hujjatlar ro'yxati.",
        parameters={"type": "object", "properties": {}},
        handler=rag_list,
        category="rag",
    )

    # ── rag_forget ──
    async def rag_forget(params: dict) -> str:
        source = params.get("source", "").strip()
        if not source:
            return json.dumps({"error": "source is required"})

        chunks_removed = await engine.delete_source(source)

        if chunks_removed == 0:
            return json.dumps({"error": f"Source '{source}' not found"})

        logger.info("RAG deleted source=%s (%d chunks)", source, chunks_removed)
        return json.dumps({
            "deleted": True,
            "source": source,
            "chunks_removed": chunks_removed,
        })

    registry.register(
        name="rag_forget",
        description="Hujjatni RAG indeksidan o'chirish.",
        parameters={
            "type": "object",
            "required": ["source"],
            "properties": {
                "source": {"type": "string", "description": "O'chiriladigan hujjat nomi (source)"},
            },
        },
        handler=rag_forget,
        category="rag",
    )
