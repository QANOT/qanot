Review the current uncommitted changes or the specified area: $ARGUMENTS

If no arguments, run `git diff` and review all pending changes.

Check for:

### Correctness
- Does the code do what it claims?
- Edge cases handled? (empty input, None, concurrent users)
- Per-user isolation preserved? No shared mutable state?
- Agent loop: circuit breaker still works? No infinite loops?
- Streaming: draft pausing during tool execution?

### Convention Compliance
- `from __future__ import annotations`?
- Config 3-place rule (dataclass + load_config + config.example.json)?
- User-facing messages in Uzbek?
- Tests updated for behavioral changes?

### Security
- No hardcoded secrets
- Input validation on external boundaries
- Telegram allowed_users check intact

### Performance
- No blocking calls in async context
- No unbounded list/dict growth
- Token usage reasonable

Output format — for each issue:
```
[SEVERITY] file:line — description
```
Severity: CRITICAL > BUG > WARN > STYLE

End with summary and recommendation: ship / fix-then-ship / needs-rework
