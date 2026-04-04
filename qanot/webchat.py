"""WebChat adapter — embeddable chat widget via WebSocket.

Adds WebSocket endpoint /ws/chat and widget page /webchat to the dashboard.
Streams agent responses in real-time using the same run_turn_stream interface
as the Telegram adapter.

Config:
    "webchat_enabled": true,
    "webchat_token": "optional-auth-token",
    "webchat_max_sessions": 50
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)

SESSION_TTL = 3600  # 1 hour idle timeout


@dataclass
class WebChatSession:
    """Active WebSocket chat session."""

    session_id: str
    user_id: str
    ws: web.WebSocketResponse
    last_active: float = field(default_factory=time.time)


class WebChatAdapter:
    """WebSocket-based chat adapter for web embedding."""

    def __init__(self, config: "Config", agent: "Agent"):
        self.config = config
        self.agent = agent
        self._sessions: dict[str, WebChatSession] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}

    def register_routes(self, app: web.Application) -> None:
        """Add WebChat routes to an existing aiohttp app."""
        app.router.add_get("/ws/chat", self._handle_ws)
        app.router.add_get("/webchat", self._handle_widget_page)
        logger.info("WebChat routes registered: /ws/chat (WebSocket), /webchat (widget)")

    async def _handle_widget_page(self, request: web.Request) -> web.Response:
        """Serve the embeddable chat widget HTML."""
        from qanot.webchat_widget import WEBCHAT_WIDGET_HTML

        html = WEBCHAT_WIDGET_HTML
        return web.Response(text=html, content_type="text/html")

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connection lifecycle."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        # Auth check
        token = request.rel_url.query.get("token", "")
        if self.config.webchat_token and token != self.config.webchat_token:
            await ws.send_json({"type": "error", "message": "Invalid token"})
            await ws.close()
            return ws

        # Session limit
        self._evict_stale()
        if len(self._sessions) >= self.config.webchat_max_sessions:
            await ws.send_json({"type": "error", "message": "Too many active sessions"})
            await ws.close()
            return ws

        # Create session
        session_id = request.rel_url.query.get("session_id", uuid.uuid4().hex[:12])
        user_id = f"webchat_{session_id}"
        session = WebChatSession(session_id=session_id, user_id=user_id, ws=ws)
        self._sessions[session_id] = session

        await ws.send_json({"type": "connected", "session_id": session_id})
        logger.info("WebChat session connected: %s", session_id)

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})
                        continue

                    msg_type = data.get("type", "message")

                    if msg_type == "ping":
                        await ws.send_json({"type": "pong"})
                    elif msg_type == "message":
                        session.last_active = time.time()
                        text = data.get("text", "").strip()
                        if text:
                            await self._process_message(session, text)
                    elif msg_type == "reset":
                        self.agent.reset(user_id)
                        await ws.send_json({"type": "reset", "message": "Conversation cleared"})

                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        except Exception as e:
            logger.warning("WebChat session %s error: %s", session_id, e)
        finally:
            self._sessions.pop(session_id, None)
            logger.info("WebChat session disconnected: %s", session_id)

        return ws

    async def _process_message(self, session: WebChatSession, text: str) -> None:
        """Process a user message and stream response over WebSocket."""
        lock = self._user_locks.setdefault(session.user_id, asyncio.Lock())

        async with lock:
            try:
                async for event in self.agent.run_turn_stream(
                    text,
                    user_id=session.user_id,
                    chat_id=None,
                ):
                    if session.ws.closed:
                        return

                    if event.type == "text_delta":
                        await session.ws.send_json({
                            "type": "text_delta",
                            "text": event.text,
                        })
                    elif event.type == "tool_use":
                        tool_name = event.tool_call.name if event.tool_call else ""
                        await session.ws.send_json({
                            "type": "tool_use",
                            "tool_name": tool_name,
                        })
                    elif event.type == "done":
                        final = event.response.content if event.response else ""
                        await session.ws.send_json({
                            "type": "done",
                            "text": final,
                        })

            except Exception as e:
                logger.error("WebChat process error for %s: %s", session.session_id, e)
                if not session.ws.closed:
                    await session.ws.send_json({
                        "type": "error",
                        "message": str(e),
                    })

    def _evict_stale(self) -> None:
        """Remove sessions idle for more than SESSION_TTL."""
        now = time.time()
        stale = [
            sid for sid, s in self._sessions.items()
            if now - s.last_active > SESSION_TTL
        ]
        for sid in stale:
            session = self._sessions.pop(sid, None)
            if session and not session.ws.closed:
                task = asyncio.create_task(session.ws.close())
                task.add_done_callback(
                    lambda t: logger.debug("WebSocket close failed: %s", t.exception())
                    if not t.cancelled() and t.exception() else None
                )
            logger.debug("Evicted stale WebChat session: %s", sid)
