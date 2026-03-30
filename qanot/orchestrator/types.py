"""Core data types for the orchestrator subsystem."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


# --- Run statuses ---
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMEOUT})

# --- Spawn modes ---
MODE_SYNC = "sync"
MODE_ASYNC = "async"
MODE_CONVERSATION = "conversation"

# --- Agent roles (depth-based) ---
ROLE_MAIN = "main"
ROLE_ORCHESTRATOR = "orchestrator"
ROLE_LEAF = "leaf"


@dataclass
class SubagentRun:
    """Tracks a single sub-agent execution."""

    run_id: str
    parent_user_id: str
    parent_chat_id: int | None
    task: str
    agent_id: str
    agent_name: str
    role: str  # "main" | "orchestrator" | "leaf"
    depth: int  # 0 = main, 1 = child, 2 = grandchild
    status: str  # pending | running | completed | failed | cancelled | timeout
    mode: str  # sync | async | conversation

    # Lifecycle timestamps (monotonic for elapsed, epoch for persistence)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None

    # Result
    result_text: str | None = None
    error: str | None = None

    # Stats
    token_input: int = 0
    token_output: int = 0
    cost: float = 0.0

    # Config captured at spawn time
    model: str = ""
    timeout: int = 120
    max_iterations: int = 15

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.ended_at or time.time()
        return end - self.started_at

    @property
    def token_total(self) -> int:
        return self.token_input + self.token_output

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubagentRun:
        # Filter to only known fields
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class SpawnParams:
    """Parameters for spawning a sub-agent."""

    task: str
    agent_id: str = ""  # empty = auto-select or use default
    mode: str = MODE_SYNC  # sync | async | conversation
    context: str = ""  # scoped context from parent (max 4000 chars)
    model: str = ""  # model override
    timeout: int = 120
    max_iterations: int = 15
    max_turns: int = 5  # conversation mode only
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)


@dataclass
class AnnouncePayload:
    """Structured result from a completed sub-agent."""

    run_id: str
    agent_id: str
    agent_name: str
    status: str  # completed | failed | timeout
    result: str
    elapsed_seconds: float
    token_input: int = 0
    token_output: int = 0
    cost: float = 0.0

    def format_stats_line(self) -> str:
        """Format compact stats (OpenClaw-style)."""
        parts = [f"runtime {self.elapsed_seconds:.1f}s"]
        total = self.token_input + self.token_output
        if total > 0:
            parts.append(f"tokens {_fmt_tokens(total)} (in {_fmt_tokens(self.token_input)} / out {_fmt_tokens(self.token_output)})")
        if self.cost > 0:
            parts.append(f"cost ${self.cost:.4f}")
        return f"Stats: {' \u2022 '.join(parts)}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt_tokens(n: int) -> str:
    """Format token count: 1500 -> '1.5k', 1500000 -> '1.5m'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def make_run_id() -> str:
    """Generate a unique run ID."""
    return uuid.uuid4().hex[:12]
