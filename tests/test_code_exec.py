"""Tests for the programmatic tool-calling runtime + tool wrapper.

Coverage:
  - AST validator: rejects disallowed imports, accepts qanot_tools + stdlib subset
  - Restricted builtins: open()/exec()/__import__ unavailable
  - Async wrapper: `await qanot_tools.foo()` works inside the script
  - Stdout capture: only print() output returned; intermediate results invisible
  - Timeout: long scripts get killed
  - Tool dispatch: calls reach the registered handler with kwargs as dict
  - Tool whitelist: scripts can't call non-whitelisted tools
  - Tool result decoding: JSON tool responses parsed before return
  - End-to-end: register the tool, exercise via registry.execute()
"""

from __future__ import annotations

import asyncio
import json

import pytest

from qanot import code_exec
from qanot.code_exec import (
    CodeValidationError,
    execute_script,
    validate_script,
)
from qanot.registry import ToolRegistry


# ── AST validator ──────────────────────────────────────────────


def test_validate_accepts_qanot_tools_import():
    validate_script("import qanot_tools\n")  # no raise


def test_validate_accepts_allowed_stdlib():
    for mod in ("json", "datetime", "re", "math", "statistics", "collections", "itertools"):
        validate_script(f"import {mod}\n")


def test_validate_rejects_os():
    with pytest.raises(CodeValidationError, match="os"):
        validate_script("import os\n")


def test_validate_rejects_subprocess():
    with pytest.raises(CodeValidationError, match="subprocess"):
        validate_script("import subprocess\n")


def test_validate_rejects_socket():
    with pytest.raises(CodeValidationError, match="socket"):
        validate_script("import socket\n")


def test_validate_rejects_from_os_import():
    with pytest.raises(CodeValidationError, match="os"):
        validate_script("from os import getcwd\n")


def test_validate_rejects_dunder_import_call():
    with pytest.raises(CodeValidationError, match="__import__"):
        validate_script("__import__('os')\n")


def test_validate_rejects_eval():
    with pytest.raises(CodeValidationError, match="eval"):
        validate_script("eval('1+1')\n")


def test_validate_rejects_syntax_error():
    with pytest.raises(CodeValidationError, match="syntax"):
        validate_script("def foo(\n")


def test_validate_accepts_complex_legitimate_script():
    """A realistic multi-step orchestration script should pass."""
    script = """
import json
from collections import Counter

results = await qanot_tools.topkey_list_tasks(assigned_to=854)
late = 0
for t in results.get('items', []):
    if t.get('status') == 'completed':
        if t.get('completed_on', '') > t.get('due_date', ''):
            late += 1
counts = Counter([t.get('status') for t in results.get('items', [])])
print(json.dumps({'late': late, 'by_status': dict(counts)}, ensure_ascii=False))
"""
    validate_script(script)  # no raise


# ── execute_script: builtins + isolation ──────────────────────


def _make_registry(tools_to_register: dict[str, callable]) -> ToolRegistry:
    """Helper: build a registry with given fake handlers."""
    r = ToolRegistry()
    for name, handler in tools_to_register.items():
        r.register(
            name=name,
            description=f"fake {name}",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        )
    return r


def test_open_is_blocked():
    r = _make_registry({})
    res = asyncio.run(execute_script(
        "open('/etc/passwd')",
        registry=r, allowed_tools=set(),
    ))
    assert res.error is not None
    assert "open" in res.error or "not defined" in res.error.lower()


def test_print_captured():
    r = _make_registry({})
    res = asyncio.run(execute_script(
        "print('hello world')",
        registry=r, allowed_tools=set(),
    ))
    assert res.error is None
    assert res.stdout.strip() == "hello world"


def test_only_stdout_returned_not_intermediate_values():
    """Variables set inside the script must not leak — only print()."""
    r = _make_registry({})
    res = asyncio.run(execute_script(
        "secret = 'do not show'\nresult = secret + ' computation'\nprint('only this')",
        registry=r, allowed_tools=set(),
    ))
    assert res.error is None
    assert "do not show" not in res.stdout
    assert "only this" in res.stdout


# ── tool dispatch ──────────────────────────────────────────────


def test_tool_call_dispatches_with_kwargs_as_dict():
    received: list[dict] = []

    async def fake_tool(params: dict) -> str:
        received.append(params)
        return json.dumps({"echo": params})

    r = _make_registry({"my_tool": fake_tool})
    script = """
result = await qanot_tools.my_tool(name='ali', age=30)
print(result['echo']['name'])
"""
    res = asyncio.run(execute_script(
        script, registry=r, allowed_tools={"my_tool"},
    ))
    assert res.error is None, res.error
    assert res.stdout.strip() == "ali"
    assert received == [{"name": "ali", "age": 30}]
    assert res.tool_calls_made == 1


def test_tool_result_json_parsed_before_return():
    """Script should receive a dict, not a JSON string."""
    async def fake_tool(params):
        return json.dumps({"items": [1, 2, 3], "total": 3})

    r = _make_registry({"q": fake_tool})
    res = asyncio.run(execute_script(
        "r = await qanot_tools.q()\nprint(r['total'], len(r['items']))",
        registry=r, allowed_tools={"q"},
    ))
    assert res.error is None, res.error
    assert res.stdout.strip() == "3 3"


def test_non_whitelisted_tool_blocked_at_attr_access():
    async def fake_tool(params):
        return "{}"

    r = _make_registry({"safe_tool": fake_tool, "danger_tool": fake_tool})
    res = asyncio.run(execute_script(
        "await qanot_tools.danger_tool()",
        registry=r, allowed_tools={"safe_tool"},
    ))
    assert res.error is not None
    assert "danger_tool" in res.error
    assert "not available" in res.error.lower()


def test_non_existent_tool_attr_returns_attribute_error():
    r = _make_registry({})
    res = asyncio.run(execute_script(
        "await qanot_tools.does_not_exist()",
        registry=r, allowed_tools=set(),
    ))
    assert res.error is not None
    assert "does_not_exist" in res.error


def test_multistep_workflow_only_summary_in_stdout():
    """The classic use case — multi-step compose, print summary only."""
    async def list_items(params):
        return json.dumps({"items": [{"id": 1}, {"id": 2}, {"id": 3}]})

    async def fetch_detail(params):
        item_id = params["id"]
        return json.dumps({"id": item_id, "verbose_data": "x" * 1000})

    r = _make_registry({"list_items": list_items, "fetch_detail": fetch_detail})
    script = """
items = await qanot_tools.list_items()
total = 0
for it in items['items']:
    detail = await qanot_tools.fetch_detail(id=it['id'])
    total += len(detail['verbose_data'])
print(f"processed: {len(items['items'])} items, total bytes: {total}")
"""
    res = asyncio.run(execute_script(
        script, registry=r,
        allowed_tools={"list_items", "fetch_detail"},
    ))
    assert res.error is None, res.error
    assert "processed: 3 items" in res.stdout
    assert "total bytes: 3000" in res.stdout
    # Critical assertion: the verbose intermediate data must NOT be in stdout
    assert "x" * 100 not in res.stdout
    assert res.tool_calls_made == 4  # 1 list + 3 fetches


# ── timeout + truncation ───────────────────────────────────────


def test_timeout_kills_long_running_script():
    async def slow_tool(params):
        await asyncio.sleep(5)
        return "{}"

    r = _make_registry({"slow_tool": slow_tool})
    res = asyncio.run(execute_script(
        "await qanot_tools.slow_tool()",
        registry=r, allowed_tools={"slow_tool"},
        timeout_s=0.5,
    ))
    assert res.error is not None
    assert "timeout" in res.error.lower()


def test_stdout_truncation_at_byte_cap():
    r = _make_registry({})
    # Generate ~5000 bytes of stdout, cap at 1000.
    res = asyncio.run(execute_script(
        "for _ in range(500):\n    print('xxxxxxxxxx')",
        registry=r, allowed_tools=set(),
        max_stdout_bytes=1000,
    ))
    assert res.error is None
    assert res.truncated_stdout is True
    assert len(res.stdout.encode("utf-8")) <= 1500  # some truncation message overhead allowed


# ── end-to-end via registry ────────────────────────────────────


def test_register_code_exec_tool_end_to_end(tmp_path):
    """Register the tool through the public path + invoke via registry.execute."""
    from qanot.tools.code_exec import register_code_exec_tool

    received: list[dict] = []

    async def fake_read_file(params):
        received.append(params)
        return json.dumps({"content": "file body", "path": params.get("path")})

    r = ToolRegistry()
    # Register a tool with a name that's in the SAFE_FOR_CODE_EXEC list.
    r.register(
        name="read_file",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=fake_read_file,
    )
    register_code_exec_tool(r, str(tmp_path))

    script = """
result = await qanot_tools.read_file(path='/test/x')
print(result['content'])
"""
    response = asyncio.run(r.execute("execute_code", {"script": script}))
    parsed = json.loads(response)
    assert parsed.get("error") is None
    assert "file body" in parsed["stdout"]
    assert parsed["tool_calls_made"] == 1


def test_validation_error_returns_envelope(tmp_path):
    from qanot.tools.code_exec import register_code_exec_tool

    r = ToolRegistry()
    register_code_exec_tool(r, str(tmp_path))
    response = asyncio.run(r.execute(
        "execute_code", {"script": "import os\nprint(os.getcwd())"},
    ))
    parsed = json.loads(response)
    assert parsed.get("error") == "validation_failed"
    assert "os" in parsed["reason"]


def test_empty_script_returns_error(tmp_path):
    from qanot.tools.code_exec import register_code_exec_tool

    r = ToolRegistry()
    register_code_exec_tool(r, str(tmp_path))
    response = asyncio.run(r.execute("execute_code", {"script": ""}))
    parsed = json.loads(response)
    assert "error" in parsed
