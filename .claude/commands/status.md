Give a full project status report:

1. **Git state**: `git status`, current branch, uncommitted changes, last 5 commits
2. **Tests**: Run `python3 -m pytest tests/ -v --tb=short` — report pass/fail count
3. **Architecture**: Count files, functions, lines of code in `qanot/`
4. **Dependencies**: Check pyproject.toml for current deps
5. **Open issues**: Any TODOs or FIXMEs in the codebase? (`grep -r "TODO\|FIXME\|HACK\|XXX" qanot/`)
6. **Summary**: Brief health assessment — what's solid, what needs attention next
