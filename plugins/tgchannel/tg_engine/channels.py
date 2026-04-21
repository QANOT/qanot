"""Persistent channel list for the tgchannel plugin.

Channels are stored in a JSON file under the bot's workspace so state
survives container restarts without requiring platform-side config
rewrites. File: workspace/memory/channels.json

Format:
{
    "channels": [
        {"id": -1001234567890, "title": "...", "username": "kanal_username"},
        ...
    ],
    "default_channel_id": -1001234567890
}
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Relative to workspace_dir
STATE_REL_PATH = Path("memory") / "channels.json"


class ChannelStore:
    """In-memory mirror of channels.json with atomic writes on update."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self._path = Path(workspace_dir) / STATE_REL_PATH
        self.channels: list[dict[str, Any]] = []
        self.default_channel_id: int | None = None
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("channels.json read failed: %s", e)
            return
        if isinstance(data, dict):
            raw = data.get("channels") or []
            if isinstance(raw, list):
                self.channels = [c for c in raw if isinstance(c, dict) and c.get("id")]
            dflt = data.get("default_channel_id")
            if isinstance(dflt, int):
                self.default_channel_id = dflt

    def _save(self) -> None:
        """Atomic write via temp-file + rename."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("channels dir mkdir failed: %s", e)
            return
        payload = {
            "channels": self.channels,
            "default_channel_id": self.default_channel_id,
        }
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".channels-", suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, str(self._path))
        except OSError as e:
            logger.warning("channels.json write failed: %s", e)

    # ── CRUD ────────────────────────────────────────────────────

    def add(self, chat: dict) -> bool:
        """Upsert a channel by id. Returns True if newly added, False if updated."""
        cid = chat.get("id")
        if not cid:
            raise ValueError("channel dict must contain id")
        entry = {
            "id": int(cid),
            "title": str(chat.get("title") or ""),
            "username": str(chat.get("username") or ""),
        }
        for i, existing in enumerate(self.channels):
            if existing.get("id") == entry["id"]:
                self.channels[i] = entry
                self._save()
                return False
        self.channels.append(entry)
        if self.default_channel_id is None:
            self.default_channel_id = entry["id"]
        self._save()
        return True

    def remove(self, channel_id: int) -> bool:
        """Drop a channel. Returns True if removed."""
        before = len(self.channels)
        self.channels = [c for c in self.channels if c.get("id") != channel_id]
        removed = len(self.channels) < before
        if removed and self.default_channel_id == channel_id:
            self.default_channel_id = (
                self.channels[0]["id"] if self.channels else None
            )
        if removed:
            self._save()
        return removed

    def list_all(self) -> list[dict]:
        return [dict(c) for c in self.channels]

    def resolve(self, user_value: str | int | None) -> int | None:
        """Resolve user input to a numeric channel_id.

        Accepts:
          - None/empty → default_channel_id
          - int → pass through
          - "-1001234..." → parsed as int
          - "@username" or "username" → match by username (case-insensitive)
          - channel title (case-insensitive substring) → best-effort match

        Returns None if nothing resolves.
        """
        if user_value is None or user_value == "":
            return self.default_channel_id
        if isinstance(user_value, int):
            return user_value
        v = str(user_value).strip()
        if not v:
            return self.default_channel_id
        # Numeric like "-1001234567890"
        try:
            as_int = int(v)
            return as_int
        except ValueError:
            pass
        # @username or bare username
        uname = v.lstrip("@").lower()
        for c in self.channels:
            if (c.get("username") or "").lower() == uname:
                return int(c["id"])
        # Title substring fallback
        for c in self.channels:
            if uname in (c.get("title") or "").lower():
                return int(c["id"])
        return None

    def set_default(self, channel_id: int) -> bool:
        if not any(c.get("id") == channel_id for c in self.channels):
            return False
        self.default_channel_id = channel_id
        self._save()
        return True
