Run and analyze tests: $ARGUMENTS

If no arguments, run the full suite. If a specific area is given, run only matching tests.

Process:

1. Run: `python3 -m pytest tests/ -v` (or filtered by argument)
2. If all pass: report count and coverage areas
3. If failures:
   - Read the failing test code
   - Read the source code it tests
   - Identify root cause (test bug vs code bug)
   - Fix whichever is wrong
   - Re-run to confirm
4. Check coverage gaps: are there untested critical paths?
   - Agent loop edge cases (circuit breaker, error classification)
   - Streaming pipeline (draft pausing, tool_use events)
   - Provider format conversion
   - Config loading edge cases

If `$ARGUMENTS` is "coverage", identify untested code paths and write new tests for them.
