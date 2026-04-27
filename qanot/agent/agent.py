"""Core agent — thin orchestrator over loop / preprocessing / conversation mixins.

Holds the constructor (where per-instance state and the `_instance` singleton
are set up) and the public turn API (`run_turn`, `run_turn_stream`).

Behavioural details (provider loop, tool dispatch, image extraction, message
repair, conversation eviction, snapshot persistence) live in their own modules
and are mixed in here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from qanot.config import Config
from qanot.context import ContextTracker, CostTracker
from qanot.conversation import ConversationManager
from qanot.hooks import HookRegistry
from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent
from qanot.registry import ToolRegistry
from qanot.session import SessionWriter

from .conversation import _ConversationMixin
from .loop import CONVERSATION_TTL, MAX_ITERATIONS, _LoopMixin
from .preprocessing import _PreprocessingMixin

logger = logging.getLogger(__name__)


class Agent(_LoopMixin, _PreprocessingMixin, _ConversationMixin):
    """Core agent that runs the tool_use loop."""

    def __init__(
        self,
        config: Config,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        session: SessionWriter | None = None,
        context: ContextTracker | None = None,
        prompt_mode: str = "full",
        system_prompt_override: str = "",
        hooks=None,
        _is_child: bool = False,
        max_iterations: int = MAX_ITERATIONS,
    ):
        self.config = config
        self.provider = provider
        self.tools = tool_registry
        self.session = session or SessionWriter(config.sessions_dir)
        self.context = context or ContextTracker(
            max_tokens=config.max_context_tokens,
            workspace_dir=config.workspace_dir,
        )
        self.prompt_mode = prompt_mode
        self._system_prompt_override = system_prompt_override
        self._current_user_id: str = ""
        self._current_chat_id: int | None = None
        self._current_message_id: int | None = None
        self._rag_indexer = None  # Set by main.py when RAG is enabled
        self.cost_tracker = CostTracker(config.workspace_dir)
        self._max_iterations = max_iterations
        self._is_child = _is_child
        # Per-user conversation histories keyed by user_id.
        # None key is used for non-user contexts (cron jobs, etc.)
        self._conv_manager = ConversationManager(
            history_limit=config.history_limit,
            ttl=CONVERSATION_TTL,
        )
        # Backward compat: expose raw dict for dashboard / tests
        self._conversations = self._conv_manager._conversations
        self._last_user_msg_id = ""
        # Loaded skills (populated by load_skills)
        self._skills: list = []
        # Per-user pending images queue (populated by generate_image tool)
        self._pending_images: dict[str, list[str]] = {}
        # Per-user pending files queue (populated by send_file tool)
        self._pending_files: dict[str, list[str]] = {}
        # Per-user pending videos queue (populated by render_video tool)
        self._pending_videos: dict[str, list[str]] = {}
        # Lazy-initialised image extractor (Haiku-backed pre-turn pipeline).
        # None until first image arrives; we build it lazily so turns that
        # never include images pay zero overhead.
        self._image_extractor = None
        # Lifecycle hooks
        self.hooks: HookRegistry = hooks or HookRegistry()
        # Only main agent sets _instance (child agents must not clobber it)
        if not _is_child:
            Agent._instance = self

    # Class-level reference for tools to push images without direct agent access
    _instance: "Agent | None" = None

    @classmethod
    def _push_pending_image(cls, user_id: str, image_path: str) -> None:
        """Push an image path to the pending queue for a user."""
        if cls._instance is not None:
            cls._instance._pending_images.setdefault(user_id, []).append(image_path)

    @classmethod
    def _push_pending_video(cls, user_id: str, video_path: str) -> None:
        """Push a video path to the pending queue for a user.

        Populated by ``render_video`` (qanot/tools/video.py). The Telegram
        adapter pops these after the agent turn and delivers via
        ``bot.send_video``.
        """
        if cls._instance is not None:
            cls._instance._pending_videos.setdefault(user_id, []).append(video_path)

    def pop_pending_images(self, user_id: str) -> list[str]:
        """Pop all pending image paths for a user."""
        return self._pending_images.pop(user_id, [])

    def pop_pending_files(self, user_id: str) -> list[str]:
        """Pop all pending file paths for a user."""
        return self._pending_files.pop(user_id, [])

    def pop_pending_videos(self, user_id: str) -> list[str]:
        """Pop all pending video paths for a user."""
        return self._pending_videos.pop(user_id, [])

    def attach_rag(self, rag_indexer) -> None:
        """Attach RAG indexer for auto-context injection."""
        self._rag_indexer = rag_indexer

    def load_skills(self, workspace_dir: str) -> None:
        """Discover and load skills from workspace/skills/ directory."""
        from qanot.skills import discover_skills
        self._skills = discover_skills(workspace_dir)

    @property
    def current_user_id(self) -> str:
        """Current user ID being processed (for RAG user-scoped queries)."""
        return self._current_user_id

    @property
    def current_chat_id(self) -> int | None:
        """Current Telegram chat ID being processed (for sub-agent delivery)."""
        return self._current_chat_id

    @property
    def current_message_id(self) -> int | None:
        """Current Telegram message_id being processed (for message scrubbing tools)."""
        return self._current_message_id

    async def run_turn(
        self,
        user_message: str,
        user_id: str | None = None,
        images: list[dict] | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        system_prompt_override: str | None = None,
    ) -> str:
        """Process a user message through the agent loop.

        Args:
            user_message: The user's text input.
            user_id: Unique user identifier for conversation isolation.
            images: Optional list of Anthropic-style image blocks.
            chat_id: Telegram chat ID (for sub-agent result delivery).
            system_prompt_override: Per-turn system prompt override (thread-safe).

        Returns the final text response.
        """
        # Shutdown check: if the bot is in the middle of a graceful
        # restart, don't kick off a new LLM turn that'll just get
        # aborted mid-flight. Tell the user to wait.
        from qanot.restart import bump_inflight, drop_inflight, is_shutting_down

        if is_shutting_down():
            return (
                "⚙️ Bot sozlama o'zgarishi sababli qayta ishga tushmoqda. "
                "Iltimos, 10 soniyadan keyin qayta urinib ko'ring."
            )

        async with self._get_lock(user_id):
            self._current_chat_id = chat_id
            self._current_message_id = message_id
            # Budget enforcement: reject if daily limit exceeded
            if user_id and self.config.daily_budget_usd > 0:
                allowed, spent, budget = self.cost_tracker.check_budget(
                    str(user_id), self.config.daily_budget_usd,
                )
                if not allowed:
                    return (
                        f"Kunlik budget tugadi (${spent:.4f} / ${budget:.2f}). "
                        f"Ertaga qayta urinib ko'ring yoki admin bilan bog'laning."
                    )
            bump_inflight()
            try:
                return await self._run_turn_impl(user_message, user_id, images=images, system_prompt_override=system_prompt_override)
            finally:
                drop_inflight()

    async def _run_turn_impl(self, user_message: str, user_id: str | None, *, images: list[dict] | None = None, system_prompt_override: str | None = None) -> str:
        """Internal implementation of run_turn (called under lock)."""
        user_message, messages = await self._init_turn(user_message, user_id, images=images)

        final_text = ""
        async for event in self._run_loop(user_message, user_id, messages, system_prompt_override=system_prompt_override, stream=False):
            if event.type == "done" and event.response:
                final_text = event.response.content or ""

        return final_text

    async def run_turn_stream(
        self,
        user_message: str,
        user_id: str | None = None,
        images: list[dict] | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        system_prompt_override: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Process a user message with streaming.

        Yields StreamEvent objects as they arrive from the provider.
        The final event has type="done" with the complete ProviderResponse.
        Tool-use iterations are handled internally; text deltas from each
        iteration are yielded as they arrive.
        """
        from qanot.restart import bump_inflight, drop_inflight, is_shutting_down

        if is_shutting_down():
            yield StreamEvent(
                type="done",
                response=ProviderResponse(content=(
                    "⚙️ Bot sozlama o'zgarishi sababli qayta ishga tushmoqda. "
                    "Iltimos, 10 soniyadan keyin qayta urinib ko'ring."
                )),
            )
            return

        async with self._get_lock(user_id):
            self._current_chat_id = chat_id
            self._current_message_id = message_id
            # Budget enforcement: reject if daily limit exceeded
            if user_id and self.config.daily_budget_usd > 0:
                allowed, spent, budget = self.cost_tracker.check_budget(
                    str(user_id), self.config.daily_budget_usd,
                )
                if not allowed:
                    msg = (
                        f"Kunlik budget tugadi (${spent:.4f} / ${budget:.2f}). "
                        f"Ertaga qayta urinib ko'ring yoki admin bilan bog'laning."
                    )
                    yield StreamEvent(type="done", response=ProviderResponse(content=msg))
                    return
            bump_inflight()
            try:
                async for event in self._run_turn_stream_impl(user_message, user_id, images=images, system_prompt_override=system_prompt_override):
                    yield event
            finally:
                drop_inflight()

    async def _run_turn_stream_impl(
        self, user_message: str, user_id: str | None, *, images: list[dict] | None = None, system_prompt_override: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Internal streaming implementation (called under lock)."""
        user_message, messages = await self._init_turn(user_message, user_id, images=images)

        async for event in self._run_loop(user_message, user_id, messages, system_prompt_override=system_prompt_override, stream=True):
            yield event
