#!/usr/bin/env python3
"""Autoresearch for Qanot AI — automated code improvement via Claude API.

Inspired by Karpathy's autoresearch, adapted for software engineering:
  ML version:   modify code → train → loss decreased? → keep/discard
  Qanot version: modify code → test  → all pass + improved? → keep/discard

Usage:
    python scripts/autoresearch.py --rounds 10
    python scripts/autoresearch.py --rounds 50 --focus performance
    python scripts/autoresearch.py --rounds 5 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from textwrap import dedent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QANOT_DIR = PROJECT_ROOT / "qanot"
TESTS_DIR = PROJECT_ROOT / "tests"
LOG_DIR = PROJECT_ROOT / "scripts" / "autoresearch_logs"
BRANCH_PREFIX = "autoresearch"

# Files the agent is allowed to modify (safety boundary)
ALLOWED_FILES = sorted(
    str(p.relative_to(PROJECT_ROOT))
    for p in QANOT_DIR.rglob("*.py")
    if "__pycache__" not in str(p)
)

FOCUS_AREAS = {
    "quality": "code quality, readability, naming, DRY, simplification",
    "performance": "performance, async efficiency, caching, reducing allocations",
    "security": "security hardening, input validation, injection prevention",
    "reliability": "error handling, edge cases, robustness, defensive coding",
    "tests": "test coverage, missing edge case tests, test quality",
}

# Core files — the most important parts of the system
CORE_FILES = [
    "qanot/agent.py",           # Agent loop — heart of the system
    "qanot/telegram.py",        # Telegram adapter — user-facing
    "qanot/context.py",         # Context/token management
    "qanot/memory.py",          # WAL protocol, memory system
    "qanot/prompt.py",          # System prompt builder
    "qanot/compaction.py",      # Conversation compaction
    "qanot/config.py",          # Configuration
    "qanot/providers/anthropic.py",  # Main LLM provider
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(cmd: list[str], timeout: int = 120, **kwargs) -> subprocess.CompletedProcess:
    """Run a command and return result."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=timeout,
        **kwargs,
    )


def git(*args: str) -> str:
    """Run git command, return stdout."""
    r = run(["git", *args])
    return r.stdout.strip()


def run_tests() -> tuple[int, int, float, str]:
    """Run pytest, return (passed, failed, duration, raw_output)."""
    r = run(
        [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short", "-q"],
        timeout=300,
    )
    output = r.stdout + r.stderr

    # Parse results: "666 passed, 45 skipped in 1.64s"
    passed = failed = 0
    duration = 0.0

    m = re.search(r"(\d+) passed", output)
    if m:
        passed = int(m.group(1))

    m = re.search(r"(\d+) failed", output)
    if m:
        failed = int(m.group(1))

    # Count collection errors as failures (e.g. SyntaxError in source)
    m_errors = re.search(r"(\d+) error", output)
    if m_errors:
        failed += int(m_errors.group(1))

    # If no tests passed at all but no explicit failures, something is wrong
    if passed == 0 and failed == 0 and r.returncode != 0:
        failed = 1  # Force failure

    m = re.search(r"in ([\d.]+)s", output)
    if m:
        duration = float(m.group(1))

    return passed, failed, duration, output


def read_file(path: str) -> str:
    """Read a project file."""
    return (PROJECT_ROOT / path).read_text()


def write_file(path: str, content: str) -> None:
    """Write a project file."""
    (PROJECT_ROOT / path).write_text(content)


def get_file_overview() -> str:
    """Get a compact overview of the codebase structure."""
    lines = []
    for f in ALLOWED_FILES:
        p = PROJECT_ROOT / f
        line_count = len(p.read_text().splitlines())
        lines.append(f"  {f} ({line_count} lines)")
    return "\n".join(lines)


def _get_api_key() -> tuple[str, str]:
    """Get API key and provider type from config or env.

    Returns (api_key, provider) where provider is 'anthropic' or 'gemini'.
    """
    # Check env vars first
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key and anthropic_key.startswith("sk-ant-"):
        return anthropic_key, "anthropic"

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        return gemini_key, "gemini"

    # Read from config.json
    config_path = PROJECT_ROOT / "config.json"
    if not config_path.exists():
        print("ERROR: No API key found (set ANTHROPIC_API_KEY or GEMINI_API_KEY)")
        sys.exit(1)

    cfg = json.loads(config_path.read_text())

    # Check for Anthropic key (API key or OAuth token)
    for p in cfg.get("providers", []):
        if p.get("provider") == "anthropic":
            key = p.get("api_key", "")
            if key.startswith("sk-ant-"):
                return key, "anthropic"

    # Fall back to Gemini
    for p in cfg.get("providers", []):
        if p.get("provider") == "gemini":
            key = p.get("api_key", "")
            if key:
                return key, "gemini"

    # Check image_api_key (often Gemini)
    if cfg.get("image_api_key"):
        return cfg["image_api_key"], "gemini"

    print("ERROR: No valid API key found in config")
    sys.exit(1)


def call_llm(messages: list[dict], system: str = "") -> str:
    """Call Claude via CLI (claude -p) — uses Claude Code auth automatically."""
    # Build prompt from system + messages
    prompt_parts = []
    if system:
        prompt_parts.append(system)
        prompt_parts.append("\n---\n")
    for msg in messages:
        prompt_parts.append(msg.get("content", ""))

    full_prompt = "\n".join(prompt_parts)

    try:
        result = subprocess.run(
            ["claude", "-p", full_prompt, "--model", "claude-sonnet-4-6"],
            capture_output=True, text=True, timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            print(f"  Claude CLI error: {result.stderr[:200]}")
            return ""
        return result.stdout.strip()
    except FileNotFoundError:
        print("  ERROR: 'claude' CLI not found. Install: npm install -g @anthropic-ai/claude-code")
        return ""
    except subprocess.TimeoutExpired:
        print("  Claude CLI timed out (120s)")
        return ""


# ---------------------------------------------------------------------------
# Core experiment loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = dedent("""\
You are an expert Python developer performing automated code improvements
on the Qanot AI project — a Telegram bot agent framework.

Your goal: find ONE small, focused improvement and output a precise diff.

Rules:
1. Only modify files you are given. Do not add new files.
2. Each change must be small and focused (1-30 lines changed).
3. Changes must NOT break existing tests.
4. Output your change as a JSON object with this exact schema:
   {{
     "file": "qanot/example.py",
     "hypothesis": "Brief description of what you're improving and why",
     "original": "exact original code block to replace",
     "replacement": "new code to replace it with",
     "category": "quality|performance|security|reliability"
   }}
5. Output ONLY the JSON object, nothing else.
6. If you cannot find a meaningful improvement, output: {{"skip": true, "reason": "..."}}
""")

DEEP_SYSTEM_PROMPT = dedent("""\
You are a senior Python architect performing deep analysis on core files
of the Qanot AI project — a Telegram bot agent framework.

This is a CORE FILE — the most critical part of the system. Focus on changes
that have REAL IMPACT on users:

PRIORITIZE (high value):
- Real bugs: race conditions, data loss, crashes under load
- Logic errors: wrong behavior, incorrect state transitions
- Async issues: missing awaits, deadlocks, task leaks, uncancelled tasks
- Memory leaks: growing dicts, unclosed resources, unbounded caches
- Concurrency bugs: shared mutable state across users, missing locks
- Performance bottlenecks: O(n²) in hot paths, blocking I/O in async code

SKIP (low value):
- Cosmetic changes: renaming, reordering, adding comments
- Defensive coding for impossible scenarios
- Input validation for trusted internal data
- "Best practice" changes that don't fix real problems
- Adding type hints or docstrings

Rules:
1. Only modify the file you are given. Do not add new files.
2. Each change must be small and focused (1-30 lines changed).
3. Changes must NOT break existing tests.
4. Output your change as a JSON object with this exact schema:
   {{
     "file": "qanot/example.py",
     "hypothesis": "Brief description of what you're improving and why",
     "original": "exact original code block to replace",
     "replacement": "new code to replace it with",
     "category": "bug|performance|async|memory|concurrency|logic"
   }}
5. Output ONLY the JSON object, nothing else.
6. If you cannot find a MEANINGFUL improvement, output: {{"skip": true, "reason": "..."}}
   It is better to skip than to make a low-value change.
""")


def propose_change(
    focus: str,
    history: list[dict],
    round_num: int,
    total_rounds: int,
    core_mode: bool = False,
) -> dict | None:
    """Ask Claude to propose one improvement."""
    file_list = CORE_FILES if core_mode else ALLOWED_FILES
    overview = get_file_overview()

    # Pick a target file — rotate through files across rounds
    target_idx = round_num % len(file_list)
    target_file = file_list[target_idx]
    target_content = read_file(target_file)

    # Build history context (what was already tried)
    history_text = ""
    if history:
        recent = history[-10:]  # Last 10 experiments
        history_lines = []
        for h in recent:
            status = "KEPT" if h.get("kept") else "DISCARDED"
            history_lines.append(f"  [{status}] {h['file']}: {h['hypothesis']}")
        history_text = f"\nPrevious experiments:\n" + "\n".join(history_lines) + "\n"

    focus_desc = FOCUS_AREAS.get(focus, focus)

    if core_mode:
        user_msg = dedent(f"""\
        Round {round_num + 1}/{total_rounds}. DEEP ANALYSIS of core file.
        {history_text}
        Target file: {target_file}
        ```python
        {target_content}
        ```

        Find ONE real bug, performance issue, or logic error. Skip cosmetic changes.
        Output JSON only.
        """)
    else:
        user_msg = dedent(f"""\
        Round {round_num + 1}/{total_rounds}. Focus area: {focus_desc}
        {history_text}
        Project structure:
        {overview}

        Target file: {target_file}
        ```python
        {target_content}
        ```

        Find ONE focused improvement in this file. Output JSON only.
        """)

    prompt = DEEP_SYSTEM_PROMPT if core_mode else SYSTEM_PROMPT

    try:
        response = call_llm(
            [{"role": "user", "content": user_msg}],
            system=prompt,
        )

        # Extract JSON from response
        # Try to find JSON block
        json_match = re.search(r"\{[\s\S]*\}", response)
        if not json_match:
            return None

        data = json.loads(json_match.group())
        if data.get("skip"):
            print(f"  Skip: {data.get('reason', 'no reason')}")
            return None

        return data

    except (json.JSONDecodeError, Exception) as e:
        print(f"  Claude error: {e}")
        return None


def apply_change(change: dict) -> bool:
    """Apply a proposed change to the file. Returns True if applied."""
    filepath = change["file"]
    original = change["original"]
    replacement = change["replacement"]

    full_path = PROJECT_ROOT / filepath
    if not full_path.exists():
        print(f"  File not found: {filepath}")
        return False

    content = full_path.read_text()

    if original not in content:
        # Try with normalized whitespace
        normalized_content = content.replace("\r\n", "\n")
        normalized_original = original.replace("\r\n", "\n")
        if normalized_original not in normalized_content:
            print(f"  Original code block not found in {filepath}")
            return False
        content = normalized_content
        original = normalized_original

    new_content = content.replace(original, replacement, 1)
    if new_content == content:
        print(f"  No change after replacement")
        return False

    full_path.write_text(new_content)
    return True


def run_experiment(
    round_num: int,
    total_rounds: int,
    focus: str,
    history: list[dict],
    dry_run: bool = False,
    core_mode: bool = False,
) -> dict | None:
    """Run a single experiment: propose → apply → test → keep/discard."""
    print(f"\n{'='*60}")
    print(f"Round {round_num + 1}/{total_rounds}" + (" [CORE]" if core_mode else ""))
    print(f"{'='*60}")

    # 1. Propose a change
    print("  Proposing change...")
    change = propose_change(focus, history, round_num, total_rounds, core_mode=core_mode)
    if not change:
        print("  No change proposed, skipping")
        return None

    print(f"  File: {change['file']}")
    print(f"  Hypothesis: {change['hypothesis']}")
    print(f"  Category: {change.get('category', 'unknown')}")

    if dry_run:
        print("  [DRY RUN] Would apply change")
        return {
            "round": round_num + 1,
            "file": change["file"],
            "hypothesis": change["hypothesis"],
            "category": change.get("category", "unknown"),
            "kept": False,
            "dry_run": True,
        }

    # 2. Save original
    original_content = read_file(change["file"])

    # 3. Apply change
    print("  Applying change...")
    if not apply_change(change):
        return {
            "round": round_num + 1,
            "file": change["file"],
            "hypothesis": change["hypothesis"],
            "category": change.get("category", "unknown"),
            "kept": False,
            "reason": "apply_failed",
        }

    # 4. Run tests
    print("  Running tests...")
    passed, failed, duration, test_output = run_tests()
    print(f"  Results: {passed} passed, {failed} failed ({duration:.1f}s)")

    # 5. Evaluate
    if failed > 0:
        # Revert
        print("  FAILED — reverting")
        write_file(change["file"], original_content)
        return {
            "round": round_num + 1,
            "file": change["file"],
            "hypothesis": change["hypothesis"],
            "category": change.get("category", "unknown"),
            "kept": False,
            "reason": f"{failed} tests failed",
            "test_output": test_output[-500:],  # Last 500 chars
        }

    # All tests pass — keep the change!
    print("  KEPT — all tests pass")

    # Commit the change
    git("add", change["file"])
    git(
        "commit", "-m",
        f"autoresearch: {change['hypothesis']}\n\n"
        f"Category: {change.get('category', 'unknown')}\n"
        f"Round: {round_num + 1}/{total_rounds}\n"
        f"Tests: {passed} passed ({duration:.1f}s)",
    )

    return {
        "round": round_num + 1,
        "file": change["file"],
        "hypothesis": change["hypothesis"],
        "category": change.get("category", "unknown"),
        "kept": True,
        "tests_passed": passed,
        "duration": duration,
    }


def main():
    parser = argparse.ArgumentParser(description="Autoresearch for Qanot AI")
    parser.add_argument("--rounds", type=int, default=10, help="Number of experiment rounds")
    parser.add_argument("--focus", default="quality", choices=list(FOCUS_AREAS) + ["all"],
                        help="Focus area for improvements")
    parser.add_argument("--dry-run", action="store_true", help="Propose changes without applying")
    parser.add_argument("--core", action="store_true", help="Deep analysis of core files only (agent, telegram, context, memory, etc)")
    parser.add_argument("--branch", default="", help="Git branch name (auto-generated if empty)")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"run_{timestamp}.json"

    # Create experiment branch
    mode_label = "core" if args.core else args.focus
    branch_name = args.branch or f"{BRANCH_PREFIX}/{timestamp}_{mode_label}"
    if not args.dry_run:
        current_branch = git("branch", "--show-current")
        git("checkout", "-b", branch_name)
        print(f"Created branch: {branch_name}")

    # Baseline test
    print("Running baseline tests...")
    base_passed, base_failed, base_duration, _ = run_tests()
    print(f"Baseline: {base_passed} passed, {base_failed} failed ({base_duration:.1f}s)")

    if base_failed > 0:
        print("ERROR: Baseline tests fail. Fix before running autoresearch.")
        if not args.dry_run:
            git("checkout", current_branch)
            git("branch", "-D", branch_name)
        sys.exit(1)

    # Run experiments
    history: list[dict] = []
    focus_areas = list(FOCUS_AREAS) if args.focus == "all" else [args.focus]
    kept_count = 0
    skipped_count = 0
    failed_count = 0

    start_time = time.time()

    for i in range(args.rounds):
        focus = focus_areas[i % len(focus_areas)]

        result = run_experiment(
            round_num=i,
            total_rounds=args.rounds,
            focus=focus,
            history=history,
            dry_run=args.dry_run,
            core_mode=args.core,
        )

        if result is None:
            skipped_count += 1
            continue

        history.append(result)

        if result.get("kept"):
            kept_count += 1
        elif result.get("reason"):
            failed_count += 1
        else:
            skipped_count += 1

    elapsed = time.time() - start_time

    # Final report
    print(f"\n{'='*60}")
    print(f"AUTORESEARCH COMPLETE")
    print(f"{'='*60}")
    print(f"Rounds:   {args.rounds}")
    print(f"Kept:     {kept_count}")
    print(f"Failed:   {failed_count}")
    print(f"Skipped:  {skipped_count}")
    print(f"Duration: {elapsed:.0f}s ({elapsed/60:.1f}m)")
    print(f"Focus:    {args.focus}")

    if not args.dry_run:
        # Final test to make sure everything is good
        print("\nFinal verification...")
        final_passed, final_failed, final_duration, _ = run_tests()
        print(f"Final: {final_passed} passed, {final_failed} failed ({final_duration:.1f}s)")

        print(f"\nBranch: {branch_name}")
        print(f"Commits: {kept_count}")
        if kept_count > 0:
            print(f"\nTo review: git log {current_branch}..{branch_name}")
            print(f"To merge:  git checkout {current_branch} && git merge {branch_name}")
            print(f"To discard: git checkout {current_branch} && git branch -D {branch_name}")

    # Save log
    log_data = {
        "timestamp": timestamp,
        "rounds": args.rounds,
        "focus": args.focus,
        "kept": kept_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "duration_seconds": round(elapsed, 1),
        "baseline_tests": base_passed,
        "branch": branch_name if not args.dry_run else None,
        "experiments": history,
    }
    log_file.write_text(json.dumps(log_data, indent=2, ensure_ascii=False))
    print(f"\nLog saved: {log_file}")


if __name__ == "__main__":
    main()
