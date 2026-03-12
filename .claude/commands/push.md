Run `git diff --stat` and `git diff` to see all local changes (staged + unstaged). Then:

1. Analyze every change — understand what was added, modified, fixed
2. Write a clear commit message: first line under 72 chars summarizing the "what", body explaining "why" if non-obvious
3. Stage only relevant files (never stage .env, credentials, or __pycache__)
4. Commit (NEVER add Co-Authored-By)
5. Push to origin

If there are no changes, say so. If changes span multiple unrelated features, ask whether to split into separate commits.
