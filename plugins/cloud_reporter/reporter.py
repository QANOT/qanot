"""Cloud Reporter Plugin — reports usage to Qanot Cloud platform."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import aiohttp

from qanot.plugins.base import Plugin, ToolDef, tool

logger = logging.getLogger(__name__)


@dataclass
class UsageCounter:
    """Accumulates usage metrics between flushes."""

    messages_in: int = 0
    messages_out: int = 0
    tokens: int = 0
    cost: float = 0.0

    def flush(self) -> dict:
        """Return current counts as dict and reset to zero."""
        data = {
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "tokens": self.tokens,
            "cost": round(self.cost, 6),
        }
        self.messages_in = 0
        self.messages_out = 0
        self.tokens = 0
        self.cost = 0.0
        return data


FLUSH_INTERVAL_SECONDS = 60


class CloudReporterPlugin(Plugin):
    """Reports usage metrics to Qanot Cloud platform."""

    name = "cloud_reporter"
    description = "Reports usage metrics to Qanot Cloud platform"
    version = "1.0.0"

    def __init__(self) -> None:
        self._counter = UsageCounter()
        self._bot_id: int = 0
        self._platform_url: str = ""
        self._flush_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

    def get_tools(self) -> list[ToolDef]:
        """Return list of tool definitions."""
        return self._collect_tools()

    @tool(
        name="report_usage",
        description="Report message usage to Qanot Cloud platform (called automatically)",
        parameters={
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["in", "out"],
                    "description": "Message direction",
                },
                "tokens": {
                    "type": "integer",
                    "description": "Token count",
                    "default": 0,
                },
                "cost": {
                    "type": "number",
                    "description": "Cost in USD",
                    "default": 0,
                },
            },
            "required": ["direction"],
        },
    )
    async def report_usage(self, params: dict) -> str:
        """Record a single message event."""
        direction = params.get("direction", "in")
        if direction == "in":
            self._counter.messages_in += 1
        else:
            self._counter.messages_out += 1
        self._counter.tokens += params.get("tokens", 0)
        self._counter.cost += params.get("cost", 0)
        return "Usage recorded"

    async def setup(self, config: dict) -> None:
        """Initialize reporter from environment variables."""
        self._bot_id = int(os.environ.get("QANOT_CLOUD_BOT_ID", "0"))
        self._platform_url = os.environ.get(
            "QANOT_CLOUD_PLATFORM_URL", "http://platform:8000"
        )

        if not self._bot_id:
            logger.info("QANOT_CLOUD_BOT_ID not set — cloud reporter disabled")
            return

        self._session = aiohttp.ClientSession()
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="cloud-reporter-flush"
        )
        logger.info(
            "Cloud reporter started (bot_id=%d, platform=%s)",
            self._bot_id,
            self._platform_url,
        )

    async def teardown(self) -> None:
        """Cancel flush loop, do final flush, close session."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        # Final flush before shutdown
        await self._flush_once()
        if self._session:
            await self._session.close()

    async def _flush_loop(self) -> None:
        """Periodically flush accumulated counters to the platform."""
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            await self._flush_once()

    async def _flush_once(self) -> None:
        """Send accumulated usage to the platform API."""
        if not self._bot_id or not self._session:
            return
        data = self._counter.flush()
        if data["messages_in"] == 0 and data["messages_out"] == 0:
            return
        data["bot_id"] = self._bot_id
        try:
            async with self._session.post(
                f"{self._platform_url}/api/internal/usage",
                json=data,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Cloud reporter flush failed: %d", resp.status)
        except Exception as e:
            logger.warning("Cloud reporter flush error: %s", e)
            # Re-add the counts so they are not lost
            self._counter.messages_in += data["messages_in"]
            self._counter.messages_out += data["messages_out"]
            self._counter.tokens += data["tokens"]
            self._counter.cost += data["cost"]

    # ------------------------------------------------------------------
    # Hooks — called by agent loop automatically
    # ------------------------------------------------------------------

    async def on_pre_turn(self, user_id: str, message: str) -> None:
        """Record incoming message."""
        self._counter.messages_in += 1

    async def on_post_turn(self, user_id: str, user_message: str, response: str) -> None:
        """Record outgoing message."""
        self._counter.messages_out += 1

    # ------------------------------------------------------------------
    # Public API for direct integration (not via tool calling)
    # ------------------------------------------------------------------

    def record_in(self, tokens: int = 0, cost: float = 0.0) -> None:
        """Record an incoming message."""
        self._counter.messages_in += 1
        self._counter.tokens += tokens
        self._counter.cost += cost

    def record_out(self, tokens: int = 0, cost: float = 0.0) -> None:
        """Record an outgoing message."""
        self._counter.messages_out += 1
        self._counter.tokens += tokens
        self._counter.cost += cost
