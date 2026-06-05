"""Programmatic tool calling — let the LLM compose multi-step workflows.

Inspired by Hermes Agent's `execute_code`: the agent writes a Python
script that orchestrates tool calls; only `print()` output returns to
the LLM. Intermediate tool results never enter the context window.

Why this exists:
  Multi-step turns ("for each task in this list, fetch its history,
  count completions, summarize") would otherwise hit the LLM with
  20+ tool_use rounds — each round including the full prior tool
  results in context. With execute_code, the entire orchestration
  runs in ONE LLM round; the LLM sees only the final summary the
  script printed.

Token cost analysis (typical multi-step workflow):
  Naive (round-trip per tool):  ~3000 tokens × 10 calls = ~30k tokens
  execute_code:                  ~500 tokens (script in + summary out)
  → 50-90% reduction on tool-heavy turns.

Security model (v1):
  - AST-walks the script, rejects all imports outside an allowlist
    (qanot_tools + safe stdlib subset).
  - Provides a restricted builtins set — no open(), no exec(), no
    __import__.
  - Wraps the script in an async function so `await` works.
  - Hard timeout via asyncio.wait_for.
  - stdout capped at 100KB; stderr swallowed (kept for logs).

What this is NOT:
  - A real sandbox. The script runs in-process. A determined hostile
    LLM (or prompt injection) could try unusual tricks. v2 work:
    move to subprocess with seccomp/restricted env.
  - A general code interpreter. Tools are explicitly whitelisted in
    qanot/tools/code_exec.py SAFE_TOOLS — adding a tool to that list
    is a deliberate decision.

What this IS:
  - A token-saving primitive for multi-step orchestration.
  - A natural fit for "for each X, do Y, summarize" patterns.
  - A bounded, auditable execution path (one log line per call).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import io
import logging
import textwrap
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass
from typing import Any

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Stdlib modules safe to import from inside the script. Carefully chosen:
# pure-data manipulation, no I/O, no networking, no subprocess.
ALLOWED_STDLIB = frozenset({
    "json", "datetime", "re", "math", "statistics",
    "collections", "itertools", "functools", "operator",
    "string", "decimal", "fractions", "random",
    "typing", "dataclasses", "enum",
})

# Builtins the script is allowed to use. Notable exclusions: open,
# exec, eval, compile, __import__, input, breakpoint, help.
SAFE_BUILTINS = frozenset({
    "abs", "all", "any", "bin", "bool", "bytes", "callable", "chr",
    "complex", "dict", "divmod", "enumerate", "filter", "float",
    "format", "frozenset", "getattr", "hasattr", "hash", "hex", "id",
    "int", "isinstance", "issubclass", "iter", "len", "list", "map",
    "max", "min", "next", "object", "oct", "ord", "pow", "print",
    "range", "repr", "reversed", "round", "set", "slice", "sorted",
    "str", "sum", "tuple", "type", "zip",
    # exception types so try/except works
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError", "StopIteration", "AssertionError",
    "True", "False", "None",
})


class CodeValidationError(ValueError):
    """Raised when the script fails the AST whitelist check."""


@dataclass
class ExecResult:
    stdout: str
    error: str | None = None
    duration_ms: int = 0
    tool_calls_made: int = 0
    truncated_stdout: bool = False


def _is_allowed_import(module_name: str) -> bool:
    """A module is allowed iff its top-level name is qanot_tools or in
    the stdlib allowlist."""
    if not module_name:
        return False
    root = module_name.split(".", 1)[0]
    return root == "qanot_tools" or root in ALLOWED_STDLIB


def _is_dunder(name: str) -> bool:
    """True for ``__dunder__`` names (the sandbox-escape surface)."""
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


# Builtins whose string argument could name a dunder to reach the escape
# surface without a literal ``.__class__`` attribute node.
_REFLECTION_BUILTINS = frozenset({"getattr", "setattr", "delattr", "vars"})


def validate_script(script: str) -> None:
    """AST-walk the script and reject anything off the whitelist.

    Raises CodeValidationError with a precise reason. Doesn't run the
    script — purely static analysis.
    """
    try:
        tree = ast.parse(script)
    except SyntaxError as e:
        raise CodeValidationError(f"syntax error: {e.msg} on line {e.lineno}") from e

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_allowed_import(alias.name):
                    raise CodeValidationError(
                        f"disallowed import {alias.name!r} (allowed: qanot_tools, {sorted(ALLOWED_STDLIB)})"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not _is_allowed_import(mod):
                raise CodeValidationError(
                    f"disallowed import-from {mod!r} (allowed: qanot_tools, {sorted(ALLOWED_STDLIB)})"
                )
        # Block direct calls to dangerous builtins via ast.Name lookups.
        # We also restrict via __builtins__ at exec time, so this is
        # belt-and-suspenders. Catches `__import__('os')` style attacks.
        elif isinstance(node, ast.Name) and node.id in {"__import__", "exec", "eval", "compile"}:
            raise CodeValidationError(f"disallowed name {node.id!r}")
        # Block dunder-attribute traversal — the classic in-process escape
        # ``().__class__.__bases__[0].__subclasses__()`` that walks from a
        # harmless object to ``os``/``__builtins__``. A pure-data script never
        # needs ``__class__``/``__globals__``/``__mro__``/etc.
        elif isinstance(node, ast.Attribute) and _is_dunder(node.attr):
            raise CodeValidationError(f"disallowed dunder attribute {node.attr!r}")
        # Same escape in string form: getattr(x, "__class__"), vars(x), …
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _REFLECTION_BUILTINS
        ):
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and _is_dunder(arg.value)
                ):
                    raise CodeValidationError(
                        f"disallowed dunder via {node.func.id}: {arg.value!r}"
                    )


def _safe_builtins() -> dict[str, Any]:
    """Build a __builtins__ dict containing only SAFE_BUILTINS."""
    import builtins as _b
    out: dict[str, Any] = {}
    for name in SAFE_BUILTINS:
        if hasattr(_b, name):
            out[name] = getattr(_b, name)
    return out


class _ToolStubs:
    """Object exposed to the script as `qanot_tools`.

    Each attribute access returns an async function that dispatches to
    the underlying ToolRegistry. The result is the JSON-decoded tool
    response (so the LLM-generated script can introspect it
    Pythonically) — NOT the raw JSON string.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        allowed_tools: set[str],
        workspace_dir: str = "",
    ):
        self._registry = registry
        self._allowed = allowed_tools
        self._workspace_dir = workspace_dir
        self._call_count = 0

    def __getattr__(self, name: str) -> Any:  # noqa: D401
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._allowed:
            raise AttributeError(
                f"tool {name!r} is not available inside execute_code "
                f"(available: {sorted(self._allowed)})"
            )

        async def _call(**kwargs: Any) -> Any:
            self._call_count += 1
            raw = await self._registry.execute(
                name, dict(kwargs), workspace_dir=self._workspace_dir,
            )
            # Tools always return JSON strings. Try to parse so the
            # script can navigate the result like a dict.
            import json
            try:
                return json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                return raw  # leave as string if not JSON

        _call.__name__ = name
        _call.__doc__ = f"Async wrapper around qanot tool '{name}'."
        return _call


async def execute_script(
    script: str,
    *,
    registry: ToolRegistry,
    allowed_tools: set[str],
    workspace_dir: str = "",
    timeout_s: float = 60.0,
    max_stdout_bytes: int = 100_000,
) -> ExecResult:
    """Run an LLM-generated script. Returns captured stdout + error.

    The script is wrapped in `async def _user_script():` so it can
    `await qanot_tools.tool_name(...)`. Only the captured stdout is
    returned to the LLM; intermediate tool results stay inside the
    script's local scope.
    """
    import time
    started = time.monotonic()

    # 1. Validate (raises CodeValidationError on failure)
    validate_script(script)

    # 2. Build the executable namespace. Note: __builtins__ is set
    #    explicitly to the curated dict; default Python would inject
    #    the full builtins module otherwise.
    stubs = _ToolStubs(registry, allowed_tools, workspace_dir=workspace_dir)
    namespace: dict[str, Any] = {
        "__builtins__": _safe_builtins(),
        "__name__": "__qanot_user_script__",
        "qanot_tools": stubs,
    }

    # 3. Wrap as async fn — indent every line, prepend `async def`. The
    #    indentation handles existing user indentation cleanly.
    wrapped = "async def _user_script():\n" + textwrap.indent(script, "    ")
    # Edge case: empty script body needs a `pass`.
    if not script.strip():
        wrapped = "async def _user_script():\n    pass\n"

    # 4. Compile + execute the wrapper to install _user_script in
    #    namespace. This step CAN raise (e.g. SyntaxError after our
    #    indentation gymnastics for weird input). We catch and re-wrap.
    try:
        compiled = compile(wrapped, "<execute_code>", "exec")
        exec(compiled, namespace)  # noqa: S102  — safe via curated namespace
    except SyntaxError as e:
        return ExecResult(
            stdout="", error=f"syntax error in wrapped script: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    user_script = namespace.get("_user_script")
    if user_script is None or not inspect.iscoroutinefunction(user_script):
        return ExecResult(
            stdout="", error="internal: _user_script not produced",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 5. Run with timeout, capturing stdout.
    stdout_buf = io.StringIO()
    error: str | None = None

    async def _runner():
        with redirect_stdout(stdout_buf):
            await user_script()

    try:
        await asyncio.wait_for(_runner(), timeout=timeout_s)
    except asyncio.TimeoutError:
        error = f"timeout after {timeout_s:.1f}s (script exceeded limit)"
    except Exception as e:  # noqa: BLE001  — capture all script errors
        # Format with traceback for the LLM to debug.
        tb = traceback.format_exc()
        # Strip wrapper-frame noise: keep only frames that mention <execute_code>.
        useful_tb = "\n".join(
            line for line in tb.splitlines()
            if "<execute_code>" in line or "Error" in line or "Exception" in line
            or line.strip().startswith("File")
            or (line and not line.startswith("Traceback") and not line.startswith("  File"))
        )
        error = f"{type(e).__name__}: {e}"
        if useful_tb and useful_tb != error:
            error += f"\n{useful_tb[:1000]}"

    out = stdout_buf.getvalue()
    truncated = False
    if len(out.encode("utf-8")) > max_stdout_bytes:
        # Truncate by bytes, not chars (stdout could be unicode-heavy).
        encoded = out.encode("utf-8")[:max_stdout_bytes]
        # Decode safely — drop any partial char at the boundary.
        out = encoded.decode("utf-8", errors="ignore")
        out += f"\n... (truncated; total > {max_stdout_bytes} bytes)"
        truncated = True

    return ExecResult(
        stdout=out,
        error=error,
        duration_ms=int((time.monotonic() - started) * 1000),
        tool_calls_made=stubs._call_count,
        truncated_stdout=truncated,
    )
