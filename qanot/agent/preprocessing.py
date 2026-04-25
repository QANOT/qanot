"""Preprocessing — system prompt, image extraction, message repair, turn prep.

Mixin that holds the per-turn preprocessing pipeline:
  * system prompt building (incl. skill index injection)
  * pre-turn image extraction (Haiku-backed)
  * WAL/RAG/link injection on the user message
  * compaction/snip + thinking-block strip + message repair on each iteration
  * lifecycle hook firing (`on_pre_turn`)
"""

from __future__ import annotations

import asyncio
import logging

from qanot.flush import memory_flush, summarize_for_compaction
from qanot.memory import wal_scan, wal_write
from qanot.messages import strip_thinking_blocks, repair_messages
from qanot.prompt import build_system_prompt

logger = logging.getLogger(__name__)


class _PreprocessingMixin:
    """Pre-turn preprocessing: prompt build, image extraction, repair, hooks."""

    def _build_system_prompt(self, active_skills_content: str = "", *, turn_prompt_override: str | None = None) -> str:
        """Build the system prompt from workspace files."""
        if turn_prompt_override:
            return turn_prompt_override
        if self._system_prompt_override:
            return self._system_prompt_override

        from qanot.skills import build_skill_index
        skill_index = build_skill_index(self._skills) if self._skills else ""

        return build_system_prompt(
            workspace_dir=self.config.workspace_dir,
            owner_name=self.config.owner_name,
            bot_name=self.config.bot_name,
            timezone_str=self.config.timezone,
            context_percent=self.context.get_context_percent(),
            total_tokens=self.context.total_tokens,
            mode=self.prompt_mode,
            user_id=str(self._current_user_id) if self._current_user_id else "",
            skill_index=skill_index,
            active_skills_content=active_skills_content,
            inject_legacy_memory=self.config.inject_legacy_memory,
        )

    async def _run_image_extractions(self, images: list[dict]) -> str:
        """Run the pre-turn extraction pipeline for N image blocks.

        Returns a markdown context string to inject into the main turn's
        user message. Empty string when extraction is disabled, nothing
        was extractable, or the provider client isn't available.

        Extraction failures (timeout, bad JSON, network) are logged but
        never raise — extraction is pure augmentation, so the main turn
        must still fire with the raw images even if this helper fails.
        """
        try:
            from qanot.extraction import ImageExtractor, extract_images
        except Exception as e:
            logger.warning("extraction module unavailable: %s", e)
            return ""

        client = getattr(self.provider, "client", None)
        if client is None:
            logger.debug("provider has no .client attr; skipping pre-extract")
            return ""

        if self._image_extractor is None:
            self._image_extractor = ImageExtractor(
                client,
                model=getattr(self.config, "pre_extract_model", "claude-haiku-4-5-20251001"),
                timeout_seconds=float(getattr(self.config, "pre_extract_timeout", 20.0)),
            )

        try:
            results = await extract_images(
                self._image_extractor,
                images,
                self.config.workspace_dir,
            )
        except Exception as e:
            logger.warning("pre-turn image extraction failed: %s", e)
            return ""

        blocks: list[str] = []
        for i, r in enumerate(results, 1):
            prefix = f"Rasm {i}/{len(results)}" if len(results) > 1 else "Rasm"
            if r.ok or r.raw_text or r.fields:
                body = r.to_context_markdown()
                if r.source_path:
                    body += f"\n- saved: {r.source_path}"
                blocks.append(f"[{prefix}]\n{body}")
            else:
                # Don't inject empty errors — just note the failure so the
                # main turn knows extraction was attempted.
                blocks.append(
                    f"[{prefix}] extraction unavailable"
                    f"{' — ' + (r.error or '') if r.error else ''}"
                )

        if not blocks:
            return ""
        return "\n\n".join(blocks)

    async def _prepare_turn(self, user_message: str, messages: list[dict], *, images: list[dict] | None = None) -> str:
        """Shared turn setup: WAL scan, RAG context, compaction recovery, add user message.

        Returns the (possibly modified) user_message.
        """
        # Unicode sanitization: strip invisible/dangerous chars before processing
        from qanot.utils import sanitize_unicode
        user_message = sanitize_unicode(user_message)

        # WAL Protocol: scan user message BEFORE generating response.
        # Skip for isolated/cron agents (no user_id) — their prompts are internal
        # instructions, not user utterances, and would produce junk SESSION-STATE.md
        # entries (e.g. the daily briefing prompt matching "decision" patterns).
        if self._current_user_id:
            wal_entries = wal_scan(user_message)
            if wal_entries:
                wal_write(wal_entries, self.config.workspace_dir, user_id=str(self._current_user_id))
                logger.debug("WAL: wrote %d entries before responding", len(wal_entries))

        # RAG context injection: auto-inject relevant memory for dumb models
        # "auto"/"always" = inject, "agentic" = skip (model uses rag_search tool)
        if (
            self._rag_indexer is not None
            and self.config.rag_mode in ("auto", "always")
            and len(user_message.strip()) > 10  # skip trivial messages like "hi"
        ):
            try:
                hints = await self._rag_indexer.search(
                    user_message, top_k=3, user_id=self._current_user_id or None,
                )
                if hints:
                    hint_text = "\n".join(
                        f"- [{h['file']}] {h['content'][:200]}" for h in hints[:3]
                    )
                    cap = self.config.max_memory_injection_chars
                    if len(hint_text) > cap:
                        hint_text = hint_text[:cap] + "\n[... truncated]"
                    user_message = (
                        f"{user_message}\n\n---\n"
                        f"[MEMORY CONTEXT — relevant past information]\n{hint_text}"
                    )
                    logger.debug("RAG: injected %d memory hints", len(hints))
            except Exception as e:
                logger.warning("RAG context injection failed: %s", e)

        # Link understanding: auto-fetch URLs in user messages (non-blocking, 3s max)
        if len(user_message.strip()) > 10:
            try:
                from qanot.links import fetch_link_previews

                link_context = await asyncio.wait_for(
                    fetch_link_previews(user_message), timeout=3.0,
                )
                if link_context:
                    cap = self.config.max_memory_injection_chars
                    if len(link_context) > cap:
                        link_context = link_context[:cap] + "\n[... truncated]"
                    user_message = f"{user_message}\n\n---\n{link_context}"
            except asyncio.TimeoutError:
                logger.debug("Link preview skipped (>3s timeout)")
            except Exception as e:
                logger.debug("Link preview injection failed: %s", e)

        # Check for compaction recovery
        if self.context.detect_compaction(messages):
            recovery = self.context.recover_from_compaction()
            if recovery:
                cap = self.config.max_memory_injection_chars
                if len(recovery) > cap:
                    recovery = recovery[:cap] + "\n[... truncated]"
                user_message = f"{user_message}\n\n---\n\n[COMPACTION RECOVERY]\n{recovery}"
                logger.info("Compaction recovery injected")

        # Session resume context: notify LLM that this is a resumed conversation
        if self._conv_manager.is_restored(self._current_user_id):
            n_msgs = len(messages)
            if n_msgs > 0:
                user_message = (
                    f"{user_message}\n\n---\n"
                    f"[SESSION RESUMED — {n_msgs} previous messages restored from last session. "
                    f"Continue the conversation naturally without mentioning the restore.]"
                )
                logger.info("Session resume context injected (%d messages)", n_msgs)
            self._conv_manager.clear_restored_flag(self._current_user_id)

        # Add user message to conversation (with images if present).
        # Pre-turn extraction: for each attached image, call Haiku with a
        # schema-enforced prompt to pull structured fields, persist the
        # extraction to workspace/memory/extractions/ (durable across
        # compaction + context resets), and inject the extracted fields
        # as text alongside the image. This removes the failure mode where
        # the main turn writes prose instead of structured data, then later
        # turns invent rows because the image has dropped from context.
        if images:
            extraction_context = ""
            if getattr(self.config, "pre_extract_images", True):
                extraction_context = await self._run_image_extractions(images)

            # Prepend extracted context to the user's text so the main model
            # has it as the first thing it reads. The image blocks still
            # follow — the model sees both signal and raw pixels.
            text_part = (
                f"{user_message}\n\n{extraction_context}".rstrip()
                if extraction_context
                else user_message
            )
            content: list[dict] = [{"type": "text", "text": text_part}]
            content.extend(images)
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_message})

        # Log to session (with user_id for replay filtering)
        self._last_user_msg_id = self.session.log_user_message(
            user_message, user_id=self._current_user_id,
        )
        return user_message

    async def _prepare_iteration(
        self, messages: list[dict], user_id: str | None, *,
        cached_system: str | None = None,
        cached_tool_defs: list[dict] | None = None,
        user_message: str = "",
        turn_prompt_override: str | None = None,
    ) -> tuple[list[dict], str, list[dict]]:
        """Shared per-iteration prep: compaction, repair, build prompt/tools.

        Pass cached_system/cached_tool_defs to reuse from the first iteration
        (system prompt and tool defs don't change within a single turn).
        user_message is used for lazy tool loading on the first iteration.

        Returns (messages, system_prompt, tool_defs).
        """
        # Tier 1: Snip old tool results (fast, no LLM)
        if self.context.needs_snip() and not self.context.needs_compaction():
            messages, freed = self.context.snip_messages(messages)
            if freed > 0:
                self._conv_manager.set_messages(user_id, messages)
                logger.info("Snipped old tool results, freed ~%d tokens", freed)

        # Tier 2: LLM summarization compaction
        if self.context.needs_compaction() and len(messages) > 6:
            # Memory flush: save durable memories BEFORE context is lost
            await memory_flush(
                messages, self.provider, self.tools,
                self._build_system_prompt, self._build_assistant_tool_message,
            )
            summary = await summarize_for_compaction(
                messages, self.provider, self.config, self.context,
            )
            compacted = self.context.compact_messages(messages, summary_text=summary)
            self._conv_manager.set_messages(user_id, compacted)
            messages = compacted
            logger.info("Proactive compaction triggered at %.1f%% (mode=%s)",
                       self.context.get_context_percent(), self.config.compaction_mode)

        # Repair messages only on the first iteration (cached_system is None)
        if cached_system is None:
            messages = strip_thinking_blocks(messages)
            messages = repair_messages(messages)
            self._conv_manager.set_messages(user_id, messages)

        if cached_system is not None:
            system = cached_system
        else:
            # Match skills on the first iteration only
            active_skills_content = ""
            if self._skills and user_message:
                from qanot.skills import match_skills, format_active_skills
                matched = match_skills(self._skills, user_message)
                if matched:
                    active_skills_content = format_active_skills(matched)
            system = self._build_system_prompt(active_skills_content=active_skills_content, turn_prompt_override=turn_prompt_override)
        # Lazy tool loading: only send tools the user likely needs
        if cached_tool_defs is not None:
            tool_defs = cached_tool_defs
        else:
            tool_defs = self.tools.get_lazy_definitions(user_message)
        return messages, system, tool_defs

    async def _init_turn(
        self, user_message: str, user_id: str | None, *, images: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Shared pre-loop setup for both streaming and non-streaming turns.

        Returns (possibly modified user_message, messages list).
        """
        self._current_user_id = user_id or ""
        self.context.turn_count += 1
        if user_id:
            self.cost_tracker.add_turn(user_id)
        messages = self._get_messages(user_id)
        user_message = await self._prepare_turn(user_message, messages, images=images)

        # Lifecycle hooks: pre-turn
        modified = await self.hooks.fire("on_pre_turn", user_id=user_id or "", message=user_message)
        if modified is not None:
            user_message = modified

        return user_message, messages
