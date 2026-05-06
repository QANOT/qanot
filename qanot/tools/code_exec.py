"""Register the `execute_code` tool — programmatic tool calling.

Lets the LLM compose multi-step tool workflows in Python without
each intermediate result entering the LLM's context. See
qanot/code_exec.py for the runtime details.
"""

from __future__ import annotations

import json
import logging

from qanot.code_exec import (
    CodeValidationError,
    execute_script,
)
from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)


# Tools available inside execute_code. Curated deliberately — adding
# a tool here is a security decision. Notable exclusions:
#   - run_command (could escape the sandbox)
#   - send_file / send_message (side effects to user without LLM seeing)
#   - agent_create / agent_delete (privileged operations)
#   - evolve_soul / revoke_lesson (avoid recursive self-modification loops)
#
# Read-only / pure-data tools are safe. Writes are allowed only when
# they're scoped (write_file goes through fs_safe; recall_lessons is
# read-only, etc.).
SAFE_FOR_CODE_EXEC = {
    # Filesystem (validated paths)
    "read_file",
    "list_dir",
    "write_file",
    # Memory / RAG
    "memory_search",
    "rag_search",
    "recall_lessons",
    # Web
    "web_search",
    "web_fetch",
    # Topkey (read-only)
    "topkey_list_users",
    "topkey_list_employees",
    "topkey_list_tasks",
    "topkey_get_task",
    "topkey_get_task_history",
    "topkey_list_projects",
    "topkey_get_project",
    "topkey_get_today_attendance",
    "topkey_get_team_summary",
    "topkey_get_employee",
    # Absmarket (read-only)
    "absmarket_query",
    "absmarket_get_cashier_daily_report",
    "absmarket_get_sales",
    "absmarket_get_sale_details",
    "absmarket_get_sales_summary",
    "absmarket_get_purchases",
    "absmarket_get_customers",
    "absmarket_get_customer_details",
    "absmarket_get_items",
    "absmarket_get_item_stock",
    "absmarket_get_outlets",
}


def register_code_exec_tool(
    registry: ToolRegistry,
    workspace_dir: str,
) -> None:
    async def execute_code_handler(params: dict) -> str:
        script = params.get("script", "")
        if not isinstance(script, str) or not script.strip():
            return json.dumps({"error": "script (Python source string) is required"})
        try:
            timeout_s = float(params.get("timeout_s") or 60.0)
        except (TypeError, ValueError):
            timeout_s = 60.0
        timeout_s = max(1.0, min(timeout_s, 120.0))

        # Filter the whitelist down to tools that are actually
        # registered (some plugins may be disabled). Stops the LLM from
        # writing scripts against tools that don't exist.
        registered = set(registry.tool_names)
        allowed = SAFE_FOR_CODE_EXEC & registered

        try:
            result = await execute_script(
                script,
                registry=registry,
                allowed_tools=allowed,
                workspace_dir=workspace_dir,
                timeout_s=timeout_s,
            )
        except CodeValidationError as e:
            return json.dumps({
                "error": "validation_failed",
                "reason": str(e),
                "hint": "Allowed imports: qanot_tools + json/datetime/re/math/statistics/collections/itertools/functools/operator. No os/sys/subprocess/socket/etc.",
            })

        envelope: dict = {
            "stdout": result.stdout,
            "duration_ms": result.duration_ms,
            "tool_calls_made": result.tool_calls_made,
        }
        if result.truncated_stdout:
            envelope["stdout_truncated"] = True
        if result.error:
            envelope["error"] = result.error
        return json.dumps(envelope, ensure_ascii=False)

    # Build a list of "available tool" names for the description so the
    # LLM knows what's accessible without trial-and-error.
    available_names = sorted(SAFE_FOR_CODE_EXEC & set(registry.tool_names))

    registry.register(
        name="execute_code",
        description=(
            "Run a Python script that orchestrates MULTI-STEP tool workflows. "
            "Use when intermediate tool results would clutter your context — e.g. "
            "'for each task in this list, fetch its history, count completions'. "
            "Only `print()` output returns to you; intermediate tool results "
            "stay inside the script. Massive token savings on >3-step turns. "
            "\n\n"
            "USAGE PATTERN:\n"
            "```python\n"
            "tasks = await qanot_tools.topkey_list_tasks(assigned_to=854, status='completed')\n"
            "late_count = 0\n"
            "for t in tasks['items']:\n"
            "    history = await qanot_tools.topkey_get_task_history(task_id=t['id'])\n"
            "    if history.get('completed_at') and t.get('due_date'):\n"
            "        if history['completed_at'] > t['due_date']:\n"
            "            late_count += 1\n"
            "print(f'Late completions: {late_count}')\n"
            "```\n\n"
            "RULES:\n"
            "- Use `await qanot_tools.tool_name(**kwargs)` to call any tool.\n"
            "- Tool results are dicts (already JSON-decoded), navigate them Pythonically.\n"
            "- Only `print()` output reaches you. Plan your final summary print.\n"
            "- Allowed imports: qanot_tools + json/datetime/re/math/statistics/collections/itertools/functools.\n"
            "- 60s default timeout (max 120). 100KB stdout cap.\n"
            "- DO NOT use for single tool calls — call the tool directly. Only use for multi-step orchestration.\n"
            "\n"
            "AVAILABLE TOOLS in this script: "
            + ", ".join(f"qanot_tools.{n}" for n in available_names[:30])
            + (f" ... (+{len(available_names) - 30} more)" if len(available_names) > 30 else "")
        ),
        parameters={
            "type": "object",
            "required": ["script"],
            "properties": {
                "script": {
                    "type": "string",
                    "description": (
                        "Python source. Wrapped automatically in `async def _user_script(): ...`. "
                        "Use `await qanot_tools.tool_name(**kwargs)` for tool calls; print() what you want returned."
                    ),
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Timeout in seconds (default 60, max 120).",
                },
            },
        },
        handler=execute_code_handler,
    )
    logger.info(
        "execute_code registered with %d tools available inside",
        len(available_names),
    )
