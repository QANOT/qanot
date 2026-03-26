# Plugin System

Qanot AI supports plugins for adding custom tools, extending the agent's personality, and integrating with external services.

## Plugin Architecture

A plugin is a directory containing at minimum a `plugin.py` file with a class that extends `Plugin`:

```
plugins/
└── myplugin/
    ├── plugin.py      # Required: Plugin subclass
    ├── TOOLS.md       # Optional: tool docs appended to workspace TOOLS.md
    └── helpers.py     # Optional: additional modules
```

Plugins are loaded from two locations (checked in order):

1. **Built-in:** `plugins/` directory at the package root
2. **External:** The `plugins_dir` path from config (default: `/data/plugins`)

## Creating a Plugin

### Step 1: Create the plugin directory

```bash
mkdir -p plugins/weather
```

### Step 2: Write plugin.py

```python
from qanot.plugins.base import Plugin, ToolDef, tool

class QanotPlugin(Plugin):
    """Weather lookup plugin."""

    name = "weather"
    description = "Weather information for Uzbekistan cities"

    # Optional: content appended to workspace TOOLS.md
    tools_md = """
## Weather Tools

### weather_get
Get current weather for a city in Uzbekistan.
- **city**: City name (e.g., "Tashkent", "Samarkand")
"""

    # Optional: content appended to workspace SOUL.md
    soul_append = """
## Weather Behavior
When asked about weather, always use the weather_get tool.
Include temperature in both Celsius and Fahrenheit.
"""

    async def setup(self, config: dict) -> None:
        """Called when the plugin loads. Initialize resources here."""
        self.api_key = config.get("api_key", "")
        self.base_url = config.get("base_url", "https://api.weather.example.com")

    async def teardown(self) -> None:
        """Called on shutdown. Clean up resources here."""
        pass

    def get_tools(self) -> list[ToolDef]:
        """Return tool definitions. Use _collect_tools() for decorated methods."""
        return self._collect_tools()

    @tool(
        name="weather_get",
        description="Hozirgi ob-havo ma'lumotlari.",
        parameters={
            "type": "object",
            "required": ["city"],
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Shahar nomi (masalan: Tashkent)",
                },
            },
        },
    )
    async def weather_get(self, params: dict) -> str:
        import aiohttp
        import json

        city = params.get("city", "Tashkent")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/current",
                params={"city": city, "key": self.api_key},
            ) as resp:
                data = await resp.json()
                return json.dumps(data, ensure_ascii=False)
```

### Step 3: Configure the plugin

Add the plugin to `config.json`:

```json
{
  "plugins": [
    {
      "name": "weather",
      "enabled": true,
      "config": {
        "api_key": "your-weather-api-key",
        "base_url": "https://api.weather.example.com"
      }
    }
  ]
}
```

## The @tool Decorator

The `@tool` decorator marks a method as an agent-callable tool:

```python
@tool(
    name="tool_name",           # Unique tool name
    description="What it does", # Shown to the LLM
    parameters={                # JSON Schema for input
        "type": "object",
        "required": ["param1"],
        "properties": {
            "param1": {"type": "string", "description": "..."},
            "param2": {"type": "integer", "description": "...", "default": 10},
        },
    },
)
async def my_tool(self, params: dict) -> str:
    # params is a dict matching the JSON Schema
    # Return a string (typically JSON)
    return json.dumps({"result": "value"})
```

The `_collect_tools()` method on `Plugin` scans for all methods with `@tool` and returns `ToolDef` objects.

## Plugin Lifecycle

### Loading

1. Plugin name is looked up in built-in and external directories
2. `plugin.py` is dynamically imported
3. A class named `QanotPlugin` is searched for; if not found, any `Plugin` subclass is used
4. The class is instantiated and `setup(config)` is called
5. `get_tools()` is called and each tool is registered in the `ToolRegistry`
6. `tools_md` content is appended to `workspace/TOOLS.md`
7. `soul_append` content is appended to `workspace/SOUL.md`

### Runtime

- Tools are available immediately after loading
- The plugin instance persists for the lifetime of the process
- Tool handlers are called with the params dict when the agent invokes them

### Shutdown

`teardown()` is called when the process exits. Use it to close connections, flush buffers, etc.

## TOOLS.md Integration

If your plugin sets `tools_md`, that content is appended to the workspace `TOOLS.md` file. This is how the agent learns about your tools -- the content appears in the system prompt.

The content is only appended once (checked by plugin name). Write it as Markdown that explains to the agent when and how to use your tools.

## SOUL_APPEND Integration

If your plugin sets `soul_append`, that content is appended to the workspace `SOUL.md` file. Use this to add personality traits or behavioral rules related to your plugin.

The first line of `soul_append` is used as a deduplication marker -- it won't be appended twice.

## Plugin Configuration

The `config` dict passed to `setup()` comes directly from the plugin entry in `config.json`. You can put any key-value pairs there:

```json
{
  "name": "myplugin",
  "enabled": true,
  "config": {
    "api_url": "https://api.example.com",
    "db_host": "localhost",
    "db_port": 3306,
    "db_user": "admin",
    "db_password": "secret",
    "timeout": 30
  }
}
```

Access in `setup()`:

```python
async def setup(self, config: dict) -> None:
    self.api_url = config["api_url"]
    self.timeout = config.get("timeout", 10)
```

## Manual Tool Registration

For cases where a full plugin is not needed, register tools directly on the `ToolRegistry`:

```python
async def my_handler(params: dict) -> str:
    return json.dumps({"ok": True})

registry.register(
    name="my_tool",
    description="Does something useful.",
    parameters={"type": "object", "properties": {}},
    handler=my_handler,
)
```

This is done in `qanot/main.py` for built-in tools and can be used in custom entry points.

## Plugin Discovery

Plugins are found by directory name. The loader checks:

1. `{package_root}/plugins/{name}/plugin.py` -- built-in plugins shipped with Qanot
2. `{plugins_dir}/{name}/plugin.py` -- external plugins from the config path

The plugin directory is temporarily added to `sys.path` during loading, then removed. This means your plugin can import from sibling modules in its directory.

## Plugin Manifest (plugin.json)

Plugins can include a `plugin.json` file for metadata and dependency management:

```json
{
  "name": "weather",
  "version": "1.0.0",
  "description": "Weather information for Uzbekistan cities",
  "author": "Your Name",
  "dependencies": ["aiohttp>=3.9"],
  "plugin_deps": ["cloud_reporter"],
  "required_config": ["api_key"],
  "min_qanot_version": "2.0.0",
  "homepage": "https://github.com/example/weather-plugin",
  "license": "MIT"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Plugin name (defaults to directory name) |
| `version` | string | Semantic version (default: `"0.1.0"`) |
| `description` | string | Human-readable description |
| `author` | string | Plugin author |
| `dependencies` | list | pip packages required by the plugin |
| `plugin_deps` | list | Other Qanot plugins this plugin depends on |
| `required_config` | list | Config keys that must be present in the plugin's config |
| `min_qanot_version` | string | Minimum Qanot version required |
| `homepage` | string | URL for plugin documentation or repository |
| `license` | string | License identifier (default: `"MIT"`) |

If `plugin.json` is not present, a default manifest is created from the directory name.

## Error Handling

### on_error() Hook

Plugins can override the `on_error()` method to handle tool execution failures:

```python
async def on_error(self, tool_name: str, error: Exception) -> None:
    """Called when a tool execution fails."""
    logger.error("Tool %s failed: %s", tool_name, error)
    # Custom error handling: retry, notify, fallback, etc.
```

This hook is called with the tool name and the exception that was raised. Override it for custom error reporting, retry logic, or graceful degradation.

### validate_tool_params()

The `validate_tool_params()` function provides lightweight JSON Schema validation for tool parameters:

```python
from qanot.plugins.base import validate_tool_params

errors = validate_tool_params(
    params={"city": "Tashkent", "units": 42},
    schema={
        "type": "object",
        "required": ["city"],
        "properties": {
            "city": {"type": "string"},
            "units": {"type": "string"},
        },
    },
)
# errors: ["Parameter 'units' expected string, got int"]
```

It checks required fields and basic type matching (string, integer, number, boolean, array, object). Returns an empty list if all parameters are valid.

## Available Plugins

| Plugin | Tools | Description |
|--------|-------|-------------|
| amoCRM | 20 | CRM integration: leads, contacts, pipelines, tasks, notes |
| Bitrix24 | 24 | CRM integration: deals, leads, contacts, tasks, activities |
| 1C Enterprise | 13 | Accounting: contractors, products, sales, purchases, balances |
| AbsMarket | 8 | POS system: products, sales, inventory, reports |
| AbsVision | 3 | HR system: employees, attendance, payroll |
| iBox POS | -- | POS system integration for iBox terminals |
| Eskiz SMS | -- | SMS sending via Eskiz.uz API |
| MySQL Query | 1 | Standalone SELECT-only SQL query tool |
| Cloud Reporter | 1 | Usage reporting to Qanot Cloud platform |

## QanotHub

Browse and install community plugins from QanotHub: [https://qanot.github.io/qanot-plugins/](https://qanot.github.io/qanot-plugins/)

### Installing Plugins from QanotHub

```bash
# Search available plugins
qanot plugin search

# Install a plugin by name
qanot plugin install <name>
```

Installed plugins are placed in your `plugins_dir` and can be configured in `config.json` like any other plugin.
