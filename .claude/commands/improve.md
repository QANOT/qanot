Analyze and improve the specified area: $ARGUMENTS

If no specific area given, scan the full codebase for the highest-impact improvements.

Process:

1. **Audit**: Read the target code. Identify concrete issues:
   - Performance: N+1 patterns, blocking in async, unnecessary allocations
   - Reliability: Missing error handling at system boundaries, unbounded growth
   - Security: Input validation gaps, injection risks
   - Logic: Edge cases, race conditions, off-by-one errors
2. **Prioritize**: Rank issues by impact. Fix high-impact first
3. **Improve**: Make targeted changes. No cosmetic-only changes — every edit must have measurable benefit
4. **Test**: Run `python3 -m pytest tests/ -v`. Add tests if improving uncovered code
5. **Report**: What was improved and why

Rules:
- Don't add features — only improve existing code
- Don't refactor for style — only for correctness/performance/security
- Don't add abstractions unless they eliminate real duplication
- Keep the diff minimal and focused
