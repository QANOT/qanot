"""Webhook endpoint — receive external events and trigger agent actions.

Adds POST /api/webhook to the dashboard's aiohttp server.
Supports two modes:
  - "notify": Format event as text, deliver to owner via proactive loop
  - "agent": Run agent turn with event payload, return response

Auth: Bearer token in Authorization header.

Example:
    curl -X POST http://localhost:8765/api/webhook \
      -H "Authorization: Bearer YOUR_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"mode": "notify", "source": "github", "event": "push", "payload": {"ref": "main"}}'
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config
    from qanot.scheduler import CronScheduler

logger = logging.getLogger(__name__)


class WebhookHandler:
    """Handles incoming webhook events from external services."""

    def __init__(self, config: "Config", agent: "Agent", scheduler: "CronScheduler"):
        self.config = config
        self.agent = agent
        self.scheduler = scheduler

    def register_routes(self, app: web.Application) -> None:
        """Add webhook routes to an existing aiohttp app."""
        app.router.add_post("/api/webhook", self._handle_webhook)
        logger.info("Webhook endpoint registered: POST /api/webhook")

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming webhook POST request."""
        # Auth check
        if self.config.webhook_token:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return web.json_response({"error": "Missing Authorization header"}, status=401)
            token = auth[7:]
            if not hmac.compare_digest(token, self.config.webhook_token):
                return web.json_response({"error": "Invalid token"}, status=403)

        # Parse body
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        mode = body.get("mode", "notify")
        source = body.get("source", "webhook")
        event = body.get("event", "")
        payload = body.get("payload", {})

        if mode == "notify":
            return await self._handle_notify(source, event, payload)
        elif mode == "agent":
            return await self._handle_agent(source, event, payload)
        else:
            return web.json_response({"error": f"Unknown mode: {mode}. Use 'notify' or 'agent'."}, status=400)

    async def _handle_notify(self, source: str, event: str, payload: dict) -> web.Response:
        """Push event as proactive message to owner."""
        text = self._format_event(source, event, payload)

        await self.scheduler.message_queue.put({
            "type": "proactive",
            "text": text,
            "source": f"webhook:{source}",
        })

        logger.info("Webhook notify: %s/%s → proactive queue", source, event)
        return web.json_response({"status": "ok", "mode": "notify"})

    async def _handle_agent(self, source: str, event: str, payload: dict) -> web.Response:
        """Run agent turn with webhook payload and return response."""
        text = self._format_event(source, event, payload)

        try:
            response = await self.agent.run_turn(
                text,
                user_id=f"webhook:{source}",
            )
            logger.info("Webhook agent: %s/%s → %d chars response", source, event, len(response or ""))
            return web.json_response({"status": "ok", "mode": "agent", "response": response})
        except Exception as e:
            logger.error("Webhook agent turn failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    @staticmethod
    def _format_event(source: str, event: str, payload: dict) -> str:
        """Format webhook event into human-readable text for the agent."""
        lines = [f"[Webhook: {source}]"]
        if event:
            lines[0] += f" Event: {event}"

        # Extract common fields
        for key in ("message", "text", "description", "subject", "title", "name"):
            if key in payload:
                lines.append(f"{key}: {payload[key]}")

        # Include full payload if small, truncated summary if large
        payload_str = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(payload_str) > 2000:
            payload_str = payload_str[:2000] + "\n... [truncated]"
        lines.append(f"\nPayload:\n{payload_str}")

        return "\n".join(lines)
