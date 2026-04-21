"""Pre-turn image extraction pipeline.

When a Telegram message arrives with image attachments, we run a dedicated
Haiku call BEFORE the main agent turn to pull structured fields out of
each image. The extraction is:

  1. **Persisted** to ``workspace/memory/extractions/<ts>_<hash>.md`` —
     survives compaction, context resets, and cross-session retrieval
     via the RAG indexer (which auto-ingests files under memory/).
  2. **Injected** into the main turn's user message as a text block
     alongside the image itself. The main model sees both the raw image
     and the structured extraction; redundancy is resilience.

Why a separate LLM call instead of trusting the main turn to extract:
  - **Deterministic**: the pipeline enforces extraction; it doesn't
    depend on the main turn's prompt eliciting it. Prompt drift no
    longer causes hallucinated-schema failures.
  - **Schema-enforced**: JSON output with a defined shape that we
    validate before trusting.
  - **Cheap**: Haiku 4.5 is $0.25/1M input, $1.25/1M output. A 1200px
    image is ~1500 vision tokens, plus ~500 tokens of extraction JSON
    — roughly $0.001 per image.
  - **Fail-soft**: if Haiku times out, returns bad JSON, or refuses,
    we log and continue with the main turn unchanged. Extraction is an
    augmentation, never a gate.

v1 scope: images only. Future: PDFs uploaded as documents, voice
transcripts, video keyframes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Schema the extractor Haiku must return. Kept flat and generic; specific
# doc types put their fields under `fields` rather than having a per-type
# schema (avoids tight coupling between extractor and downstream consumers).
EXTRACTION_SYSTEM_PROMPT = """You extract structured data from images of documents or photos.

Return ONLY a single JSON object (no prose before/after, no markdown fences) matching this schema exactly:

{
  "doc_type": one of ["receipt", "invoice", "business_card", "contract", "menu", "handwritten", "id_document", "product_catalog", "order_form", "other"],
  "title": "short human-readable description (e.g. 'Korzinka chek 2026-04-21', 'Business card — Akmal Karimov')",
  "fields": {
    "<field_name>": "<value as string>",
    ...
  },
  "entities": {
    "people": ["Full Name", ...],
    "organizations": ["Company Name", ...],
    "dates": ["YYYY-MM-DD", ...],
    "amounts": [{"value": <number>, "currency": "UZS"|"USD"|"EUR"|other}, ...],
    "phones": ["+998XXXXXXXXX", ...],
    "emails": ["...@..."],
    "addresses": ["..."]
  },
  "raw_text": "best-effort transcription of all readable text, preserving original language (Uzbek/Russian/English)",
  "confidence": 0.0 to 1.0,
  "warnings": ["human-readable warnings such as 'bottom half blurry', 'handwriting unclear'"]
}

Absolute rules:
- NEVER invent data. If a field isn't visible, omit it from `fields` or mark value as "?".
- Preserve numeric precision in `entities.amounts` — amounts are NUMBERS not strings.
- Normalize phones to +998XXXXXXXXX when Uzbek.
- If the image isn't a document at all (selfie, landscape, food photo), set doc_type="other" and confidence low (<0.3).
- Return ONLY the JSON. No preamble. No code fences. No commentary."""


@dataclass
class ExtractionResult:
    """Structured output of a single-image extraction."""

    doc_type: str = "other"
    title: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    entities: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)

    # Pipeline metadata (not from Haiku)
    image_hash: str = ""
    image_media_type: str = ""
    created_at: str = ""
    source_path: str = ""  # relative path where extraction was persisted
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_context_markdown(self) -> str:
        """Short markdown block injected into the main turn's user message."""
        lines = [
            "[Rasmdan ajratib olingan ma'lumotlar (extraction pipeline):]",
            f"- doc_type: {self.doc_type} (confidence: {self.confidence:.2f})",
        ]
        if self.title:
            lines.append(f"- title: {self.title}")
        if self.fields:
            lines.append("- fields:")
            for k, v in self.fields.items():
                lines.append(f"    {k}: {v}")
        ent = self.entities or {}
        for key in ("people", "organizations", "dates", "phones", "emails", "addresses"):
            vals = ent.get(key) or []
            if vals:
                lines.append(f"- {key}: {', '.join(str(v) for v in vals)}")
        amounts = ent.get("amounts") or []
        if amounts:
            fmt = []
            for a in amounts:
                if isinstance(a, dict):
                    fmt.append(f"{a.get('value', '?')} {a.get('currency', '?')}")
                else:
                    fmt.append(str(a))
            lines.append(f"- amounts: {', '.join(fmt)}")
        if self.warnings:
            lines.append(f"- warnings: {'; '.join(self.warnings)}")
        return "\n".join(lines)

    def to_memory_markdown(self) -> str:
        """Full markdown written to workspace/memory/extractions/ for durability."""
        ts = self.created_at or datetime.now(timezone.utc).isoformat()
        lines = [
            f"# Rasm extraction — {ts}",
            "",
            f"**Doc type:** {self.doc_type}  ",
            f"**Confidence:** {self.confidence:.2f}  ",
            f"**Title:** {self.title or '(untitled)'}  ",
            f"**Image hash:** {self.image_hash}  ",
            f"**Media type:** {self.image_media_type}  ",
            "",
        ]
        if self.fields:
            lines.append("## Fields")
            for k, v in self.fields.items():
                lines.append(f"- **{k}:** {v}")
            lines.append("")
        if self.entities:
            lines.append("## Entities")
            for key, vals in self.entities.items():
                if not vals:
                    continue
                if key == "amounts":
                    lines.append(f"- **{key}:**")
                    for a in vals:
                        if isinstance(a, dict):
                            lines.append(
                                f"    - {a.get('value', '?')} {a.get('currency', '?')}"
                            )
                        else:
                            lines.append(f"    - {a}")
                else:
                    lines.append(f"- **{key}:** {', '.join(str(v) for v in vals)}")
            lines.append("")
        if self.raw_text:
            lines.append("## Raw text")
            lines.append("")
            lines.append("```")
            lines.append(self.raw_text.strip())
            lines.append("```")
            lines.append("")
        if self.warnings:
            lines.append("## Warnings")
            for w in self.warnings:
                lines.append(f"- {w}")
            lines.append("")
        return "\n".join(lines)


def _hash_image_block(image_block: dict) -> str:
    """SHA256 of the raw image bytes — used for dedup + filename."""
    src = image_block.get("source") or {}
    data = src.get("data") or ""
    if not data:
        return ""
    # data is base64; hash the base64 string itself for stability
    return hashlib.sha256(data.encode("ascii", errors="ignore")).hexdigest()[:16]


def _strip_code_fences(text: str) -> str:
    """Haiku sometimes wraps JSON in ```json ... ``` despite the system
    prompt forbidding it. Strip either kind of fence."""
    s = text.strip()
    if s.startswith("```"):
        # ```json\n...\n```  or  ```\n...\n```
        m = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", s, re.DOTALL)
        if m:
            return m.group(1).strip()
    return s


def _parse_json_safe(text: str) -> tuple[dict, str | None]:
    """Parse Haiku's text response into a dict. Returns (parsed, error_message)."""
    cleaned = _strip_code_fences(text)
    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            return {}, f"JSON root is not an object: {type(parsed).__name__}"
        return parsed, None
    except json.JSONDecodeError as e:
        return {}, f"Malformed JSON: {e.msg} @ pos {e.pos}"


def _coerce_result(raw: dict, image_hash: str, media_type: str) -> ExtractionResult:
    """Validate and clean Haiku's parsed JSON into an ExtractionResult.

    We are permissive: missing fields get defaults, wrong types become
    empty collections. A partial extraction is still useful context for
    the main turn.
    """
    res = ExtractionResult(
        doc_type=str(raw.get("doc_type") or "other"),
        title=str(raw.get("title") or ""),
        fields=raw["fields"] if isinstance(raw.get("fields"), dict) else {},
        entities=raw["entities"] if isinstance(raw.get("entities"), dict) else {},
        raw_text=str(raw.get("raw_text") or ""),
        confidence=float(raw["confidence"]) if isinstance(raw.get("confidence"), (int, float)) else 0.0,
        warnings=[str(w) for w in (raw.get("warnings") or []) if w],
        image_hash=image_hash,
        image_media_type=media_type,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    # Clamp confidence
    if res.confidence < 0:
        res.confidence = 0.0
    elif res.confidence > 1:
        res.confidence = 1.0
    return res


class ImageExtractor:
    """Haiku-backed single-image extractor.

    Thin wrapper around an ``anthropic.AsyncAnthropic`` client. Extraction
    is one-shot (no conversation, no tool use) so we don't reuse the full
    AnthropicProvider machinery.
    """

    def __init__(
        self,
        client: Any,
        model: str = "claude-haiku-4-5-20251001",
        *,
        timeout_seconds: float = 20.0,
        max_tokens: int = 2048,
    ) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens

    async def extract(self, image_block: dict) -> ExtractionResult:
        """Extract structured fields from a single Anthropic image content block.

        ``image_block`` is the dict produced by telegram.media.download_photo:
        ``{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}}``.

        On any failure, returns an ExtractionResult with error set. Callers
        should check ``.ok`` before trusting fields — but error results are
        still safe to stringify (empty fields, doc_type="other").
        """
        img_hash = _hash_image_block(image_block)
        media_type = (image_block.get("source") or {}).get("media_type", "image/jpeg")

        try:
            resp = await asyncio.wait_for(
                self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=EXTRACTION_SYSTEM_PROMPT,
                    messages=[
                        {"role": "user", "content": [image_block]},
                    ],
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Image extraction timed out after %.1fs (hash=%s)",
                self._timeout, img_hash,
            )
            return ExtractionResult(
                image_hash=img_hash,
                image_media_type=media_type,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                error="extraction timed out",
            )
        except Exception as e:
            logger.warning(
                "Image extraction call failed (hash=%s): %s", img_hash, e,
            )
            return ExtractionResult(
                image_hash=img_hash,
                image_media_type=media_type,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                error=f"extraction call failed: {e}",
            )

        # Pull text from the first text block in the response.
        text = ""
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "") or ""
                break

        if not text.strip():
            logger.warning("Extraction returned empty response (hash=%s)", img_hash)
            return ExtractionResult(
                image_hash=img_hash,
                image_media_type=media_type,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                error="empty response",
            )

        parsed, parse_error = _parse_json_safe(text)
        if parse_error:
            logger.warning(
                "Extraction JSON parse failed (hash=%s): %s. Raw: %s",
                img_hash, parse_error, text[:300],
            )
            # Fall back to raw-text-only result so the main turn still gets *something*
            return ExtractionResult(
                image_hash=img_hash,
                image_media_type=media_type,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                raw_text=text[:4000],
                doc_type="other",
                confidence=0.0,
                warnings=[f"structured extraction failed: {parse_error}"],
                error=parse_error,
            )

        return _coerce_result(parsed, img_hash, media_type)


# ── Persistence ──────────────────────────────────────────────────


def persist_extraction(
    result: ExtractionResult,
    workspace_dir: str | Path,
) -> str | None:
    """Write the extraction as markdown under workspace/memory/extractions/.

    Returns the RELATIVE path (from workspace_dir) of the written file, or
    None if the write failed. Relative path lets callers reference it in
    prompts without leaking container filesystem layout.
    """
    ws = Path(workspace_dir)
    dest_dir = ws / "memory" / "extractions"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Couldn't create extractions dir: %s", e)
        return None

    # Filename: 2026-04-21T14-40-00_<hash>.md
    # Colon-free for cross-FS safety; hash dedupes if same image is re-sent.
    ts = (result.created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    ts_safe = ts.replace(":", "-").replace("+00-00", "Z").replace("+00:00", "Z")
    fname = f"{ts_safe}_{result.image_hash or 'unknown'}.md"
    fpath = dest_dir / fname

    # Dedup: if a file with this hash already exists for today, skip write
    # (avoids rewriting the same extraction on every resend of the same photo).
    if result.image_hash:
        for existing in dest_dir.glob(f"*_{result.image_hash}.md"):
            rel = str(existing.relative_to(ws))
            result.source_path = rel
            logger.debug("Extraction already persisted at %s, skipping write", rel)
            return rel

    try:
        fpath.write_text(result.to_memory_markdown(), encoding="utf-8")
    except OSError as e:
        logger.warning("Extraction file write failed: %s", e)
        return None

    rel = str(fpath.relative_to(ws))
    result.source_path = rel
    return rel


# ── Batch orchestrator ───────────────────────────────────────────


async def extract_images(
    extractor: ImageExtractor,
    images: list[dict],
    workspace_dir: str | Path,
    *,
    max_concurrent: int = 3,
) -> list[ExtractionResult]:
    """Run extractions for N images in parallel (bounded) + persist each.

    Callers pass the raw ``images`` list from the Telegram adapter; this
    function returns ExtractionResult objects in the same order, with
    ``source_path`` populated for successful persists.
    """
    if not images:
        return []

    semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))

    async def _run_one(img: dict) -> ExtractionResult:
        async with semaphore:
            result = await extractor.extract(img)
        # Persist even failed extractions when they carry raw_text — the
        # main turn can still reference them. Purely-errored results with
        # no raw_text are skipped to avoid noise.
        if result.raw_text or result.fields or result.entities:
            persist_extraction(result, workspace_dir)
        return result

    return await asyncio.gather(*[_run_one(img) for img in images])
