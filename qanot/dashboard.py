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

DASHBOARD_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
    '<title>Qanot AI Dashboard</title>'
    '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">'
    '<style>'
    ':root{--bg:#080b12;--surface:#111827;--surface2:#1a2332;--border:#1e2d3d;--text:#e2e8f0;--muted:#64748b;--accent:#38bdf8;--accent2:#818cf8;--green:#34d399;--yellow:#fbbf24;--red:#f87171;}'
    '*{margin:0;padding:0;box-sizing:border-box}'
    'body{font-family:"DM Sans",sans-serif;background:var(--bg);color:var(--text);min-height:100vh}'
    '.noise{position:fixed;inset:0;opacity:.03;pointer-events:none;background:url("data:image/svg+xml,%3Csvg viewBox=\'0 0 256 256\' xmlns=\'http://www.w3.org/2000/svg\'%3E%3Cfilter id=\'n\'%3E%3CfeTurbulence type=\'fractalNoise\' baseFrequency=\'.8\' numOctaves=\'4\' stitchTiles=\'stitch\'/%3E%3C/filter%3E%3Crect width=\'100%25\' height=\'100%25\' filter=\'url(%23n)\'/%3E%3C/svg%3E")}'
    '.header{background:linear-gradient(135deg,#0c1220,#111827);padding:24px 32px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;position:relative;overflow:hidden}'
    '.header::before{content:"";position:absolute;top:-50%;right:-10%;width:400px;height:400px;background:radial-gradient(circle,rgba(56,189,248,.06),transparent 70%);pointer-events:none}'
    '.logo{display:flex;align-items:center;gap:12px}'
    '.logo-icon{width:40px;height:40px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;box-shadow:0 0 20px rgba(56,189,248,.2)}'
    '.logo h1{font-size:20px;font-weight:700;letter-spacing:-.5px}'
    '.logo h1 span{background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}'
    '.live{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}'
    '.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}'
    '@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}'
    '.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;padding:24px}'
    '.card-wide{grid-column:1/-1}'
    '.memory-layout{display:grid;grid-template-columns:280px 1fr;gap:16px;min-height:400px}'
    '@media(max-width:768px){.memory-layout{grid-template-columns:1fr}}'
    '.card{background:var(--surface);border-radius:16px;padding:24px;border:1px solid var(--border);transition:border-color .2s}'
    '.card:hover{border-color:rgba(56,189,248,.2)}'
    '.card-title{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:16px;display:flex;align-items:center;gap:8px}'
    '.card-title i{font-style:normal}'
    '.big-stat{font-size:36px;font-weight:700;letter-spacing:-1px;background:linear-gradient(135deg,var(--text),var(--muted));-webkit-background-clip:text;-webkit-text-fill-color:transparent}'
    '.sub{font-size:12px;color:var(--muted);margin-top:4px}'
    '.row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border)}'
    '.row:last-child{border:none}'
    '.row-label{font-size:13px;color:var(--muted)}'
    '.row-value{font-size:13px;font-weight:500;font-family:"JetBrains Mono",monospace}'
    '.pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;font-family:"JetBrains Mono",monospace}'
    '.pill-green{background:rgba(52,211,153,.1);color:var(--green);border:1px solid rgba(52,211,153,.2)}'
    '.pill-blue{background:rgba(56,189,248,.1);color:var(--accent);border:1px solid rgba(56,189,248,.2)}'
    '.pill-purple{background:rgba(129,140,248,.1);color:var(--accent2);border:1px solid rgba(129,140,248,.2)}'
    '.pill-yellow{background:rgba(251,191,36,.1);color:var(--yellow);border:1px solid rgba(251,191,36,.2)}'
    '.scroll-list{max-height:280px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--border) transparent}'
    '.tool-item{padding:8px 0;border-bottom:1px solid var(--border);font-size:12px}'
    '.tool-item:last-child{border:none}'
    '.tool-name{color:var(--accent);font-weight:600;font-family:"JetBrains Mono",monospace;font-size:12px}'
    '.file-item{padding:10px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;cursor:pointer;transition:background .15s;margin:0 -8px;padding-left:8px;padding-right:8px;border-radius:6px}'
    '.file-item:hover{background:var(--surface2)}'
    '.file-name{color:var(--accent2);font-size:13px;font-weight:500}'
    '.file-size{color:var(--muted);font-size:11px;font-family:"JetBrains Mono",monospace}'
    '#file-viewer{background:var(--bg);border:1px solid var(--border);border-radius:12px;padding:20px;white-space:pre-wrap;font-family:"JetBrains Mono",monospace;font-size:13px;line-height:1.7;overflow-y:auto;color:var(--text);min-height:300px;max-height:500px}'
    '#file-viewer-name{font-size:13px;font-weight:600;color:var(--accent);margin-bottom:8px;font-family:"JetBrains Mono",monospace}'
    '.file-item.active{background:var(--surface2)}'
    '.ctx-bar{height:6px;background:var(--surface2);border-radius:3px;margin-top:12px;overflow:hidden}'
    '.ctx-fill{height:100%;border-radius:3px;transition:width .5s ease}'
    '</style></head><body><div class="noise"></div>'
    '<div class="header">'
    '<div class="logo"><div class="logo-icon">&#x1fab6;</div><h1><span>Qanot AI</span> Dashboard</h1></div>'
    '<div class="live"><div class="live-dot"></div><span id="refresh-time">Connecting...</span></div>'
    '</div>'
    '<div class="grid">'
    '<div class="card"><div class="card-title"><i>&#x1f4ca;</i> STATUS</div>'
    '<div class="big-stat" id="s-model">—</div><div class="sub" id="s-provider">—</div>'
    '<div class="ctx-bar" style="margin-top:16px"><div class="ctx-fill" id="s-ctx-bar" style="width:0%;background:var(--green)"></div></div>'
    '<div class="sub" style="margin-top:4px">Context: <span id="s-context">0</span>%</div>'
    '<div style="margin-top:12px">'
    '<div class="row"><span class="row-label">Uptime</span><span class="row-value" id="s-uptime">—</span></div>'
    '<div class="row"><span class="row-label">Turns</span><span class="row-value" id="s-turns">0</span></div>'
    '<div class="row"><span class="row-label">API Calls</span><span class="row-value" id="s-api">0</span></div>'
    '<div class="row"><span class="row-label">Active Users</span><span class="row-value" id="s-users">0</span></div>'
    '</div></div>'
    '<div class="card"><div class="card-title"><i>&#x1f4b0;</i> COSTS</div>'
    '<div class="big-stat" id="c-total">$0.00</div><div class="sub">Total spent</div>'
    '<div id="c-users" style="margin-top:16px"></div></div>'
    '<div class="card"><div class="card-title"><i>&#x1f500;</i> ROUTING</div><div id="r-info"><div class="sub">Loading...</div></div></div>'
    '<div class="card"><div class="card-title"><i>&#x2699;&#xfe0f;</i> CONFIG</div><div id="cfg-info" class="scroll-list"></div></div>'
    '<div class="card"><div class="card-title"><i>&#x1f527;</i> TOOLS <span class="pill pill-blue" id="t-count">0</span></div><div class="scroll-list" id="t-list"></div></div>'
    '<div class="card card-wide"><div class="card-title"><i>&#x1f4c1;</i> MEMORY &amp; FILES</div>'
    '<div class="memory-layout">'
    '<div class="scroll-list" id="m-files" style="border-right:1px solid var(--border);padding-right:12px"></div>'
    '<div><div id="file-viewer-name">Select a file to view</div><pre id="file-viewer" style="display:block">Click a file on the left to view its contents here.</pre></div>'
    '</div></div>'
    '</div>'
    '<script>'
    'async function load(){'
    'try{'
    'const[s,c,r,cf,t,m]=await Promise.all(['
    'fetch("/api/status").then(r=>r.json()),'
    'fetch("/api/costs").then(r=>r.json()),'
    'fetch("/api/routing").then(r=>r.json()),'
    'fetch("/api/config").then(r=>r.json()),'
    'fetch("/api/tools").then(r=>r.json()),'
    'fetch("/api/memory").then(r=>r.json())]);'
    'document.getElementById("s-model").textContent=s.model;'
    'document.getElementById("s-provider").textContent=s.provider+" | "+s.bot_name;'
    'document.getElementById("s-uptime").textContent=s.uptime;'
    'document.getElementById("s-context").textContent=s.context_percent;'
    'var bar=document.getElementById("s-ctx-bar");bar.style.width=s.context_percent+"%";'
    'bar.style.background=s.context_percent>70?"var(--red)":s.context_percent>40?"var(--yellow)":"var(--green)";'
    'document.getElementById("s-turns").textContent=s.turn_count;'
    'document.getElementById("s-api").textContent=s.api_calls;'
    'document.getElementById("s-users").textContent=s.active_conversations;'
    'document.getElementById("c-total").textContent="$"+c.total_cost.toFixed(2);'
    'var cu=document.getElementById("c-users");cu.innerHTML="";'
    'for(var[uid,d] of Object.entries(c.users||{})){'
    'cu.innerHTML+=\'<div class="row"><span class="row-label">User \'+uid.slice(-4)+\'</span><span class="row-value pill pill-green">$\'+(d.total_cost||0).toFixed(3)+\' / \'+(d.turns||0)+\' turns</span></div>\';}'
    'var ri=document.getElementById("r-info");'
    'if(r.stats){'
    'var st=r.stats;ri.innerHTML='
    '\'<div class="row"><span class="row-label">Simple</span><span class="pill pill-green">\'+r.cheap_model+\'</span></div>\''
    '+\'<div class="row"><span class="row-label">Primary</span><span class="pill pill-purple">\'+r.primary_model+\'</span></div>\''
    '+\'<div class="row"><span class="row-label">Savings</span><span class="pill pill-yellow">\'+st.savings_pct+\'%</span></div>\''
    '+\'<div class="row"><span class="row-label">Requests</span><span class="row-value">\'+st.total+\' (\'+st.routed_cheap+\' cheap)</span></div>\';'
    '}else{ri.innerHTML=\'<div class="sub">Routing disabled</div>\';}'
    'var ci=document.getElementById("cfg-info");ci.innerHTML="";'
    'for(var[k,v] of Object.entries(cf)){'
    'ci.innerHTML+=\'<div class="row"><span class="row-label">\'+k+\'</span><span class="row-value">\'+v+\'</span></div>\';}'
    'document.getElementById("t-count").textContent=t.count;'
    'var tl=document.getElementById("t-list");'
    'tl.innerHTML=t.tools.map(function(x){return\'<div class="tool-item"><span class="tool-name">\'+x.name+\'</span> <span style="color:var(--muted)">— \'+((x.description||"").slice(0,55))+\'</span></div>\'}).join("");'
    'var mf=document.getElementById("m-files");'
    'mf.innerHTML=m.files.map(function(f){return\'<div class="file-item" onclick="viewFile(this)" data-name="\'+f.name+\'"><span class="file-name">\'+f.name+\'</span><span class="file-size">\'+(f.size/1024).toFixed(1)+\' KB</span></div>\'}).join("");'
    'document.getElementById("refresh-time").textContent="Live \u2022 "+new Date().toLocaleTimeString();'
    '}catch(e){console.error(e)}}'
    'async function viewFile(el){'
    'var name=el.getAttribute("data-name");'
    'var v=document.getElementById("file-viewer");'
    'var vn=document.getElementById("file-viewer-name");'
    'document.querySelectorAll(".file-item").forEach(function(x){x.classList.remove("active")});'
    'el.classList.add("active");'
    'vn.textContent=name;'
    'v.textContent="Loading...";'
    'try{var fn=name.indexOf("/")>=0?name.split("/").pop():name;'
    'var r=await fetch("/api/memory/"+fn);var d=await r.json();'
    'v.textContent=d.content||d.error||"Empty";'
    '}catch(e){v.textContent="Error loading file";}}'
    'load();setInterval(load,8000);'
    '</script></body></html>'
)

