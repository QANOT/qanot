"""Runtime glue: a callable bundle of (active memos, LLM client) that
the registry can invoke once per tool field without holding state itself.

The agent constructs one of these per turn (cheap — just stores a few
references) and passes it to ``ToolRegistry._maybe_validate_input``.
Decoupling the runtime from both the registry and the agent keeps the
seams loose: tests instantiate a runtime with a stub client + stub
store; production wiring lives in ``qanot/bootstrap/tool_registry.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from .spec import MemoSpec, MemoType
from .store import MemoStore
from .validator import ValidationResult, validate_text_against_memos

logger = logging.getLogger(__name__)


class MemoValidatorRuntime:
    """Callable wrapper: ``await runtime(text, field_context=...) -> ValidationResult``.

    The runtime owns three references:
      - the LLM client (anthropic AsyncAnthropic, or any duck-typed shim)
      - a list of active feedback memos for the current scope
      - a model name (defaults to Haiku per ``validator.VALIDATOR_MODEL``)

    Construct once per turn when there ARE rules in scope. When there
    are none, return None instead of a runtime — the registry skips the
    validator entirely and pays zero LLM cost.
    """

    def __init__(
        self,
        *,
        client: Any,
        active_memos: list[MemoSpec],
        model: str | None = None,
    ):
        self.client = client
        self.active_memos = active_memos
        self.model = model  # None → validator uses its default

    @property
    def has_rules(self) -> bool:
        return any(m.type == MemoType.FEEDBACK for m in self.active_memos)

    async def __call__(
        self, text: str, *, field_context: str,
    ) -> ValidationResult:
        kwargs: dict[str, Any] = {
            "field_context": field_context,
            "active_memos": self.active_memos,
            "client": self.client,
        }
        if self.model is not None:
            kwargs["model"] = self.model
        return await validate_text_against_memos(text, **kwargs)


# ─── factory used by the bootstrap layer ────────────────────────


def build_runtime(
    *,
    client: Any,
    workspace_dir: str,
    user_id: str | None = None,
    thread_id: str | None = None,
) -> MemoValidatorRuntime | None:
    """Pull active feedback memos for the current scope and bundle them
    with the LLM client. Returns None when no rules apply — the registry
    treats that as "validation disabled" and skips the LLM call entirely.

    This is the per-turn lookup the registry calls via the factory the
    agent installs at startup. Cheap: scope filtering is in-memory once
    the memos directory has been read.
    """
    if client is None:
        return None
    store = MemoStore(workspace_dir)
    active = store.list_in_scope(user_id=user_id, thread_id=thread_id)
    feedback = [m for m in active if m.type == MemoType.FEEDBACK]
    if not feedback:
        return None
    return MemoValidatorRuntime(client=client, active_memos=feedback)
