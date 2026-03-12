"""JSONL session read/write — compatible with LiveBuilder's activity_monitor."""

from __future__ import annotations

import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from qanot.providers.base import Usage

logger = logging.getLogger(__name__)


class SessionWriter:
    """Append-only JSONL session writer with file locking."""

    def __init__(self, sessions_dir: str = "/data/sessions"):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._session_id: str | None = None
        self._msg_counter = 0

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            self._session_id = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._session_id

    @property
    def session_path(self) -> Path:
        return self.sessions_dir / f"{self.session_id}.jsonl"

    def _next_id(self) -> str:
        self._msg_counter += 1
        return f"msg_{self._msg_counter:06d}"

    def log_user_message(self, text: str, parent_id: str = "") -> str:
        """Log a user message. Returns the message ID."""
        msg_id = self._next_id()
        entry = {
            "type": "message",
            "id": msg_id,
            "parentId": parent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {
                "role": "user",
                "content": text,
            },
        }
        self._append(entry)
        return msg_id

    def log_assistant_message(
        self,
        text: str,
        tool_uses: list[dict] | None = None,
        usage: Usage | None = None,
        parent_id: str = "",
        model: str = "",
    ) -> str:
        """Log an assistant message. Returns the message ID."""
        msg_id = self._next_id()

        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        if tool_uses:
            for tu in tool_uses:
                content.append({
                    "type": "tool_use",
                    "name": tu.get("name", ""),
                    "input": tu.get("input", {}),
                })

        entry: dict = {
            "type": "message",
            "id": msg_id,
            "parentId": parent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {
                "role": "assistant",
                "content": content,
            },
        }

        if model:
            entry["model"] = model

        if usage:
            entry["usage"] = {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "cacheRead": usage.cache_read_input_tokens,
                "cacheWrite": usage.cache_creation_input_tokens,
                "cost": {"total": usage.cost},
            }

        self._append(entry)
        return msg_id

    def _append(self, entry: dict) -> None:
        """Append a JSON entry to the session file with file locking."""
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(self.session_path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def new_session(self, session_id: str | None = None) -> None:
        """Start a new session with an optional custom ID."""
        self._session_id = session_id or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._msg_counter = 0
