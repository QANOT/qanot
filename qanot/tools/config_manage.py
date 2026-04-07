"""Config secret management tools — agent proposes, user approves.

Security model (non-negotiable):

1. A hard-coded allowlist gates which fields the agent can touch via chat.
   System-critical secrets (bot_token, api_key, provider api_keys) are NEVER
   settable through this flow — they require SSH + ``qanot config set``.
2. Secrets never land in ``config.json`` plaintext. On approval we:
     a) write ``QANOT_<FIELD>=<value>`` to ``config.secrets_env_path`` (chmod 0600),
     b) write ``{"env": "QANOT_<FIELD>"}`` as a SecretRef in config.json,
     c) set ``os.environ[QANOT_<FIELD>]`` so the current process can resolve it
        even before the next restart.
3. The original user message is scrubbed via ``bot.delete_message`` before the
   proposal is even stored — this shrinks the exposure window on Telegram's
   servers. If the delete fails, the tool surfaces it in the result so the
   agent can ask the user to delete manually.
4. Only the user who triggered the proposal can approve it. TTL 10 minutes.
5. Every propose/approve/deny/expire event is audit-logged to the daily note.
   The audit line contains the field name and a short SHA256 prefix of the
   value for correlation — NEVER the value itself.
6. Approval cards mask the value: first 4 + ``***`` + last 4 + length.
   Values shorter than 12 chars show ``*** (len N)`` with no prefix/suffix.
7. On approval the transactional write does config.json + secrets.env atomically
   with rollback on partial failure.

This module also exposes ``delete_message`` as a standalone tool so the agent
can scrub sensitive messages outside the config flow.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

from qanot.tools._approval_base import PROPOSAL_TTL_SECONDS, now as _now, append_audit as _base_append_audit

# Fields the agent may set via chat. Everything else is rejected at the tool
# layer. Keep this list MINIMAL — system-critical secrets must require SSH.
ALLOWED_FIELDS: frozenset[str] = frozenset({
    "brave_api_key",
    "voice_api_key",
    "image_api_key",
})

# Explicit denylist for clarity in error messages.
_HARD_NO_FIELDS: frozenset[str] = frozenset({
    "bot_token",
    "api_key",
})

_MIN_VALUE_LEN = 8
_MAX_VALUE_LEN = 4096

# Common placeholder strings we reject so the agent doesn't propose junk.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "todo", "tbd", "your-key-here", "your_key_here", "xxx", "xxxx",
    "replace-me", "replace_me", "placeholder", "example", "changeme",
    "your-api-key", "your_api_key", "<api_key>", "none", "null",
})

# Module-level flag to prevent double-registration (idempotent).
_REGISTERED_REGISTRIES: set[int] = set()


# ── Helpers ───────────────────────────────────────────────────────────




def _mask_value(v: str) -> str:
    """Return a display-safe masked value for approval cards."""
    n = len(v)
    if n < 12:
        return f"*** (len {n})"
    return f"{v[:4]}***{v[-4:]} (len {n})"


def _value_hash(v: str) -> str:
    """Short SHA256 prefix used for audit correlation. Never reversible."""
    return hashlib.sha256(v.encode("utf-8")).hexdigest()[:8]


def _validate_field(field: str) -> str | None:
    if not field:
        return "field is required"
    if field in _HARD_NO_FIELDS:
        return (
            f"Field '{field}' is system-critical and cannot be set via chat. "
            f"Use 'qanot config set' on the server over SSH."
        )
    if field not in ALLOWED_FIELDS:
        return (
            f"Field '{field}' is not settable via chat. "
            f"Allowlisted fields: {', '.join(sorted(ALLOWED_FIELDS))}. "
            f"For other fields, use 'qanot config set' on the server."
        )
    return None


def _validate_value(v: str) -> str | None:
    if not isinstance(v, str):
        return "value must be a string"
    stripped = v.strip()
    if not stripped:
        return "value is empty"
    if len(stripped) < _MIN_VALUE_LEN:
        return f"value is too short (min {_MIN_VALUE_LEN} chars)"
    if len(stripped) > _MAX_VALUE_LEN:
        return f"value is too long (max {_MAX_VALUE_LEN} chars)"
    if stripped.lower() in _PLACEHOLDER_VALUES:
        return f"value looks like a placeholder ({stripped!r}) — refusing"
    # Control chars would break .env parsing and SecretRef resolution.
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in stripped):
        return "value contains control characters"
    return None


def _env_var_name(field: str) -> str:
    return f"QANOT_{field.upper()}"


def _update_secrets_env(path: Path, var_name: str, value: str) -> None:
    """Read existing secrets.env (if any), upsert ``var_name=value``, write
    atomically, chmod 0600.

    Lines are formatted ``KEY=VALUE`` with no quoting. We reject values
    containing ``\\n`` / ``\\r`` upstream in ``_validate_value`` so shell-style
    escaping is not required.
    """
    from qanot.utils import atomic_write

    existing_lines: list[str] = []
    if path.exists():
        try:
            existing_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise RuntimeError(f"cannot read {path}: {e}") from e

    prefix = f"{var_name}="
    replaced = False
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.lstrip()
        # Preserve comments and blank lines verbatim.
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if stripped.startswith(prefix):
            if not replaced:
                new_lines.append(f"{var_name}={value}")
                replaced = True
            # Skip duplicate occurrences.
            continue
        new_lines.append(line)

    if not replaced:
        new_lines.append(f"{var_name}={value}")

    content = "\n".join(new_lines)
    if not content.endswith("\n"):
        content += "\n"

    atomic_write(path, content)
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        # chmod failure on some filesystems is non-fatal but worth warning.
        logger.warning("chmod 0600 failed on %s: %s", path, e)


def _atomic_set_config_secretref(field: str, env_var: str) -> Any:
    """Write ``raw[field] = {"env": env_var}`` to config.json atomically.

    Returns the previous value (for rollback) or a sentinel if the field was
    absent.
    """
    from qanot.config import read_config_json, write_config_json
    raw = read_config_json()
    old_value = raw.get(field, _MISSING)
    raw[field] = {"env": env_var}
    write_config_json(raw)
    return old_value


def _rollback_config(field: str, old_value: Any) -> None:
    """Best-effort rollback of a config.json change."""
    from qanot.config import read_config_json, write_config_json
    try:
        raw = read_config_json()
        if old_value is _MISSING:
            raw.pop(field, None)
        else:
            raw[field] = old_value
        write_config_json(raw)
    except Exception as e:
        logger.error("Config rollback failed for %s: %s", field, e)


class _Missing:
    def __repr__(self) -> str:
        return "<MISSING>"


_MISSING = _Missing()


def _append_audit(workspace_dir: str, user_id: str, event: str, details: dict) -> None:
    """Append a config-secret audit event to the daily note."""
    _base_append_audit(workspace_dir, user_id, event, details, tag="secret")


def _trigger_restart(reason: str) -> None:
    """Reuse the mcp_manage restart helper for consistency."""
    from qanot.tools.mcp_manage import _trigger_restart as mcp_trigger
    mcp_trigger(reason)


# ── Tool registration ────────────────────────────────────────────────


def register_config_tools(
    registry: "ToolRegistry",
    config: "Config",
    telegram_adapter: Any,
    *,
    get_user_id: Callable[[], str],
    get_chat_id: Callable[[], int | None],
    get_message_id: Callable[[], int | None],
    get_bot: Callable[[], Any],
) -> None:
    """Register delete_message and config_set_secret tools.

    Idempotent: calling twice on the same registry is a no-op the second time.
    """
    registry_id = id(registry)
    if registry_id in _REGISTERED_REGISTRIES:
        logger.debug("Config management tools already registered on this registry")
        return
    _REGISTERED_REGISTRIES.add(registry_id)

    if telegram_adapter is not None and not hasattr(
        telegram_adapter, "_pending_config_proposals"
    ):
        telegram_adapter._pending_config_proposals = {}

    # ── delete_message ──
    async def delete_message(params: dict) -> str:
        msg_id = params.get("message_id")
        chat_id = params.get("chat_id")
        if msg_id is None:
            msg_id = get_message_id()
        if chat_id is None:
            chat_id = get_chat_id()
        if msg_id is None:
            return json.dumps({
                "success": False,
                "error": "no message_id available (no active message context)",
            })
        if chat_id is None:
            return json.dumps({
                "success": False,
                "error": "no chat_id available (no active chat context)",
            })
        try:
            bot = get_bot()
            if bot is None:
                return json.dumps({"success": False, "error": "bot unavailable"})
            await bot.delete_message(chat_id=int(chat_id), message_id=int(msg_id))
            return json.dumps({"success": True, "chat_id": int(chat_id), "message_id": int(msg_id)})
        except Exception as e:
            logger.warning("delete_message failed: %s", e)
            return json.dumps({"success": False, "error": str(e)})

    # ── config_set_secret ──
    async def config_set_secret(params: dict) -> str:
        field = (params.get("field") or "").strip()
        value_raw = params.get("value")
        source = (params.get("source") or "").strip()
        reason = (params.get("reason") or "").strip()

        # Step 1: scrub the triggering user message FIRST, before anything else.
        # Even if validation fails, the raw value may be in the message body —
        # deleting it is always the right call.
        scrubbed = False
        scrub_error: str | None = None
        try:
            bot = get_bot()
            msg_id = get_message_id()
            chat_id_for_delete = get_chat_id()
            if bot is not None and msg_id is not None and chat_id_for_delete is not None:
                await bot.delete_message(chat_id=int(chat_id_for_delete), message_id=int(msg_id))
                scrubbed = True
            else:
                scrub_error = "no active message context"
        except Exception as e:
            scrub_error = str(e)
            logger.warning("config_set_secret: scrub failed: %s", e)

        # Step 2: allowlist check.
        if err := _validate_field(field):
            _append_audit(config.workspace_dir, get_user_id() or "", "propose_rejected", {
                "field": field, "error": err, "source": source,
            })
            return json.dumps({
                "success": False,
                "error": err,
                "message_scrubbed": scrubbed,
                "scrub_error": scrub_error,
            })

        if not source:
            return json.dumps({
                "success": False,
                "error": "'source' is required — where did this value come from?",
                "message_scrubbed": scrubbed,
            })
        if not reason:
            return json.dumps({
                "success": False,
                "error": "'reason' is required — explain why the user wants this set.",
                "message_scrubbed": scrubbed,
            })

        # Step 3: value validation.
        if not isinstance(value_raw, str):
            return json.dumps({
                "success": False,
                "error": "'value' must be a string",
                "message_scrubbed": scrubbed,
            })
        value = value_raw.strip()
        if err := _validate_value(value):
            _append_audit(config.workspace_dir, get_user_id() or "", "propose_rejected", {
                "field": field, "error": err, "source": source,
                "value_hash": _value_hash(value) if value else "",
            })
            return json.dumps({
                "success": False,
                "error": err,
                "message_scrubbed": scrubbed,
            })

        # Step 4: resolve chat/user context for the approval card.
        user_id_str = get_user_id() or ""
        chat_id = get_chat_id()
        if chat_id is None:
            return json.dumps({
                "success": False,
                "error": "No active chat — config_set_secret must be called from a user conversation.",
                "message_scrubbed": scrubbed,
            })
        try:
            user_id_int = int(user_id_str) if user_id_str else 0
        except ValueError:
            user_id_int = 0
        if not user_id_int:
            return json.dumps({
                "success": False,
                "error": "Cannot identify user — config_set_secret requires a user context.",
                "message_scrubbed": scrubbed,
            })

        # Step 5: store pending proposal.
        proposal_id = hashlib.sha256(
            f"cfg:{user_id_int}:{field}:{_now()}".encode()
        ).hexdigest()[:12]
        vhash = _value_hash(value)
        masked = _mask_value(value)

        pending = {
            "field": field,
            "value": value,
            "source": source,
            "reason": reason,
            "user_id": user_id_int,
            "chat_id": int(chat_id),
            "message_id": get_message_id(),
            "expires_at": _now() + PROPOSAL_TTL_SECONDS,
            "value_hash": vhash,
        }
        telegram_adapter._pending_config_proposals[proposal_id] = pending

        _append_audit(config.workspace_dir, user_id_str, "propose", {
            "proposal_id": proposal_id,
            "field": field,
            "source": source,
            "reason": reason,
            "value_hash": vhash,
            "message_scrubbed": scrubbed,
        })

        # Step 6: send the approval card.
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            scrub_line = (
                "🧹 Xabar o'chirildi (sirlarni muhofaza qilish uchun)"
                if scrubbed
                else "⚠️ Xabarni o'chirib bo'lmadi — qo'lda o'chiring"
            )
            text = (
                f"🔐 *Maxfiy sozlama taklifi*\n\n"
                f"*Maydon:* `{field}`\n"
                f"*Qiymat:* `{masked}`\n"
                f"*Manba:* {source}\n"
                f"*Sabab:* {reason}\n\n"
                f"{scrub_line}\n\n"
                f"⚠️ Bu qiymat `{config.secrets_env_path}` ichiga yoziladi, "
                f"`config.json` da faqat reference saqlanadi.\n\n"
                f"_10 daqiqada bekor bo'ladi. Tasdiqlangandan so'ng bot restart bo'ladi._"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ Tasdiqlash",
                    callback_data=f"cfg_approve:{proposal_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Rad etish",
                    callback_data=f"cfg_deny:{proposal_id}",
                ),
            ]])
            bot = get_bot()
            await bot.send_message(
                int(chat_id), text, reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Failed to send config approval card: %s", e)
            telegram_adapter._pending_config_proposals.pop(proposal_id, None)
            return json.dumps({
                "success": False,
                "error": f"Failed to send approval card: {e}",
                "message_scrubbed": scrubbed,
            })

        return json.dumps({
            "success": True,
            "proposal_id": proposal_id,
            "status": "awaiting_approval",
            "field": field,
            "masked_value": masked,
            "message_scrubbed": scrubbed,
            "scrub_error": scrub_error,
            "message": (
                "Tasdiqlash kartasi yuborildi. Foydalanuvchi tugmani bossin — "
                "qabul qilingandan keyin bot restart bo'ladi. Endi foydalanuvchiga "
                "bu token chat loglarida qolganini va provider dashboardda "
                "rotate qilish kerakligini ayting."
            ),
        })

    # ── Register ──
    registry.register(
        name="delete_message",
        description=(
            "Delete a Telegram message. Use this to scrub sensitive content "
            "(API keys, tokens, passwords) from chat history immediately. "
            "Defaults to the user's current incoming message when called "
            "with no arguments. Safe to call even if the message cannot be "
            "deleted — returns an error in the result."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "description": "Telegram message_id to delete. Defaults to the current incoming message.",
                },
                "chat_id": {
                    "type": "integer",
                    "description": "Chat ID. Defaults to the current chat.",
                },
            },
        },
        handler=delete_message,
        category="core",
    )

    registry.register(
        name="config_set_secret",
        description=(
            "Propose setting a credential config field. Use this when the user "
            "pastes an API key in chat — the tool immediately scrubs the user's "
            "message, then sends a Telegram approval card showing a masked "
            "preview. On approval, the value is written to the secrets env file "
            "(not config.json) and the bot restarts. Allowlisted fields only: "
            "brave_api_key, voice_api_key, image_api_key. System-critical "
            "fields (bot_token, api_key, provider keys) cannot be set via chat — "
            "those require SSH + 'qanot config set'. NEVER lecture the user "
            "about pasting credentials — scrub the message, propose the change, "
            "and remind them to rotate the token at the provider dashboard."
        ),
        parameters={
            "type": "object",
            "required": ["field", "value", "source", "reason"],
            "properties": {
                "field": {
                    "type": "string",
                    "enum": sorted(ALLOWED_FIELDS),
                    "description": "Config field name (allowlisted).",
                },
                "value": {
                    "type": "string",
                    "description": "The secret value. Stored in secrets.env, not config.json.",
                },
                "source": {
                    "type": "string",
                    "description": "Where the value came from (e.g. 'user pasted in chat').",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the user wants this set.",
                },
            },
        },
        handler=config_set_secret,
        category="core",
    )

    # ── config_toggle ──
    # Safe boolean fields the agent can toggle (no secrets, no critical paths)
    _TOGGLEABLE_FIELDS: frozenset[str] = frozenset({
        "voicecall_enabled",
        "reactions_enabled",
        "routing_enabled",
        "code_execution",
        "browser_enabled",
        "dashboard_enabled",
        "rag_enabled",
        "memory_tool",
        "backup_enabled",
        "heartbeat_enabled",
        "briefing_enabled",
        "agents_enabled",
    })

    # Fields that require additional config to be present before enabling
    _TOGGLE_PREREQUISITES: dict[str, list[str]] = {
        "voicecall_enabled": ["voicecall_api_id", "voicecall_api_hash", "voicecall_session"],
    }

    async def config_toggle(params: dict) -> str:
        """Toggle a boolean config field. Requires bot restart to take effect."""
        field_name = (params.get("field") or "").strip()
        value = params.get("value")  # True/False or "true"/"false"

        if not field_name:
            return json.dumps({"error": "field is required"})

        if field_name not in _TOGGLEABLE_FIELDS:
            return json.dumps({
                "error": f"Field '{field_name}' is not toggleable. "
                f"Allowed: {', '.join(sorted(_TOGGLEABLE_FIELDS))}",
            })

        # Normalize value
        if isinstance(value, str):
            value = value.lower() in ("true", "1", "on", "yes")
        elif value is None:
            # If no value given, toggle current
            current = getattr(config, field_name, False)
            value = not current

        # Check prerequisites
        prereqs = _TOGGLE_PREREQUISITES.get(field_name, [])
        if value and prereqs:
            missing = [p for p in prereqs if not getattr(config, p, None)]
            if missing:
                return json.dumps({
                    "error": f"Cannot enable {field_name}: missing required config fields: {', '.join(missing)}. "
                    f"Set them first with config_set_secret or qanot config set.",
                })

        # Apply
        setattr(config, field_name, value)

        # Persist to config.json
        try:
            from qanot.config import read_config_json, write_config_json
            raw = read_config_json()
            raw[field_name] = value
            write_config_json(raw)
        except Exception as e:
            logger.warning("Failed to persist config toggle %s: %s", field_name, e)
            return json.dumps({
                "success": True,
                "field": field_name,
                "value": value,
                "warning": "Changed in memory but failed to persist to disk. Will reset on restart.",
            })

        status = "enabled" if value else "disabled"
        needs_restart = field_name in {"voicecall_enabled", "browser_enabled", "agents_enabled", "webhook_enabled", "webchat_enabled"}

        result = {
            "success": True,
            "field": field_name,
            "value": value,
            "message": f"{field_name} {status}.",
        }
        if needs_restart:
            result["note"] = "Restart required for this change to take effect. Use /stop then qanot start, or qanot restart."

        return json.dumps(result)

    registry.register(
        name="config_toggle",
        description="Toggle a boolean config field on/off. For features like voice calls, routing, code execution, reactions, browser, etc.",
        parameters={
            "type": "object",
            "required": ["field"],
            "properties": {
                "field": {
                    "type": "string",
                    "description": "Config field to toggle",
                    "enum": sorted(_TOGGLEABLE_FIELDS),
                },
                "value": {
                    "type": "boolean",
                    "description": "Set to true (enable) or false (disable). If omitted, toggles current value.",
                },
            },
        },
        handler=config_toggle,
        category="core",
    )

    logger.info("Config management tools registered (delete_message, config_set_secret, config_toggle)")


# ── Callback handlers ─────────────────────────────────────────────────


async def handle_config_approve_callback(
    adapter: Any,
    config: "Config",
    callback: Any,
    proposal_id: str,
) -> None:
    """Handle cfg_approve:<id> callback — perform the transactional write."""
    user_id = callback.from_user.id
    pending = adapter._pending_config_proposals.get(proposal_id)

    if not pending:
        await callback.answer("Bu so'rov muddati tugagan yoki topilmadi.", show_alert=True)
        return

    if _now() > pending.get("expires_at", 0):
        adapter._pending_config_proposals.pop(proposal_id, None)
        _append_audit(config.workspace_dir, str(user_id), "propose_expired", {
            "proposal_id": proposal_id,
            "field": pending.get("field", ""),
        })
        await callback.answer("Muddati tugagan.", show_alert=True)
        try:
            await callback.message.edit_text(
                f"{callback.message.text}\n\n⏰ Muddati tugadi",
            )
        except Exception:
            pass
        return

    if pending["user_id"] != user_id:
        await callback.answer(
            "Faqat so'rov egasi ruxsat berishi mumkin.", show_alert=True,
        )
        return

    # Consume atomically.
    adapter._pending_config_proposals.pop(proposal_id, None)

    field = pending["field"]
    value = pending["value"]
    env_var = _env_var_name(field)
    secrets_path = Path(config.secrets_env_path)
    vhash = pending["value_hash"]

    # Transactional write: secrets.env first (it's the value store), then
    # config.json (the reference). If config.json fails after secrets.env
    # succeeded, the leftover env var is harmless — nothing references it yet.
    try:
        _update_secrets_env(secrets_path, env_var, value)
    except Exception as e:
        _append_audit(config.workspace_dir, str(user_id), "write_failed", {
            "proposal_id": proposal_id, "field": field, "value_hash": vhash,
            "stage": "secrets_env", "error": str(e),
        })
        try:
            await callback.message.edit_text(
                f"{callback.message.text}\n\n❌ secrets.env yozishda xato: {e}",
            )
        except Exception:
            pass
        await callback.answer(f"Xato: {e}", show_alert=True)
        return

    # Export to current process so in-process resolution works immediately.
    os.environ[env_var] = value

    # Update config.json with SecretRef.
    try:
        old_value = _atomic_set_config_secretref(field, env_var)
    except Exception as e:
        # Roll back the env-var export and attempt to roll back secrets.env.
        os.environ.pop(env_var, None)
        try:
            # Best-effort: rewrite secrets.env without our new var. We don't
            # know what it was before (may not have existed), so we remove it.
            if secrets_path.exists():
                lines = secrets_path.read_text(encoding="utf-8").splitlines()
                filtered = [
                    ln for ln in lines
                    if not ln.lstrip().startswith(f"{env_var}=")
                ]
                content = "\n".join(filtered)
                if filtered and not content.endswith("\n"):
                    content += "\n"
                from qanot.utils import atomic_write
                atomic_write(secrets_path, content)
        except Exception as rollback_err:
            logger.error("secrets.env rollback failed: %s", rollback_err)

        _append_audit(config.workspace_dir, str(user_id), "write_failed", {
            "proposal_id": proposal_id, "field": field, "value_hash": vhash,
            "stage": "config_json", "error": str(e),
        })
        try:
            await callback.message.edit_text(
                f"{callback.message.text}\n\n❌ config.json yozishda xato: {e}",
            )
        except Exception:
            pass
        await callback.answer(f"Xato: {e}", show_alert=True)
        return

    # Mirror into in-memory Config so tools that read config.<field> directly
    # see the value without waiting for the restart to complete.
    try:
        setattr(config, field, value)
    except Exception as e:
        logger.warning("Failed to mirror %s into in-memory Config: %s", field, e)

    _append_audit(config.workspace_dir, str(user_id), "approved", {
        "proposal_id": proposal_id,
        "field": field,
        "value_hash": vhash,
        "env_var": env_var,
        "had_previous": old_value is not _MISSING,
    })

    logger.warning(
        "Secret field %s written to %s. Operator must source this file "
        "from their systemd/launchd unit for the variable to persist across "
        "restarts.", field, secrets_path,
    )

    try:
        await callback.message.edit_text(
            f"{callback.message.text}\n\n✅ Saqlandi — 2 soniyadan keyin qayta ishga tushyapti...",
        )
    except Exception:
        pass
    await callback.answer("✅ Saqlandi, restart...")

    _trigger_restart(f"config_set_secret: {field}")


async def handle_config_deny_callback(
    adapter: Any,
    config: "Config",
    callback: Any,
    proposal_id: str,
) -> None:
    """Handle cfg_deny:<id> callback."""
    user_id = callback.from_user.id
    pending = adapter._pending_config_proposals.get(proposal_id)

    if not pending:
        await callback.answer("Bu so'rov muddati tugagan yoki topilmadi.", show_alert=True)
        return

    if _now() > pending.get("expires_at", 0):
        adapter._pending_config_proposals.pop(proposal_id, None)
        _append_audit(config.workspace_dir, str(user_id), "propose_expired", {
            "proposal_id": proposal_id,
            "field": pending.get("field", ""),
        })
        await callback.answer("Muddati tugagan.", show_alert=True)
        try:
            await callback.message.edit_text(
                f"{callback.message.text}\n\n⏰ Muddati tugadi",
            )
        except Exception:
            pass
        return

    if pending["user_id"] != user_id:
        await callback.answer(
            "Faqat so'rov egasi ruxsat berishi mumkin.", show_alert=True,
        )
        return

    adapter._pending_config_proposals.pop(proposal_id, None)
    _append_audit(config.workspace_dir, str(user_id), "denied", {
        "proposal_id": proposal_id,
        "field": pending["field"],
        "value_hash": pending.get("value_hash", ""),
    })
    try:
        await callback.message.edit_text(
            f"{callback.message.text}\n\n❌ Rad etildi",
        )
    except Exception:
        pass
    await callback.answer("❌ Rad etildi")
