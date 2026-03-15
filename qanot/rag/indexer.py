"""Memory indexer that bridges Qanot's memory system with RAG."""

from __future__ import annotations

import logging
from pathlib import Path

from qanot.rag.engine import RAGEngine

logger = logging.getLogger(__name__)


class MemoryIndexer:
    """Indexes workspace memory files into the RAG engine.

    Watches MEMORY.md, SESSION-STATE.md, and daily notes,
    re-indexing them when content changes.
    """

    _MAX_DAILY_NOTES = 30

    def __init__(self, engine: RAGEngine, workspace_dir: str = "/data/workspace"):
        self.engine = engine
        self.workspace_dir = Path(workspace_dir)
        self._indexed_hashes: dict[str, int] = {}

    async def index_workspace(self, user_id: str = "") -> int:
        """Index all memory files in the workspace.

        Skips files whose content hash hasn't changed since last indexing.

        Args:
            user_id: Owner of this workspace.

        Returns:
            Number of new chunks indexed.
        """
        total_chunks = 0

        # Index shared root files (all users' facts and session state)
        for filename in ("MEMORY.md", "SESSION-STATE.md"):
            root_path = self.workspace_dir / filename
            if root_path.exists():
                total_chunks += await self._index_file(root_path, user_id)

        # Index shared daily notes (workspace root, OpenClaw-style)
        if (memory_dir := self.workspace_dir / "memory").exists():
            for note_path in sorted(memory_dir.glob("*.md"), reverse=True)[:self._MAX_DAILY_NOTES]:
                total_chunks += await self._index_file(note_path, user_id)

        if total_chunks > 0:
            logger.info("Indexed %d chunks from workspace for user=%r", total_chunks, user_id)

        return total_chunks

    async def index_text(
        self,
        text: str,
        *,
        source: str,
        user_id: str = "",
        metadata: dict | None = None,
    ) -> list[str]:
        """Index arbitrary text into RAG.

        Args:
            text: Content to index.
            source: Source identifier.
            user_id: Owner.
            metadata: Extra metadata.

        Returns:
            List of chunk IDs.
        """
        return await self.engine.ingest(
            text,
            source=source,
            user_id=user_id,
            metadata=metadata,
        )

    _MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

    async def _index_file(self, path: Path, user_id: str) -> int:
        """Index a single file if its content has changed."""
        try:
            if path.stat().st_size > self._MAX_FILE_BYTES:
                logger.warning("Skipping %s: exceeds %d-byte limit", path, self._MAX_FILE_BYTES)
                return 0
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            return 0

        content_hash = hash(content)
        source = str(path.relative_to(self.workspace_dir))
        old_hash = self._indexed_hashes.get(source)

        if old_hash == content_hash:
            return 0

        if old_hash is not None:
            self.engine.store.delete_source(source)
        chunk_ids = await self.engine.ingest(
            content,
            source=source,
            user_id=user_id,
            metadata={"file": path.name},
        )
        self._indexed_hashes[source] = content_hash
        return len(chunk_ids)

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        user_id: str | None = None,
    ) -> list[dict]:
        """Search indexed memory. Returns dicts matching memory.py format.

        Args:
            query: Search query.
            top_k: Max results.
            user_id: Filter by user.

        Returns:
            List of result dicts with file, content, score, and chunk_id keys.
        """
        return [
            {
                "file": r.metadata.get("source", ""),
                "content": r.text,
                "score": r.score,
                "chunk_id": r.chunk_id,
            }
            for r in (await self.engine.query(query, top_k=top_k, user_id=user_id)).results
        ]
