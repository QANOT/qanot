"""JSONL audit log for userbot events.

One line per event appended to ``<workspace_dir>/userbot_audit.log``. We
never log the full message text — a 200-char preview plus length is
enough for debugging without turning the log into a privacy hazard.

The writer tolerates a missing workspace dir (creates it on first write)
and uses ``os.fsync`` so crashes don't lose the line — the userbot has
real-world side effects and the audit trail is the only way to
reconstruct what the agent did on someone else's behalf.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUDIT_FILENAME = "userbot_audit.log"
PREVIEW_MAX = 200


class AuditLog:
    """Append-only JSONL writer."""

    def __init__(self, workspace_dir: str | os.PathLike[str]) -> None:
        self._path = Path(workspace_dir) / AUDIT_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def _write(self, entry: dict[str, Any]) -> None:
        entry.setdefault("ts", _utc_iso())
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Optional fs_safe check — don't hard-fail if the helper moves.
            try:
                from qanot.fs_safe import validate_write_path

                err = validate_write_path(str(self._path))
                if err:
                    logger.warning("userbot audit path blocked: %s", err)
                    return
            except Exception:  # pragma: no cover — framework-absence path
                pass

            # Plain open()+fsync — an aiofile dep would be overkill for one
            # line at a time, and we WANT the sync so a crash right after
            # an RPC send still leaves evidence.
            line = json.dumps(entry, ensure_ascii=False)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # Some filesystems (e.g. tmpfs in CI) reject fsync; the
                    # write itself is still durable for our purposes.
                    pass
        except Exception as e:
            logger.warning("userbot audit write failed: %s", e)

    # ── Event helpers ────────────────────────────────────────────

    def send(
        self,
        *,
        recipient_id: str,
        recipient: str,
        text: str,
        message_id: int,
        reply_to_message_id: int | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "event": "send",
            "recipient_id": recipient_id,
            "recipient": recipient,
            "text_preview": _preview(text),
            "text_len": len(text),
            "message_id": message_id,
        }
        if reply_to_message_id:
            entry["reply_to_message_id"] = reply_to_message_id
        self._write(entry)

    def dry_run(
        self,
        *,
        recipient_id: str,
        recipient: str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Record a draft that the agent prepared but did NOT send.

        Logged separately from real sends so we can see how often the agent
        proposes drafts vs. how often the operator actually approves.
        """
        entry: dict[str, Any] = {
            "event": "dry_run",
            "recipient_id": recipient_id,
            "recipient": recipient,
            "text_preview": _preview(text),
            "text_len": len(text),
        }
        if reply_to_message_id:
            entry["reply_to_message_id"] = reply_to_message_id
        self._write(entry)

    def rate_limit(
        self,
        *,
        recipient: str,
        bucket: str,
        retry_after: int,
    ) -> None:
        self._write({
            "event": "rate_limit",
            "recipient": recipient,
            "bucket": bucket,
            "retry_after": retry_after,
        })

    def whitelist_reject(self, *, recipient: str) -> None:
        self._write({
            "event": "whitelist_reject",
            "recipient": recipient,
        })

    def send_error(self, *, recipient: str, error_class: str) -> None:
        self._write({
            "event": "send_error",
            "recipient": recipient,
            "error_class": error_class,
        })


def _utc_iso() -> str:
    t = time.gmtime()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


def _preview(text: str) -> str:
    if len(text) <= PREVIEW_MAX:
        return text
    return text[:PREVIEW_MAX]
