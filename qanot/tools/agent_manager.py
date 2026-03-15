"""Dynamic agent management — create, update, delete agents at runtime.

No limits. Users can create as many agents as they want, each with its own
Telegram bot token, model, personality, tools. Like OpenClaw but simpler.

Agents are persisted to config.json and hot-launched without restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from qanot.agent import ToolRegistry
from qanot.config import AgentDefinition

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.providers.base import LLMProvider
    from qanot.agent_bot import AgentBot

logger = logging.getLogger(__name__)

# Runtime registry of active agent bots (for hot-launch/stop)
_active_agent_bots: dict[str, "AgentBot"] = {}  # agent_id → AgentBot


_RE_AGENT_ID_INVALID = re.compile(r"[^a-z0-9\-]")
_RE_AGENT_ID_DASHES = re.compile(r"-+")


def _sanitize_agent_id(raw: str) -> str:
    """Sanitize agent ID to safe format: lowercase, alphanumeric + hyphens."""
    cleaned = _RE_AGENT_ID_INVALID.sub("-", raw.lower().strip())
    cleaned = _RE_AGENT_ID_DASHES.sub("-", cleaned).strip("-")
    return cleaned[:32] or "agent"


# Fields serialized only when truthy
_TRUTHY_FIELDS: tuple[str, ...] = (
    "name", "prompt", "model", "provider", "api_key",
    "bot_token", "tools_allow", "tools_deny", "delegate_allow",
)
# Fields serialized only when they differ from their default value
_DEFAULT_FIELDS: dict[str, int] = {"max_iterations": 15, "timeout": 120}


def _save_agents_to_config(config: "Config") -> None:
    """Persist current agents list to config.json."""
    config_path = os.environ.get("QANOT_CONFIG", "/data/config.json")
    p = Path(config_path)
    if not p.exists():
        logger.warning("Config file not found at %s, cannot persist agents", config_path)
        return

    raw = json.loads(p.read_text(encoding="utf-8"))

    # Serialize agents

    agents_data = []
    for ad in config.agents:
        agent_dict = {"id": ad.id}
        for field in _TRUTHY_FIELDS:
            value = getattr(ad, field, None)
            if value:
                agent_dict[field] = value
        for field, default in _DEFAULT_FIELDS.items():
            value = getattr(ad, field, default)
            if value != default:
                agent_dict[field] = value
        agents_data.append(agent_dict)

    raw["agents"] = agents_data
    p.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved %d agents to config.json", len(agents_data))


async def _hot_launch_agent_bot(
    agent_def: AgentDefinition,
    config: "Config",
    provider: "LLMProvider",
    parent_registry: ToolRegistry,
) -> None:
    """Hot-launch a new agent bot without restart."""
    if not agent_def.bot_token:
        return

    from qanot.agent_bot import AgentBot

    # Stop existing bot if running
    await _stop_agent_bot(agent_def.id)

    agent_bot = AgentBot(
        agent_def=agent_def,
        config=config,
        provider=provider,
        parent_registry=parent_registry,
    )
    _active_agent_bots[agent_def.id] = agent_bot

    asyncio.create_task(
        agent_bot.start(),
        name=f"agent_bot_{agent_def.id}",
    )
    logger.info("Hot-launched agent bot: %s (%s)", agent_def.id, agent_def.name or agent_def.id)


async def _stop_agent_bot(agent_id: str) -> bool:
    """Stop a running agent bot. Returns True if was running."""
    if bot := _active_agent_bots.get(agent_id):
        try:
            await bot.stop()
        except Exception as e:
            logger.warning("Error stopping agent bot '%s': %s", agent_id, e)
        _active_agent_bots.pop(agent_id, None)
        return True
    return False


def register_agent_manager_tools(
    registry: ToolRegistry,
    config: "Config",
    provider: "LLMProvider",
    parent_registry: ToolRegistry,
    *,
    get_user_id: callable,
) -> None:
    """Register dynamic agent creation/management tools."""

    # ── create_agent ──

    async def create_agent(params: dict) -> str:
        """Create a new agent dynamically."""
        agent_id = _sanitize_agent_id(params.get("id", ""))
        if not agent_id:
            return json.dumps({"error": "id is required"})

        # Check if agent already exists
        for existing in config.agents:
            if existing.id == agent_id:
                return json.dumps({
                    "error": f"Agent '{agent_id}' already exists. Use update_agent to modify.",
                })

        name = params.get("name", "").strip() or agent_id
        prompt = params.get("prompt", "").strip()
        model = params.get("model", "").strip()
        agent_provider = params.get("provider", "").strip()
        bot_token = params.get("bot_token", "").strip()
        tools_allow = params.get("tools_allow", [])
        tools_deny = params.get("tools_deny", [])
        timeout = params.get("timeout", 120)

        if not prompt:
            prompt = f"You are {name}. You are a helpful AI assistant. Complete any task assigned to you thoroughly and professionally."

        agent_def = AgentDefinition(
            id=agent_id,
            name=name,
            prompt=prompt,
            model=model,
            provider=agent_provider,
            bot_token=bot_token,
            tools_allow=tools_allow if isinstance(tools_allow, list) else [],
            tools_deny=tools_deny if isinstance(tools_deny, list) else [],
            timeout=timeout,
        )

        # Add to runtime config
        config.agents.append(agent_def)

        # Persist to config.json
        _save_agents_to_config(config)

        # Create per-agent SOUL.md if workspace exists
        soul_dir = Path(config.workspace_dir) / "agents" / agent_id
        soul_dir.mkdir(parents=True, exist_ok=True)
        soul_path = soul_dir / "SOUL.md"
        if not soul_path.exists():
            soul_path.write_text(prompt, encoding="utf-8")
            logger.info("Created agent identity: %s", soul_path)

        # Re-register delegate tools to include new agent in enum
        from qanot.tools.delegate import register_delegate_tools
        # Re-register so the updated agent list is reflected in the tool schema
        # (tools with same name get overwritten in the registry)
        register_delegate_tools(
            registry, config, provider, parent_registry,
            get_user_id=get_user_id,
        )

        # Hot-launch if bot_token provided
        if bot_token:
            await _hot_launch_agent_bot(agent_def, config, provider, parent_registry)

        result = {
            "status": "created",
            "agent_id": agent_id,
            "name": name,
            "model": model or config.model,
            "has_bot": bool(bot_token),
        }

        if bot_token:
            result["message"] = f"Agent '{name}' created and Telegram bot launched! Users can now chat with it."
        else:
            result["message"] = f"Agent '{name}' created as internal agent. Use delegate_to_agent to interact with it."

        logger.info("Agent created: %s (%s), bot=%s", agent_id, name, bool(bot_token))
        return json.dumps(result)

    # ── update_agent ──

    async def update_agent(params: dict) -> str:
        """Update an existing agent's configuration."""
        agent_id = params.get("id", "").strip()
        if not agent_id:
            return json.dumps({"error": "id is required"})

        # Find existing agent
        target = next((ad for ad in config.agents if ad.id == agent_id), None)
        if target is None:
            return json.dumps({"error": f"Agent '{agent_id}' not found."})

        changes = []

        if "name" in params and params["name"]:
            target.name = params["name"].strip()
            changes.append("name")
        if "prompt" in params and params["prompt"]:
            target.prompt = params["prompt"].strip()
            changes.append("prompt")
            # Also update SOUL.md
            soul_path = Path(config.workspace_dir) / "agents" / agent_id / "SOUL.md"
            soul_path.parent.mkdir(parents=True, exist_ok=True)
            soul_path.write_text(target.prompt, encoding="utf-8")
        if "model" in params:
            target.model = params["model"].strip()
            changes.append("model")
        if "provider" in params:
            target.provider = params["provider"].strip()
            changes.append("provider")
        if "bot_token" in params:
            old_token = target.bot_token
            target.bot_token = params["bot_token"].strip()
            changes.append("bot_token")

            # Handle bot token changes
            if old_token and not target.bot_token:
                # Token removed — stop bot
                await _stop_agent_bot(agent_id)
            elif target.bot_token and target.bot_token != old_token:
                # New token — launch new bot
                await _hot_launch_agent_bot(target, config, provider, parent_registry)

        if "tools_allow" in params:
            target.tools_allow = params["tools_allow"] if isinstance(params["tools_allow"], list) else []
            changes.append("tools_allow")
        if "tools_deny" in params:
            target.tools_deny = params["tools_deny"] if isinstance(params["tools_deny"], list) else []
            changes.append("tools_deny")
        if "timeout" in params:
            target.timeout = params["timeout"]
            changes.append("timeout")

        if not changes:
            return json.dumps({"error": "No changes provided."})

        # Persist
        _save_agents_to_config(config)

        # Re-register delegate tools
        from qanot.tools.delegate import register_delegate_tools
        register_delegate_tools(
            registry, config, provider, parent_registry,
            get_user_id=get_user_id,
        )

        logger.info("Agent updated: %s (changed: %s)", agent_id, ", ".join(changes))
        return json.dumps({
            "status": "updated",
            "agent_id": agent_id,
            "changes": changes,
        })

    # ── delete_agent ──

    async def delete_agent(params: dict) -> str:
        """Delete an agent."""
        agent_id = params.get("id", "").strip()
        if not agent_id:
            return json.dumps({"error": "id is required"})

        # Find and remove
        target = next((ad for ad in config.agents if ad.id == agent_id), None)
        if target is None:
            return json.dumps({"error": f"Agent '{agent_id}' not found."})
        config.agents.remove(target)

        # Stop bot if running
        was_running = await _stop_agent_bot(agent_id)

        # Persist
        _save_agents_to_config(config)

        # Re-register delegate tools
        from qanot.tools.delegate import register_delegate_tools
        register_delegate_tools(
            registry, config, provider, parent_registry,
            get_user_id=get_user_id,
        )

        logger.info("Agent deleted: %s (was running: %s)", agent_id, was_running)
        return json.dumps({
            "status": "deleted",
            "agent_id": agent_id,
            "bot_stopped": was_running,
        })

    # ── restart_self ──

    async def restart_self(params: dict) -> str:
        """Restart the entire bot process (self-healing)."""
        import signal
        reason = params.get("reason", "manual restart")
        logger.info("Self-restart requested: %s", reason)

        # Schedule graceful exit after short delay so the response gets sent first.
        # sys.exit allows cleanup (finally blocks, atexit, session flush).
        # systemd/launchd Restart=always will bring us back automatically.
        async def _do_restart():
            await asyncio.sleep(2)
            logger.info("Graceful exit for restart (daemon will respawn)...")
            # SIGTERM triggers graceful shutdown in main.py
            import os
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_do_restart())
        return json.dumps({
            "status": "restarting",
            "reason": reason,
            "message": "Bot 2 soniyadan keyin qayta ishga tushadi.",
        })

    # ── Register tools ──

    registry.register(
        name="create_agent",
        description=(
            "Yangi agent yaratish — o'z Telegram boti, modeli, shaxsiyati bilan. "
            "Cheklov yo'q — xohlagancha agent yarating. "
            "bot_token bersangiz, alohida Telegram bot sifatida ishlaydi. "
            "Bermasangiz, ichki agent sifatida delegate_to_agent orqali ishlaydi."
        ),
        parameters={
            "type": "object",
            "required": ["id", "name"],
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Agent ID (lotin harflar, raqamlar, tire). Masalan: 'seo-expert', 'marketing-bot'.",
                },
                "name": {
                    "type": "string",
                    "description": "Agent nomi. Masalan: 'SEO Mutaxassis', 'Marketing Boshqaruvchi'.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Agent shaxsiyati va vazifasi. Bu agent kimligini belgilaydi.",
                },
                "model": {
                    "type": "string",
                    "description": "LLM model (bo'sh = asosiy model). Masalan: 'claude-opus-4-6', 'claude-haiku-4-5'.",
                },
                "provider": {
                    "type": "string",
                    "description": "LLM provider (bo'sh = asosiy). Masalan: 'anthropic', 'openai'.",
                },
                "bot_token": {
                    "type": "string",
                    "description": "Telegram bot token (@BotFather dan olinadi). Bo'sh = ichki agent.",
                },
                "tools_allow": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Faqat ruxsat berilgan toollar (bo'sh = hammasi).",
                },
                "tools_deny": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Taqiqlangan toollar.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout soniyalarda (default: 120).",
                },
            },
        },
        handler=create_agent,
        category="agent",
    )

    registry.register(
        name="update_agent",
        description=(
            "Mavjud agentni yangilash — nomi, prompti, modeli, bot tokeni va boshqalarni o'zgartirish. "
            "Faqat o'zgartirmoqchi bo'lgan maydonlarni bering."
        ),
        parameters={
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Yangilanadigan agent ID.",
                },
                "name": {"type": "string"},
                "prompt": {"type": "string"},
                "model": {"type": "string"},
                "provider": {"type": "string"},
                "bot_token": {"type": "string"},
                "tools_allow": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "tools_deny": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "timeout": {"type": "integer"},
            },
        },
        handler=update_agent,
        category="agent",
    )

    registry.register(
        name="delete_agent",
        description="Agentni o'chirish. Agar Telegram boti bo'lsa, to'xtatiladi.",
        parameters={
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {
                    "type": "string",
                    "description": "O'chiriladigan agent ID.",
                },
            },
        },
        handler=delete_agent,
        category="agent",
    )

    registry.register(
        name="restart_self",
        description=(
            "Botni qayta ishga tushirish (self-restart). "
            "Yangi agentlar, konfiguratsiya o'zgarishlari yoki xatoliklardan keyin ishlatiladi. "
            "Bot 2 soniyada qayta boshlanadi — systemd avtomatik qayta tiklaydi."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Qayta ishga tushirish sababi.",
                },
            },
        },
        handler=restart_self,
        category="agent",
    )
