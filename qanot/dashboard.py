"""Web dashboard — real-time bot monitoring and management.

Serves a web UI + JSON API on configurable port (default: 8765).
Uses aiohttp (already a dependency) — no extra packages needed.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)

DASHBOARD_PORT = 8765


class Dashboard:
    """Lightweight web dashboard for Qanot AI."""

    def __init__(self, config: "Config", agent: "Agent"):
        self.config = config
        self.agent = agent
        self.app = web.Application()
        self._setup_routes()
        self._start_time = time.time()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/api/status", self._handle_api_status)
        self.app.router.add_get("/api/config", self._handle_api_config)
        self.app.router.add_get("/api/costs", self._handle_api_costs)
        self.app.router.add_get("/api/memory", self._handle_api_memory)
        self.app.router.add_get("/api/memory/{filename}", self._handle_api_memory_file)
        self.app.router.add_get("/api/tools", self._handle_api_tools)
        self.app.router.add_get("/api/routing", self._handle_api_routing)

    # ── API endpoints ──

    async def _handle_api_status(self, request: web.Request) -> web.Response:
        status = self.agent.context.session_status()
        uptime = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        data = {
            "bot_name": self.config.bot_name,
            "model": self.config.model,
            "provider": self.config.provider,
            "uptime": f"{hours}h {minutes}m {seconds}s",
            "uptime_seconds": uptime,
            "context_percent": round(status["context_percent"], 1),
            "total_tokens": status["total_tokens"],
            "turn_count": status["turn_count"],
            "api_calls": status["api_calls"],
            "buffer_active": status["buffer_active"],
            "active_conversations": self.agent._conv_manager.active_count(),
        }
        return web.json_response(data)

    async def _handle_api_config(self, request: web.Request) -> web.Response:
        data = {
            "provider": self.config.provider,
            "model": self.config.model,
            "response_mode": self.config.response_mode,
            "voice_mode": self.config.voice_mode,
            "voice_provider": self.config.voice_provider,
            "rag_enabled": self.config.rag_enabled,
            "routing_enabled": self.config.routing_enabled,
            "exec_security": self.config.exec_security,
            "max_context_tokens": self.config.max_context_tokens,
            "heartbeat_enabled": self.config.heartbeat_enabled,
        }
        return web.json_response(data)

    async def _handle_api_costs(self, request: web.Request) -> web.Response:
        tracker = self.agent.cost_tracker
        return web.json_response({
            "total_cost": tracker.get_total_cost(),
            "users": tracker.get_all_stats(),
        })

    async def _handle_api_memory(self, request: web.Request) -> web.Response:
        ws = Path(self.config.workspace_dir)
        files = []

        def _entry(name: str, path: Path) -> dict:
            st = path.stat()
            return {"name": name, "size": st.st_size, "modified": st.st_mtime}

        # Workspace root files
        for f in sorted(ws.glob("*.md")):
            files.append(_entry(f.name, f))

        # Daily notes
        mem_dir = ws / "memory"
        if mem_dir.exists():
            for f in sorted(mem_dir.glob("*.md"), reverse=True):
                files.append(_entry(f"memory/{f.name}", f))

        return web.json_response({"files": files})

    async def _handle_api_memory_file(self, request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        # Security: only allow .md files, block traversal
        if ".." in filename or "/" in filename or "\\" in filename or not filename.endswith(".md"):
            return web.json_response({"error": "invalid path"}, status=400)

        ws = Path(self.config.workspace_dir).resolve()
        # Try workspace root first, then memory dir
        path = (ws / filename).resolve()
        if not path.exists():
            path = (ws / "memory" / filename).resolve()
        if not path.exists():
            return web.json_response({"error": "not found"}, status=404)

        # Verify resolved path is within workspace
        try:
            path.relative_to(ws)
        except ValueError:
            return web.json_response({"error": "invalid path"}, status=400)

        return web.json_response({"name": filename, "content": path.read_text(encoding="utf-8")})

    async def _handle_api_tools(self, request: web.Request) -> web.Response:
        tools = [
            {"name": t["name"], "description": t.get("description", "")}
            for t in self.agent.tools.get_definitions()
        ]
        return web.json_response({"tools": tools, "count": len(tools)})

    async def _handle_api_routing(self, request: web.Request) -> web.Response:
        provider = self.agent.provider
        if hasattr(provider, "status"):
            data = provider.status()
            if isinstance(data, dict):
                return web.json_response(data)
            elif isinstance(data, list):
                return web.json_response({"providers": data})
        return web.json_response({"routing": "disabled"})

    # ── Dashboard HTML ──

    async def _handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    # ── Start/Stop ──

    async def start(self, port: int = DASHBOARD_PORT, host: str = "") -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        bind = host or getattr(self.config, "dashboard_host", "127.0.0.1")
        site = web.TCPSite(runner, bind, port)
        await site.start()
        logger.info("Dashboard running at http://%s:%d", bind, port)


# ── Inline HTML Dashboard ──

from qanot.dashboard_html import DASHBOARD_HTML  # noqa: E402
