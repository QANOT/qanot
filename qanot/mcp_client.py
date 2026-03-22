"""MCP (Model Context Protocol) client — connect to external MCP servers and expose their tools.

Allows Qanot to connect to 1000+ community MCP servers (Google Drive, Slack,
Notion, Postgres, Docker, etc.) and use their tools natively in the agent loop.

Config example:
    "mcp_servers": [
        {
            "name": "filesystem",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
        },
        {
            "name": "postgres",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-postgres"],
            "env": {"DATABASE_URL": "postgresql://..."}
        }
    ]
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class MCPServerConnection:
    """A connection to a single MCP server process."""

    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = env
        self._session = None
        self._client_ctx = None
        self._session_ctx = None
        self._tools: list[dict] = []

    async def connect(self) -> bool:
        """Connect to the MCP server and discover its tools."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            logger.warning(
                "MCP server '%s' skipped — 'mcp' package not installed. "
                "Install with: pip install mcp",
                self.name,
            )
            return False

        try:
            server_params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=self.env,
            )

            self._client_ctx = stdio_client(server_params)
            read, write = await self._client_ctx.__aenter__()

            self._session_ctx = ClientSession(read, write)
            self._session = await self._session_ctx.__aenter__()

            await self._session.initialize()

            # Discover tools
            tools_result = await self._session.list_tools()
            self._tools = []
            for tool in tools_result.tools:
                self._tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                })

            logger.info(
                "MCP server '%s' connected — %d tools: %s",
                self.name,
                len(self._tools),
                ", ".join(t["name"] for t in self._tools),
            )
            return True

        except Exception as e:
            logger.error("MCP server '%s' failed to connect: %s", self.name, e)
            await self.disconnect()
            return False

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool on this MCP server."""
        if not self._session:
            return json.dumps({"error": f"MCP server '{self.name}' not connected"})

        try:
            result = await self._session.call_tool(tool_name, arguments=arguments)

            # Extract text content from result
            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
                elif hasattr(content, "data"):
                    parts.append(f"[binary data: {len(content.data)} bytes]")
                else:
                    parts.append(str(content))

            return "\n".join(parts) if parts else json.dumps({"result": "ok"})

        except Exception as e:
            logger.error("MCP tool '%s' on server '%s' failed: %s", tool_name, self.name, e)
            return json.dumps({"error": str(e)})

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        try:
            if self._session_ctx:
                await self._session_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            if self._client_ctx:
                await self._client_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        self._session = None
        self._session_ctx = None
        self._client_ctx = None

    @property
    def tools(self) -> list[dict]:
        return self._tools


class MCPManager:
    """Manages multiple MCP server connections and registers their tools."""

    def __init__(self):
        self._servers: dict[str, MCPServerConnection] = {}
        # Map tool_name → server_name for routing
        self._tool_to_server: dict[str, str] = {}

    async def connect_servers(self, server_configs: list[dict]) -> int:
        """Connect to all configured MCP servers. Returns number of successful connections."""
        connected = 0
        for cfg in server_configs:
            name = cfg.get("name", "unnamed")
            command = cfg.get("command", "")
            args = cfg.get("args", [])
            env = cfg.get("env")

            if not command:
                logger.warning("MCP server '%s' has no command — skipping", name)
                continue

            server = MCPServerConnection(name, command, args, env)
            if await server.connect():
                self._servers[name] = server
                for tool in server.tools:
                    self._tool_to_server[tool["name"]] = name
                connected += 1

        return connected

    def register_tools(self, registry) -> int:
        """Register all MCP tools into the Qanot tool registry. Returns count."""
        count = 0
        for server_name, server in self._servers.items():
            for tool in server.tools:
                tool_name = tool["name"]

                # Prefix with server name to avoid collisions with built-in tools
                # e.g., "filesystem_read_file" instead of "read_file"
                prefixed_name = f"mcp_{server_name}_{tool_name}"

                # Check for name collision with built-in tools
                if tool_name in registry.tool_names:
                    register_name = prefixed_name
                else:
                    register_name = tool_name

                # Create handler closure
                _server = server
                _tool_name = tool_name

                async def handler(params: dict, _s=_server, _t=_tool_name) -> str:
                    return await _s.call_tool(_t, params)

                registry.register(
                    name=register_name,
                    description=f"[MCP:{server_name}] {tool.get('description', '')}",
                    parameters=tool.get("input_schema", {"type": "object", "properties": {}}),
                    handler=handler,
                    category="mcp",
                )
                count += 1

        if count:
            logger.info("Registered %d MCP tools from %d servers", count, len(self._servers))
        return count

    async def disconnect_all(self) -> None:
        """Disconnect all MCP servers."""
        for name, server in self._servers.items():
            logger.info("Disconnecting MCP server '%s'", name)
            await server.disconnect()
        self._servers.clear()
        self._tool_to_server.clear()

    @property
    def connected_servers(self) -> list[str]:
        return list(self._servers.keys())

    @property
    def total_tools(self) -> int:
        return sum(len(s.tools) for s in self._servers.values())
