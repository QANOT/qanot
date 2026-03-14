"""Web dashboard — real-time bot monitoring and management.

Serves a web UI + JSON API on configurable port (default: 8765).
Uses aiohttp (already a dependency) — no extra packages needed.
"""

from __future__ import annotations

import asyncio
import json
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
            "active_conversations": len(self.agent._conversations),
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
        data = {
            "total_cost": tracker.get_total_cost(),
            "users": tracker.get_all_stats(),
        }
        return web.json_response(data)

    async def _handle_api_memory(self, request: web.Request) -> web.Response:
        ws = Path(self.config.workspace_dir)
        files = []

        # Workspace root files
        for f in sorted(ws.glob("*.md")):
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })

        # Daily notes
        mem_dir = ws / "memory"
        if mem_dir.exists():
            for f in sorted(mem_dir.glob("*.md"), reverse=True):
                files.append({
                    "name": f"memory/{f.name}",
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                })

        return web.json_response({"files": files})

    async def _handle_api_memory_file(self, request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        # Security: only allow .md files from workspace
        if ".." in filename or "/" in filename:
            return web.json_response({"error": "invalid path"}, status=400)

        ws = Path(self.config.workspace_dir)
        # Try workspace root first, then memory dir
        path = ws / filename
        if not path.exists():
            path = ws / "memory" / filename
        if not path.exists() or not path.suffix == ".md":
            return web.json_response({"error": "not found"}, status=404)

        content = path.read_text(encoding="utf-8")
        return web.json_response({"name": filename, "content": content})

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

    async def start(self, port: int = DASHBOARD_PORT) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Dashboard running at http://localhost:%d", port)


# ── Inline HTML Dashboard ──

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Qanot AI Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e4e4e7; }
  .header { background: linear-gradient(135deg, #1e293b, #0f172a); padding: 20px 30px; border-bottom: 1px solid #1e293b; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 24px; } .header h1 span { color: #60a5fa; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; background: #22c55e; display: inline-block; margin-right: 8px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; padding: 20px; }
  .card { background: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; }
  .card h2 { font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
  .stat { font-size: 32px; font-weight: 700; color: #f8fafc; }
  .stat-label { font-size: 13px; color: #64748b; margin-top: 4px; }
  .stat-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #334155; }
  .stat-row:last-child { border: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
  .badge-green { background: #065f46; color: #6ee7b7; }
  .badge-blue { background: #1e3a5f; color: #93c5fd; }
  .badge-yellow { background: #713f12; color: #fde047; }
  .tools-list { max-height: 300px; overflow-y: auto; }
  .tool-item { padding: 6px 0; border-bottom: 1px solid #1e293b; font-size: 13px; }
  .tool-name { color: #60a5fa; font-weight: 600; }
  .files-list { max-height: 300px; overflow-y: auto; }
  .file-item { padding: 8px 0; border-bottom: 1px solid #1e293b; display: flex; justify-content: space-between; }
  .file-name { color: #a5b4fc; cursor: pointer; } .file-name:hover { text-decoration: underline; }
  .file-size { color: #64748b; font-size: 12px; }
  #file-content { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 16px; white-space: pre-wrap; font-family: monospace; font-size: 13px; max-height: 400px; overflow-y: auto; display: none; margin-top: 12px; }
  .refresh { color: #64748b; font-size: 12px; }
</style>
</head>
<body>
<div class="header">
  <h1><span class="status-dot"></span>🪶 <span>Qanot AI</span> Dashboard</h1>
  <div class="refresh" id="refresh-time">Loading...</div>
</div>

<div class="grid">
  <div class="card">
    <h2>Status</h2>
    <div class="stat" id="s-model">—</div>
    <div class="stat-label" id="s-provider">—</div>
    <div style="margin-top:16px">
      <div class="stat-row"><span>Uptime</span><span id="s-uptime">—</span></div>
      <div class="stat-row"><span>Context</span><span id="s-context">—</span></div>
      <div class="stat-row"><span>Turns</span><span id="s-turns">—</span></div>
      <div class="stat-row"><span>API Calls</span><span id="s-api">—</span></div>
      <div class="stat-row"><span>Users</span><span id="s-users">—</span></div>
    </div>
  </div>

  <div class="card">
    <h2>Costs</h2>
    <div class="stat" id="c-total">$0.00</div>
    <div class="stat-label">Total spent</div>
    <div id="c-users" style="margin-top:16px"></div>
  </div>

  <div class="card">
    <h2>Routing</h2>
    <div id="r-info"></div>
  </div>

  <div class="card">
    <h2>Config</h2>
    <div id="cfg-info"></div>
  </div>

  <div class="card">
    <h2>Tools (<span id="t-count">0</span>)</h2>
    <div class="tools-list" id="t-list"></div>
  </div>

  <div class="card">
    <h2>Memory Files</h2>
    <div class="files-list" id="m-files"></div>
    <pre id="file-content"></pre>
  </div>
</div>

<script>
async function load() {
  try {
    const [status, costs, routing, config, tools, memory] = await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/costs').then(r=>r.json()),
      fetch('/api/routing').then(r=>r.json()),
      fetch('/api/config').then(r=>r.json()),
      fetch('/api/tools').then(r=>r.json()),
      fetch('/api/memory').then(r=>r.json()),
    ]);

    // Status
    document.getElementById('s-model').textContent = status.model;
    document.getElementById('s-provider').textContent = status.provider + ' | ' + status.bot_name;
    document.getElementById('s-uptime').textContent = status.uptime;
    document.getElementById('s-context').textContent = status.context_percent + '%';
    document.getElementById('s-turns').textContent = status.turn_count;
    document.getElementById('s-api').textContent = status.api_calls;
    document.getElementById('s-users').textContent = status.active_conversations;

    // Costs
    document.getElementById('c-total').textContent = '$' + costs.total_cost.toFixed(2);
    const cusers = document.getElementById('c-users');
    cusers.innerHTML = '';
    for (const [uid, data] of Object.entries(costs.users || {})) {
      cusers.innerHTML += '<div class="stat-row"><span>User ' + uid.slice(-4) + '</span><span>$' + (data.total_cost||0).toFixed(3) + ' (' + (data.turns||0) + ' turns)</span></div>';
    }

    // Routing
    const rinfo = document.getElementById('r-info');
    if (routing.stats) {
      const s = routing.stats;
      rinfo.innerHTML = '<div class="stat-row"><span>Cheap</span><span class="badge badge-green">' + routing.cheap_model + '</span></div>' +
        '<div class="stat-row"><span>Primary</span><span class="badge badge-blue">' + routing.primary_model + '</span></div>' +
        '<div class="stat-row"><span>Savings</span><span class="badge badge-yellow">' + s.savings_pct + '%</span></div>' +
        '<div class="stat-row"><span>Total</span><span>' + s.total + ' (' + s.routed_cheap + ' cheap)</span></div>';
    } else {
      rinfo.innerHTML = '<div class="stat-label">Routing disabled</div>';
    }

    // Config
    const cinfo = document.getElementById('cfg-info');
    cinfo.innerHTML = '';
    for (const [k, v] of Object.entries(config)) {
      cinfo.innerHTML += '<div class="stat-row"><span>' + k + '</span><span>' + v + '</span></div>';
    }

    // Tools
    document.getElementById('t-count').textContent = tools.count;
    const tlist = document.getElementById('t-list');
    tlist.innerHTML = tools.tools.map(t => '<div class="tool-item"><span class="tool-name">' + t.name + '</span> — ' + (t.description||'').slice(0,60) + '</div>').join('');

    // Memory
    const mfiles = document.getElementById('m-files');
    mfiles.innerHTML = memory.files.map(f => '<div class="file-item"><span class="file-name" onclick="viewFile(&quot;' + f.name + '&quot;)">' + f.name + '</span><span class="file-size">' + (f.size/1024).toFixed(1) + ' KB</span></div>').join('');

    document.getElementById('refresh-time').textContent = 'Updated: ' + new Date().toLocaleTimeString();
  } catch(e) { console.error(e); }
}

async function viewFile(name) {
  const el = document.getElementById('file-content');
  try {
    const fname = name.includes('/') ? name.split('/').pop() : name;
    const r = await fetch('/api/memory/' + fname);
    const d = await r.json();
    el.textContent = d.content || d.error || 'Empty';
    el.style.display = 'block';
  } catch(e) { el.textContent = 'Error loading file'; el.style.display = 'block'; }
}

load();
setInterval(load, 10000);
</script>
</body>
</html>"""
