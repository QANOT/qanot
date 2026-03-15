"""Text chunking and BM25 keyword search for RAG.

Zero external dependencies — uses character-based approximation for token
counts (1 token ~ 4 chars) and a pure-Python BM25 implementation.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

logger = logging.getLogger(__name__)


def chunk_text(
    text: str,
    max_tokens: int = 512,
    overlap: int = 64,
    separator: str | None = None,
) -> list[str]:
    """Split text into overlapping chunks.

    Uses paragraph boundaries when possible, falls back to sentence
    splitting, then word-level splitting for very long passages.

    Args:
        text: Input text to chunk.
        max_tokens: Approximate max tokens per chunk (1 token ~ 4 chars).
        overlap: Overlap in approximate tokens between chunks.
        separator: Optional custom separator regex pattern.

    Returns:
        List of text chunks.
    """
    if not text or not text.strip():
        return []

    max_chars = max_tokens * 4
    overlap_chars = overlap * 4

    # Split into paragraphs first
    if separator:
        segments = re.split(separator, text)
    else:
        segments = re.split(r"\n{2,}", text)

    # Remove empty segments
    segments = [stripped for s in segments if (stripped := s.strip())]

    # If a single segment is too long, split by sentences
    expanded: list[str] = []
    for seg in segments:
        if len(seg) <= max_chars:
            expanded.append(seg)
        else:
            sentences = re.split(r"(?<=[.!?])\s+", seg)
            expanded.extend(s.strip() for s in sentences if s.strip())

    # Merge small segments into chunks with overlap
    chunks: list[str] = []
    current = ""

    for segment in expanded:
        candidate = f"{current}\n\n{segment}".strip() if current else segment

        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
                # Keep overlap from the end of the current chunk
                if overlap_chars > 0 and len(current) > overlap_chars:
                    current = current[-overlap_chars:].lstrip() + "\n\n" + segment
                    if len(current) > max_chars:
                        current = segment
                else:
                    current = segment
            else:
                # Single segment exceeds max — split by words
                words = segment.split()
                current = ""
                for word in words:
                    test = f"{current} {word}".strip()
                    if len(test) <= max_chars:
                        current = test
                    else:
                        if current:
                            chunks.append(current)
                        current = word

    if current:
        chunks.append(current)

    return chunks


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer for BM25."""
    return re.findall(r"\w+", text.lower())


class BM25Index:
    """In-memory BM25 index for keyword-based retrieval.

    Pure-Python implementation (no external dependencies). Useful as a
    complement to vector search for hybrid retrieval.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: list[str] = []
        self._doc_ids: list[str] = []
        self._doc_freqs: list[Counter] = []
        self._doc_lens: list[int] = []
        self._avg_dl: float = 0.0
        self._idf: dict[str, float] = {}

    def add(self, doc_ids: list[str], texts: list[str]) -> None:
        """Add documents to the index and recompute statistics."""
        for doc_id, text in zip(doc_ids, texts):
            tokens = _tokenize(text)
            self._docs.append(text)
            self._doc_ids.append(doc_id)
            self._doc_freqs.append(Counter(tokens))
            self._doc_lens.append(len(tokens))

        self._recompute_stats()

    def _recompute_stats(self) -> None:
        """Recompute IDF and average document length."""
        n = len(self._docs)
        if n == 0:
            return

        self._avg_dl = sum(self._doc_lens) / n

        # Document frequency for each term
        df: Counter = Counter()
        for freq in self._doc_freqs:
            for term in freq:
                df[term] += 1

        self._idf = {
            term: math.log((n - count + 0.5) / (count + 0.5) + 1.0)
            for term, count in df.items()
        }

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Search the index.

        Returns:
            List of (doc_id, score) tuples sorted by score descending.
        """
        if not self._docs:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores: list[tuple[str, float]] = []

        for doc_id, freq, dl in zip(self._doc_ids, self._doc_freqs, self._doc_lens):
            score = 0.0

            for token in query_tokens:
                if token not in freq:
                    continue
                tf = freq[token]
                idf = self._idf.get(token, 0.0)
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * dl / self._avg_dl)
                score += idf * numerator / denominator

            if score > 0:
                scores.append((doc_id, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def clear(self) -> None:
        """Clear the entire index."""
        self._docs.clear()
        self._doc_ids.clear()
        self._doc_freqs.clear()
        self._doc_lens.clear()
        self._avg_dl = 0.0
        self._idf.clear()
