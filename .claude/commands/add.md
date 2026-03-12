Implement the feature described: $ARGUMENTS

Follow this process:

1. **Understand**: Read the relevant existing code first. Never assume — check current patterns
2. **Design**: Brief mental model of what changes where. If it touches >3 files or has architectural implications, outline the plan before coding
3. **Implement**: Write production code following project conventions exactly:
   - `from __future__ import annotations` at top
   - Type hints, f-strings, private `_` prefix for internals
   - Config changes → update Config dataclass + load_config() + config.example.json
   - New tools → register in main.py, document in TOOLS.md template
   - User-facing messages in Uzbek
   - Per-user isolation: no shared mutable state in agent loop
4. **Test**: Add tests for the new behavior. Run `python3 -m pytest tests/ -v`
5. **Report**: What was added and any decisions made

Keep it lightweight — no enterprise bloat, no speculative features. Build exactly what's asked.
