Investigate and fix the issue described: $ARGUMENTS

Follow this process:

1. **Reproduce**: Understand the exact failure — run tests, read error traces, grep for related code
2. **Trace**: Follow the request flow: Telegram → Agent → Provider → Tools → back. Identify which component fails
3. **Root cause**: Find the specific line/logic causing the issue (don't guess — read the code)
4. **Fix**: Make the minimal change that fixes the root cause. No drive-by refactoring
5. **Test**: Run `python3 -m pytest tests/ -v` to verify no regressions. Add a test for the fix if the bug wasn't covered
6. **Report**: Brief summary of what was wrong and what you changed

Key files to check based on bug type:
- Agent loop issues → `qanot/agent.py` (circuit breaker, tool execution, message construction)
- Streaming issues → `qanot/telegram.py` (draft pausing, flush interval, race conditions)
- Provider errors → `qanot/providers/` (API format, streaming events, cost calculation)
- Config issues → `qanot/config.py` + `qanot/main.py` (3-place propagation rule)
- Memory/context → `qanot/memory.py` + `qanot/context.py` (WAL protocol, compaction)
