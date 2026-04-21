#!/usr/bin/env python3
"""Canonical benchmark: memory-injection vs. context-editing tradeoffs.

Replays a fixed turn set against two provider configurations and reports
tokens, cost, latency, and cache behaviour. The whole point is to answer
ONE question with data instead of vibes:

    Does `inject_legacy_memory=False` + `context_editing_enabled=True`
    net-save tokens and latency at realistic scale?

Designed to be rerun after config or model changes.

Usage:
    # Full run — both configs, shared conversation (measures cache effects)
    ANTHROPIC_API_KEY=... PYTHONPATH=. python3 scripts/bench_memory.py

    # Isolate one config
    ... python3 scripts/bench_memory.py --configs production

    # Short smoke test
    ... python3 scripts/bench_memory.py --turns 3

Runs inside a throwaway workspace (no effect on /data/workspace).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bench")
log.setLevel(logging.INFO)


# ── Canonical turn set ─────────────────────────────────────────────

_RAW_TURNS = [
    # ── Memory recall (15) ─────────────────────────────────────────
    ("recall",    "men nimani yoqtiraman?"),
    ("recall",    "qaysi hujjat formatini afzal ko'raman?"),
    ("recall",    "video uchun qaysi aspect ratio ishlataman?"),
    ("recall",    "what's my name in this bot?"),
    ("recall",    "sevimli rangim nima edi?"),
    ("recall",    "what do you know about my work preferences?"),
    ("recall",    "qisqa videolar uchun odatda qancha klip so'rayman?"),
    ("recall",    "what language do I usually speak in?"),
    ("recall",    "bozor savollarida qanday ma'lumot kutaman?"),
    ("recall",    "am I a morning or evening person?"),
    ("recall",    "qaysi narxlarni doim kuzataman?"),
    ("recall",    "how do I prefer you to communicate?"),
    ("recall",    "qaysi formatni afzal ko'rardim hujjat uchun?"),
    ("recall",    "remember what I told you about python vs javascript?"),
    ("recall",    "nima ish qilaman odatda?"),

    # ── Memory write (10) ──────────────────────────────────────────
    ("write",     "eslab qol: mening sevimli raqamim 42"),
    ("write",     "remember: i like python more than javascript"),
    ("write",     "eslab qol: har dushanba kuni soat 10 da meeting bor"),
    ("write",     "to'g'irlash: men keksa emas, 28 yoshdaman"),
    ("write",     "save this: my office is on the 5th floor"),
    ("write",     "eslab qol: men dokumentlarni doim WhatsApp emas Telegram orqali olaman"),
    ("write",     "remember i hate spicy food"),
    ("write",     "eslab qol: asosiy mijozim AKFA kompaniyasi"),
    ("write",     "remember: prefer bullet points over paragraphs"),
    ("write",     "eslab qol: hafta oxirida ish qilmayman"),

    # ── Tool/task use (15) ─────────────────────────────────────────
    ("task",      "salom"),
    ("task",      "soat nima?"),
    ("task",      "17 + 23 qancha bo'ladi?"),
    ("task",      "3 ta qisqa gap yoz Uzbekistan haqida"),
    ("task",      "rahmat"),
    ("task",      "hozir ob-havo qanday Toshkentda?"),
    ("task",      "100 ga 7 foiz qo'sh"),
    ("task",      "list 5 common uzbek names"),
    ("task",      "ertaga nima qilishim kerak?"),
    ("task",      "how many days until new year?"),
    ("task",      "bugun qaysi kun?"),
    ("task",      "tell me a very short joke in uzbek"),
    ("task",      "what's 250 * 4?"),
    ("task",      "hayot mazmuni haqida bir jumla"),
    ("task",      "give me a random motivational quote"),

    # ── Follow-ups (10) — references + clarifications ─────────────
    ("followup",  "birinchisini qayta ko'rsatgin"),
    ("followup",  "rahmat"),
    ("followup",  "va yana bitta"),
    ("followup",  "oldingisiga o'xshash"),
    ("followup",  "ok tushundim"),
    ("followup",  "that's correct"),
    ("followup",  "keyingi"),
    ("followup",  "yaxshi, davom et"),
    ("followup",  "qaytarib ber"),
    ("followup",  "noto'g'ri, qaytadan"),
]


def _canonical_turns(seed: int = 42) -> list[tuple[str, str]]:
    """Shuffle turns with a fixed seed so the order is realistic-interleaved
    (recall / write / task / followup mixed) but reproducible across runs."""
    import random
    rng = random.Random(seed)
    shuffled = list(_RAW_TURNS)
    rng.shuffle(shuffled)
    return shuffled


CANONICAL_TURNS = _canonical_turns()


# ── Configs under test ─────────────────────────────────────────────

CONFIGS = {
    "baseline": {
        "inject_legacy_memory": True,
        "context_editing_enabled": False,
        "label": "MEMORY.md injected, no context editing (current default)",
    },
    "production": {
        "inject_legacy_memory": False,
        "context_editing_enabled": True,
        "label": "memory_tool only + context editing (hypothesis)",
    },
}


# ── Pricing (Claude Sonnet 4.6, USD per MTok) ──────────────────────

PRICING = {
    "input":         3.00,
    "cache_read":    0.30,
    "cache_write":   3.75,
    "output":       15.00,
}


# ── Minimal memory tool handler (view/create only — enough for bench) ──

def _handle_memory_tool(cmd_input: dict, memories_dir: Path) -> str:
    cmd = cmd_input.get("command", "")
    path_str = cmd_input.get("path", "")
    # Resolve safely inside memories_dir
    if path_str.startswith("/memories"):
        rel = path_str[len("/memories"):].lstrip("/")
    else:
        rel = path_str.lstrip("/")
    try:
        target = (memories_dir / rel).resolve()
        target.relative_to(memories_dir.resolve())
    except (ValueError, OSError):
        return f"Error: invalid path {path_str}"

    if cmd == "view":
        if not target.exists():
            return f"Error: path {path_str} does not exist."
        if target.is_dir():
            items = []
            for p in sorted(target.iterdir()):
                items.append(f"{'dir' if p.is_dir() else 'file'}\t{p.name}")
            return "\n".join(items) or "(empty)"
        try:
            content = target.read_text(encoding="utf-8")
        except Exception as e:
            return f"Error reading: {e}"
        return content[:4000]
    if cmd == "create":
        text = cmd_input.get("file_text", "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return f"File created: {path_str}"
    if cmd == "str_replace":
        old_s = cmd_input.get("old_str", "")
        new_s = cmd_input.get("new_str", "")
        if not target.exists():
            return f"Error: {path_str} does not exist"
        t = target.read_text(encoding="utf-8")
        if old_s not in t:
            return f"Error: old_str not found"
        target.write_text(t.replace(old_s, new_s, 1), encoding="utf-8")
        return "Edit applied"
    return f"(bench: skipping unsupported command '{cmd}')"


# ── Per-turn runner ────────────────────────────────────────────────

@dataclass
class TurnMetric:
    idx: int
    category: str
    text: str
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    latency_ms: float = 0.0
    tool_calls: int = 0
    tools_used: list[str] = field(default_factory=list)
    error: str = ""
    # Captured response text + grading (populated when --grade is used).
    response_text: str = ""
    score: int = 0          # 1-5 (0 = ungraded / errored)
    score_reason: str = ""


async def run_turn(
    provider: Any,
    system: str,
    conv_messages: list[dict],
    user_msg: str,
    category: str,
    idx: int,
    memories_dir: Path,
    max_iters: int = 6,
) -> TurnMetric:
    m = TurnMetric(idx=idx, category=category, text=user_msg)
    conv_messages.append({"role": "user", "content": user_msg})
    tools = [{"type": "memory_20250818", "name": "memory"}]

    t0 = time.perf_counter()
    for _ in range(max_iters):
        try:
            resp = await provider.chat(
                messages=conv_messages, tools=tools, system=system,
            )
        except Exception as e:
            m.error = f"{type(e).__name__}: {e}"[:200]
            break

        m.api_calls += 1
        u = resp.usage
        m.input_tokens   += u.input_tokens
        m.output_tokens  += u.output_tokens
        m.cache_read     += getattr(u, "cache_read_input_tokens", 0) or 0
        m.cache_write    += getattr(u, "cache_creation_input_tokens", 0) or 0

        if resp.tool_calls:
            # Record assistant message with tool_use blocks
            blocks: list[dict] = []
            if resp.content:
                blocks.append({"type": "text", "text": resp.content})
            for tc in resp.tool_calls:
                blocks.append({
                    "type": "tool_use", "id": tc.id,
                    "name": tc.name, "input": tc.input,
                })
                m.tool_calls += 1
                m.tools_used.append(tc.name)
            conv_messages.append({"role": "assistant", "content": blocks})

            # Handle each tool call
            tool_results: list[dict] = []
            for tc in resp.tool_calls:
                if tc.name == "memory":
                    result = _handle_memory_tool(tc.input, memories_dir)
                else:
                    result = "(bench: non-memory tool skipped — result would be empty)"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result,
                })
            conv_messages.append({"role": "user", "content": tool_results})
            continue

        # Final text response — append and stop
        final_text = resp.content or ""
        conv_messages.append({"role": "assistant", "content": final_text})
        m.response_text = final_text
        break
    else:
        m.error = f"hit max_iters={max_iters} without final response"

    m.latency_ms = (time.perf_counter() - t0) * 1000
    return m


# ── Workspace setup ────────────────────────────────────────────────

def _bootstrap_workspace(src_ws: Path, dst_ws: Path) -> None:
    """Copy enough files from the real workspace so recall questions have
    something to retrieve. If no src available, create a minimal stub."""
    dst_ws.mkdir(parents=True, exist_ok=True)
    (dst_ws / "memories").mkdir(exist_ok=True)
    for name in ("MEMORY.md", "SESSION-STATE.md", "IDENTITY.md", "SOUL.md"):
        s = src_ws / name
        if s.exists():
            shutil.copy(s, dst_ws / name)
    for sub in ("memories", "memory"):
        s = src_ws / sub
        if s.exists() and s.is_dir():
            d = dst_ws / sub
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d)

    # Minimal stubs if the source had nothing
    if not (dst_ws / "MEMORY.md").exists():
        (dst_ws / "MEMORY.md").write_text(
            "# MEMORY.md\n\n## User Profile\n"
            "- Prefers DOCX format for documents\n"
            "- Interested in gold (XAUUSD) and oil prices\n"
            "- Uses 9:16 vertical format for videos\n"
        )


async def run_config(
    config_name: str,
    config_spec: dict,
    turns: list[tuple[str, str]],
    workspace: Path,
) -> list[TurnMetric]:
    """Instantiate a provider with the spec and run the turn set."""
    from qanot.config import load_config
    from qanot.providers.anthropic import AnthropicProvider
    from qanot.prompt import build_system_prompt

    cfg = load_config()

    provider = AnthropicProvider(
        api_key=cfg.api_key,
        model=cfg.model,
        thinking_level="off",
        memory_tool=True,
        context_editing=config_spec["context_editing_enabled"],
        context_editing_trigger_tokens=cfg.context_editing_trigger_tokens,
        context_editing_keep_tool_uses=cfg.context_editing_keep_tool_uses,
        context_editing_clear_at_least_tokens=cfg.context_editing_clear_at_least_tokens,
    )

    # System prompt: use the real builder so we capture SOUL/IDENTITY/tools etc.
    system = build_system_prompt(
        workspace_dir=str(workspace),
        owner_name="bench",
        bot_name="bench-bot",
        timezone_str=cfg.timezone,
        inject_legacy_memory=config_spec["inject_legacy_memory"],
    )

    memories_dir = workspace / "memories"
    conv_messages: list[dict] = []
    results: list[TurnMetric] = []
    for i, (cat, text) in enumerate(turns, start=1):
        m = await run_turn(provider, system, conv_messages, text, cat, i, memories_dir)
        results.append(m)
        log.info(
            "[%s] %02d/%d (%s) calls=%d in=%d out=%d cache_r=%d tools=%d %.0fms %s",
            config_name, i, len(turns), cat, m.api_calls, m.input_tokens,
            m.output_tokens, m.cache_read, m.tool_calls, m.latency_ms,
            f"ERR: {m.error}" if m.error else "",
        )
    return results


# ── Correctness grading (LLM judge) ────────────────────────────────

JUDGE_MODEL = "claude-haiku-4-5-20251001"
JUDGE_SYSTEM = """You are an impartial evaluator of an AI assistant's replies to a Telegram user.

You rate QUALITY on a 1-5 scale:
  5 — excellent: complete, factually correct, well-formed
  4 — good: mostly correct, minor omission or rough edge
  3 — acceptable: partial answer, some things right, others wrong
  2 — weak: largely wrong, unhelpful, or irrelevant
  1 — bad: wrong answer, confused, or refused when it shouldn't

Category-specific rubrics:
  recall  — the user is asking about THEIR OWN facts. The assistant should
            surface concrete preferences / habits / identity items.
            A vague "I don't know" is 2. A clear personalised answer = 5.
  write   — the user stated a new durable fact and said "remember it".
            The assistant should acknowledge and commit to remembering.
            Committing + confirming the fact = 5. Generic reply = 3.
  task    — the user asked for a factual answer or simple action. Math
            must be exact. Greetings must be returned naturally.
  followup — depends on context. A naturally-contextual reply = 4+.
            Confused/repeated = 2.

The user speaks Uzbek, English, and occasionally Russian. Replies in ANY
of those languages are fine; score the CONTENT, not the language choice.

Output ONLY a single-line JSON object, no markdown, no commentary:
{"score": <int 1-5>, "reason": "<≤15 words>"}"""


async def grade_turn(judge, m: TurnMetric) -> tuple[int, str]:
    """Ask Haiku to grade a single (question, answer, category) triple."""
    if m.error:
        return 0, f"ungraded (turn errored: {m.error[:80]})"
    if not m.response_text.strip():
        return 1, "ungraded: empty response"

    user_prompt = (
        f"Category: {m.category}\n"
        f"User question: {m.text}\n"
        f"Assistant answer:\n{m.response_text[:2000]}"
    )
    try:
        resp = await judge.chat(
            messages=[{"role": "user", "content": user_prompt}],
            tools=None,
            system=JUDGE_SYSTEM,
        )
    except Exception as e:
        return 0, f"judge error: {type(e).__name__}: {str(e)[:80]}"

    raw = (resp.content or "").strip()
    # Strip markdown fences if the judge slipped them in
    for fence in ("```json", "```"):
        if raw.startswith(fence):
            raw = raw[len(fence):]
        if raw.endswith("```"):
            raw = raw[:-3]
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
        score = int(parsed.get("score", 0))
        reason = str(parsed.get("reason", ""))[:200]
        if score < 1 or score > 5:
            return 0, f"judge returned out-of-range score: {score}"
        return score, reason
    except Exception as e:
        return 0, f"judge output unparseable: {raw[:80]!r}"


async def grade_all(metrics: list[TurnMetric]) -> None:
    """Grade every turn in-place. Runs with bounded concurrency."""
    from qanot.config import load_config
    from qanot.providers.anthropic import AnthropicProvider

    cfg = load_config()
    judge = AnthropicProvider(
        api_key=cfg.api_key,
        model=JUDGE_MODEL,
        thinking_level="off",
        memory_tool=False,
        context_editing=False,
    )
    sem = asyncio.Semaphore(5)

    async def _grade_one(m: TurnMetric) -> None:
        async with sem:
            score, reason = await grade_turn(judge, m)
            m.score = score
            m.score_reason = reason

    await asyncio.gather(*(_grade_one(m) for m in metrics))


# ── Reporting ──────────────────────────────────────────────────────

def cost(m: TurnMetric) -> float:
    # Anthropic returns input_tokens = fresh (uncached) input; cache_read
    # and cache_write are separate buckets. Do not subtract.
    return (
        m.input_tokens   * PRICING["input"]        / 1e6
        + m.cache_read   * PRICING["cache_read"]   / 1e6
        + m.cache_write  * PRICING["cache_write"]  / 1e6
        + m.output_tokens * PRICING["output"]      / 1e6
    )


def _summary_row(name: str, metrics: list[TurnMetric]) -> dict:
    latencies = [m.latency_ms for m in metrics if not m.error]
    graded = [m for m in metrics if m.score > 0]
    by_cat: dict[str, list[int]] = {}
    for m in graded:
        by_cat.setdefault(m.category, []).append(m.score)
    cat_means = {
        cat: round(sum(scores) / len(scores), 2)
        for cat, scores in by_cat.items()
    }
    return {
        "config": name,
        "turns": len(metrics),
        "errors": sum(1 for m in metrics if m.error),
        "total_input": sum(m.input_tokens for m in metrics),
        "total_output": sum(m.output_tokens for m in metrics),
        "total_cache_read": sum(m.cache_read for m in metrics),
        "total_cache_write": sum(m.cache_write for m in metrics),
        "total_api_calls": sum(m.api_calls for m in metrics),
        "total_tool_calls": sum(m.tool_calls for m in metrics),
        "total_cost_usd": round(sum(cost(m) for m in metrics), 4),
        "p50_latency_ms": round(statistics.median(latencies), 0) if latencies else 0,
        "p95_latency_ms": (
            round(statistics.quantiles(latencies, n=20)[-1], 0)
            if len(latencies) >= 20 else
            (round(max(latencies), 0) if latencies else 0)
        ),
        "mean_score": (
            round(sum(m.score for m in graded) / len(graded), 2)
            if graded else 0.0
        ),
        "graded_count": len(graded),
        "by_category_score": cat_means,
    }


def _print_comparison(summaries: dict[str, dict]) -> None:
    cols = [
        ("config",           16),
        ("turns",             6),
        ("errors",            6),
        ("total_api_calls",  10),
        ("total_tool_calls", 10),
        ("total_input",      14),
        ("total_cache_read", 16),
        ("total_output",     12),
        ("total_cost_usd",   14),
        ("p50_latency_ms",   14),
        ("p95_latency_ms",   14),
        ("mean_score",       10),
    ]
    header = " | ".join(f"{k:<{w}}" for k, w in cols)
    print("\n" + header)
    print("-" * len(header))
    for name, s in summaries.items():
        row = " | ".join(f"{str(s.get(k, '')):<{w}}" for k, w in cols)
        print(row)

    # Per-category score breakdown if grading ran
    cat_table_rows = []
    for name, s in summaries.items():
        cats = s.get("by_category_score") or {}
        if cats:
            cat_table_rows.append((name, cats))
    if cat_table_rows:
        all_cats = sorted({c for _, cats in cat_table_rows for c in cats})
        header2 = "config            | " + " | ".join(f"{c:<8}" for c in all_cats)
        print("\n" + header2)
        print("-" * len(header2))
        for name, cats in cat_table_rows:
            row = f"{name:<17} | " + " | ".join(
                f"{cats.get(c, '-'):<8}" for c in all_cats
            )
            print(row)
    print()


# ── Main ───────────────────────────────────────────────────────────

async def amain(args: argparse.Namespace) -> int:
    real_ws = Path(args.source_workspace) if args.source_workspace else Path("/data/workspace")

    with tempfile.TemporaryDirectory(prefix="qanot-bench-") as td:
        ws = Path(td) / "workspace"
        _bootstrap_workspace(real_ws, ws)
        log.info("Bench workspace bootstrapped at %s", ws)

        turn_set = CANONICAL_TURNS[: args.turns] if args.turns else CANONICAL_TURNS
        log.info("Running %d turns per config", len(turn_set))

        configs_to_run = [c for c in (args.configs.split(",") if args.configs else CONFIGS) if c in CONFIGS]
        all_metrics: dict[str, list[TurnMetric]] = {}
        for name in configs_to_run:
            log.info("=== Config: %s — %s ===", name, CONFIGS[name]["label"])
            all_metrics[name] = await run_config(
                name, CONFIGS[name], turn_set, ws,
            )

    # Grading pass (optional, parallel across turns, judge=Haiku).
    if args.grade:
        for name, metrics in all_metrics.items():
            log.info("Grading %d responses for config=%s (judge=%s)…",
                     len(metrics), name, JUDGE_MODEL)
            await grade_all(metrics)
            n_ok = sum(1 for m in metrics if m.score >= 4)
            log.info("  %d/%d responses scored ≥4", n_ok, len(metrics))

    # Summary + JSON dump
    summaries = {name: _summary_row(name, ms) for name, ms in all_metrics.items()}
    _print_comparison(summaries)

    out = {
        "configs": {n: CONFIGS[n] for n in configs_to_run},
        "turn_count": len(turn_set),
        "summaries": summaries,
        "per_turn": {
            name: [m.__dict__ for m in ms]
            for name, ms in all_metrics.items()
        },
    }
    # Prefer the repo's scripts/ dir; fall back to cwd when run from elsewhere.
    scripts_dir = PROJECT_ROOT / "scripts"
    base = scripts_dir if scripts_dir.is_dir() else Path.cwd()
    out_path = base / f"bench_memory_{int(time.time())}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"Detailed results: {out_path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=0, help="Limit to first N turns (default: all)")
    ap.add_argument("--configs", type=str, default="", help="Comma-sep subset of: baseline,production")
    ap.add_argument("--source-workspace", type=str, default="", help="Workspace to seed bench from (defaults to /data/workspace)")
    ap.add_argument("--grade", action="store_true", help="Grade answer quality per turn with Haiku (1-5)")
    args = ap.parse_args()
    sys.exit(asyncio.run(amain(args)))
