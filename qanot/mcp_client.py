"""MCP (Model Context Protocol) client — connect to external MCP servers and expose their tools.

Allows Qanot to connect to 1000+ community MCP servers (Google Drive, Slack,
Notion, Postgres, Docker, etc.) and use their tools natively in the agent loop.

Supports three transports:
    - stdio: local subprocess (command + args)
    - sse:   remote HTTPS server-sent-events URL
    - http:  remote HTTPS streamable-http URL

Config example:
    "mcp_servers": [
        {
            "name": "filesystem",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
        },
        {
            "name": "postgres",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-postgres"],
            "env": {"DATABASE_URL": "${POSTGRES_URL}"}
        },
        {
            "name": "remote-sse",
            "transport": "sse",
            "url": "https://mcp.example.com/sse"
        }
    ]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Exponential backoff schedule for connect retries (seconds), capped at 30s.
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)


def _resolve_env_placeholders(value: Any) -> Any:
    """Replace ``${VAR}`` placeholders with os.environ values inside env dict values.

    Only processes strings. Unknown vars resolve to empty string with a warning.
    SecretRef dicts (``{"env": "VAR"}``, ``{"file": "..."}``) are resolved via
    qanot.secrets.resolve_secret.
    """
    if isinstance(value, dict) and ("env" in value or "file" in value):
        from qanot.secrets import resolve_secret
        try:
            return resolve_secret(value)
        except Exception as e:
            logger.warning("Failed to resolve MCP secret ref %r: %s", value, e)
            return ""
    if not isinstance(value, str):
        return value

    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        resolved = os.environ.get(var, "")
        if not resolved:
            logger.warning("MCP env var ${%s} is empty or unset", var)
        return resolved

    return _ENV_VAR_RE.sub(_sub, value)


def _resolve_env_dict(env: dict | None) -> dict[str, str] | None:
    """Resolve all ${VAR} placeholders + SecretRefs in an env dict."""
    if not env:
        return env
    return {str(k): str(_resolve_env_placeholders(v)) for k, v in env.items()}


class MCPServerConnection:
    """A connection to a single MCP server (stdio, sse, or http transport)."""

    def __init__(
        self,
        name: str,
        command: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        transport: str = "stdio",
        url: str | None = None,
    ):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env
        self.transport = transport
        self.url = url
        self._session = None
        self._client_ctx = None
        self._session_ctx = None
        self._tools: list[dict] = []

    async def _open_transport(self):
        """Open the transport-specific client context and return (read, write)."""
        if self.transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            resolved_env = _resolve_env_dict(self.env)
            server_params = StdioServerParameters(
                command=self.command,
                args=list(self.args),
                env=resolved_env,
            )
            self._client_ctx = stdio_client(server_params)
            streams = await self._client_ctx.__aenter__()
            # stdio_client yields (read, write); other transports may yield triples.
            return streams[0], streams[1]

        if self.transport == "sse":
            if not self.url:
                raise ValueError(f"MCP server '{self.name}' transport=sse requires 'url'")
            from mcp.client.sse import sse_client

            self._client_ctx = sse_client(self.url)
            streams = await self._client_ctx.__aenter__()
            return streams[0], streams[1]

        if self.transport == "http":
            if not self.url:
                raise ValueError(f"MCP server '{self.name}' transport=http requires 'url'")
            from mcp.client.streamable_http import streamablehttp_client

            self._client_ctx = streamablehttp_client(self.url)
            streams = await self._client_ctx.__aenter__()
            # streamable_http yields (read, write, *extras)
            return streams[0], streams[1]

        raise ValueError(
            f"MCP server '{self.name}' has unknown transport '{self.transport}' "
            f"(expected stdio | sse | http)"
        )

    async def _connect_once(self) -> bool:
        """Single connection attempt. Returns True on success."""
        try:
            from mcp import ClientSession  # noqa: F401
        except ImportError:
            logger.warning(
                "MCP server '%s' skipped — 'mcp' package not installed. "
                "Install with: pip install mcp",
                self.name,
            )
            return False

        from mcp import ClientSession

        try:
            read, write = await self._open_transport()

            self._session_ctx = ClientSession(read, write)
            self._session = await self._session_ctx.__aenter__()

            await self._session.initialize()

            tools_result = await self._session.list_tools()
            self._tools = []
            for tool in tools_result.tools:
                self._tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                })

            logger.info(
                "MCP server '%s' connected (%s) — %d tools: %s",
                self.name,
                self.transport,
                len(self._tools),
                ", ".join(t["name"] for t in self._tools),
            )
            return True

        except Exception as e:
            logger.error("MCP server '%s' connect attempt failed: %s", self.name, e)
            await self.disconnect()
            return False

    async def connect(self, *, max_attempts: int = 5) -> bool:
        """Connect to the MCP server with exponential backoff retry.

        Attempts up to ``max_attempts`` times (default 5) with delays
        1s → 2s → 4s → 8s → 16s between attempts, capped at 30s. Returns
        True on success, False after all attempts fail.
        """
        attempts = max(1, max_attempts)
        for attempt in range(attempts):
            if await self._connect_once():
                return True
            if attempt + 1 >= attempts:
                break
            delay = min(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)], 30.0)
            logger.info(
                "MCP server '%s' retry %d/%d in %.1fs",
                self.name, attempt + 2, attempts, delay,
            )
            await asyncio.sleep(delay)
        logger.error(
            "MCP server '%s' failed to connect after %d attempts", self.name, attempts,
        )
        return False

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool on this MCP server."""
        if not self._session:
            return json.dumps({"error": f"MCP server '{self.name}' not connected"})

        try:
            result = await self._session.call_tool(tool_name, arguments=arguments)

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


def _build_connection(cfg: dict) -> MCPServerConnection:
    """Build an MCPServerConnection from a config dict."""
    name = cfg.get("name", "unnamed")
    transport = cfg.get("transport", "stdio")
    return MCPServerConnection(
        name=name,
        command=cfg.get("command", ""),
        args=cfg.get("args", []),
        env=cfg.get("env"),
        transport=transport,
        url=cfg.get("url"),
    )


class MCPManager:
    """Manages multiple MCP server connections and registers their tools."""

    def __init__(self):
        self._servers: dict[str, MCPServerConnection] = {}
        self._failed: list[str] = []
        # Map tool_name → server_name for routing
        self._tool_to_server: dict[str, str] = {}

    async def connect_servers(self, server_configs: list[dict]) -> int:
        """Connect to all configured MCP servers. Returns number of successful connections.

        One failing server never blocks the others — failures are logged and
        the server name is recorded in ``self.failed_servers`` so the /mcp
        command can surface it.
        """
        connected = 0
        self._failed = []
        for cfg in server_configs:
            name = cfg.get("name", "unnamed")
            transport = cfg.get("transport", "stdio")
            if transport == "stdio" and not cfg.get("command"):
                logger.warning("MCP server '%s' has no command — skipping", name)
                self._failed.append(name)
                continue
            if transport in ("sse", "http") and not cfg.get("url"):
                logger.warning("MCP server '%s' has no url — skipping", name)
                self._failed.append(name)
                continue

            server = _build_connection(cfg)
            if await server.connect():
                self._servers[name] = server
                for tool in server.tools:
                    self._tool_to_server[tool["name"]] = name
                connected += 1
            else:
                self._failed.append(name)

        return connected

    async def add_server(
        self, cfg: dict, *, dry_run: bool = False,
    ) -> tuple[bool, list[dict], str]:
        """Probe or add a single MCP server.

        Returns ``(success, tools, error)``. When ``dry_run`` is True, the
        connection is opened, tools are discovered, then the server is
        disconnected immediately — nothing is added to self._servers. When
        False, on success the server stays connected and its tools are
        registered in the routing map.
        """
        server = _build_connection(cfg)
        ok = await server.connect()
        if not ok:
            await server.disconnect()
            return False, [], f"Failed to connect to MCP server '{server.name}'"

        tools = list(server.tools)

        if dry_run:
            await server.disconnect()
            return True, tools, ""

        self._servers[server.name] = server
        for tool in tools:
            self._tool_to_server[tool["name"]] = server.name
        return True, tools, ""

    def register_tools(self, registry) -> int:
        """Register all MCP tools into the Qanot tool registry. Returns count."""
        count = 0
        for server_name, server in self._servers.items():
            for tool in server.tools:
                tool_name = tool["name"]

                prefixed_name = f"mcp_{server_name}_{tool_name}"
                if tool_name in registry.tool_names:
                    register_name = prefixed_name
                else:
                    register_name = tool_name

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
    def failed_servers(self) -> list[str]:
        return list(self._failed)

    @property
    def total_tools(self) -> int:
        return sum(len(s.tools) for s in self._servers.values())
