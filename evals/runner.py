"""Eval runner — orchestrates golden cases + recordings + judge → report.

Usage:
    python -m evals.runner                      # run all cases
    python -m evals.runner --case topkey/no_taxminiy_label
    python -m evals.runner --tag regression
    python -m evals.runner --strict             # exit 1 on any failure (CI mode)
    python -m evals.runner --json out.json      # machine-readable output

Each case is a YAML file under evals/cases/<domain>/<id>.yaml. Each case
expects a recording at evals/recordings/<domain>__<id>.json containing the
agent's actual response. The judge scores the recording against the
rubric in the case file.

Recordings are captured separately (manually for v1, via a fixture-mode
agent harness in v2). This separation lets the judge run in CI without
needing to invoke the agent (which requires API keys, network, plugins).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evals.judge import JudgeVerdict, RubricItem, judge

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

EVALS_DIR = Path(__file__).resolve().parent
CASES_DIR = EVALS_DIR / "cases"
RECORDINGS_DIR = EVALS_DIR / "recordings"


@dataclass
class Case:
    id: str
    domain: str  # subdir name (topkey/absmarket/presentation/basic)
    description: str
    user_message: str
    rubric: list[RubricItem]
    tags: list[str] = field(default_factory=list)
    context: str = ""
    path: Path | None = None

    @property
    def full_id(self) -> str:
        return f"{self.domain}/{self.id}"

    @property
    def recording_filename(self) -> str:
        return f"{self.domain}__{self.id}.json"


@dataclass
class CaseRun:
    case: Case
    verdict: JudgeVerdict | None = None
    error: str = ""

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if self.verdict is None:
            return "skipped"
        return self.verdict.verdict


def load_cases() -> list[Case]:
    cases: list[Case] = []
    for path in sorted(CASES_DIR.rglob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            logger.error("YAML parse error in %s: %s", path, e)
            continue
        rubric_items: list[RubricItem] = []
        for r in data.get("rubric", []):
            severity = r.get("severity", "important")
            if severity not in ("critical", "important", "nice"):
                logger.warning("Unknown severity %r in %s — defaulting to 'important'", severity, path)
                severity = "important"
            rubric_items.append(RubricItem(criterion=r["criterion"], severity=severity))
        domain = path.parent.name
        cases.append(Case(
            id=data["id"],
            domain=domain,
            description=data.get("description", ""),
            user_message=data["input"]["user_message"],
            context=data["input"].get("context", ""),
            rubric=rubric_items,
            tags=data.get("tags", []),
            path=path,
        ))
    return cases


def load_recording(case: Case) -> str | None:
    """Read the recorded agent response for this case. Returns None if missing."""
    rec_path = RECORDINGS_DIR / case.recording_filename
    if not rec_path.exists():
        return None
    try:
        data = json.loads(rec_path.read_text(encoding="utf-8"))
        return data.get("response", "")
    except json.JSONDecodeError as e:
        logger.error("Recording %s is invalid JSON: %s", rec_path, e)
        return None


def run_case(case: Case) -> CaseRun:
    response = load_recording(case)
    if response is None:
        return CaseRun(case=case, error=f"recording missing: {case.recording_filename}")
    if not response.strip():
        return CaseRun(case=case, error="recording has empty response")
    try:
        verdict = judge(
            case_id=case.full_id,
            user_message=case.user_message,
            response=response,
            rubric=case.rubric,
        )
        return CaseRun(case=case, verdict=verdict)
    except Exception as e:
        logger.exception("Judge failed for %s", case.full_id)
        return CaseRun(case=case, error=f"judge exception: {e}")


def filter_cases(cases: list[Case], *, case_filter: str | None, tag: str | None) -> list[Case]:
    if case_filter:
        cases = [c for c in cases if case_filter in c.full_id]
    if tag:
        cases = [c for c in cases if tag in c.tags]
    return cases


def format_report(runs: list[CaseRun], *, verbose: bool = False) -> str:
    lines: list[str] = []
    lines.append("=== Eval results ===\n")
    width = max((len(r.case.full_id) for r in runs), default=20)
    pass_count = fail_count = error_count = 0
    total_score = 0.0
    scored = 0
    for r in runs:
        name = r.case.full_id.ljust(width + 2)
        if r.error:
            lines.append(f"  ⚠ {name}  ERROR: {r.error}")
            error_count += 1
            continue
        v = r.verdict
        assert v is not None
        score_str = f"{v.score:5.1f}/100"
        if v.verdict == "pass":
            mark = "✓"
            pass_count += 1
        elif v.verdict == "judge_error":
            mark = "⚠"
            error_count += 1
        else:
            mark = "✗"
            fail_count += 1
        if v.verdict != "judge_error":
            total_score += v.score
            scored += 1
        lines.append(f"  {mark} {name}  {score_str}  {v.verdict}")
        if v.summary and (verbose or v.verdict != "pass"):
            lines.append(f"      → {v.summary}")
        if verbose or v.verdict != "pass":
            for item in v.rubric_results:
                if item.result == "fail":
                    sev = item.severity[0].upper()  # C/I/N
                    lines.append(f"      [{sev}] FAIL: {item.criterion}")
                    if item.reason:
                        lines.append(f"             reason: {item.reason}")
    lines.append("")
    total = len(runs)
    avg = (total_score / scored) if scored else 0.0
    lines.append(f"  {pass_count} passed · {fail_count} failed · {error_count} errors  ({total} cases, avg {avg:.1f}/100)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="Filter to cases whose full_id contains this string")
    parser.add_argument("--tag", help="Filter to cases with this tag")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 if any case fails (for CI gating)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show passing rubric items too")
    parser.add_argument("--json", dest="json_out",
                        help="Write machine-readable results to this path")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel judge calls (each is one Anthropic API call)")
    args = parser.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        logger.error("ANTHROPIC_API_KEY not set")
        return 2

    cases = load_cases()
    cases = filter_cases(cases, case_filter=args.case, tag=args.tag)
    if not cases:
        logger.error("no cases matched the filter")
        return 2

    logger.info("Running %d cases through judge (model: claude-sonnet-4-6)", len(cases))
    runs: list[CaseRun] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(run_case, c): c for c in cases}
        for fut in as_completed(futures):
            runs.append(fut.result())
    runs.sort(key=lambda r: r.case.full_id)

    print(format_report(runs, verbose=args.verbose))

    if args.json_out:
        json_data = {
            "results": [
                {
                    "case_id": r.case.full_id,
                    "status": r.status,
                    "error": r.error,
                    "verdict": asdict(r.verdict) if r.verdict else None,
                }
                for r in runs
            ],
        }
        Path(args.json_out).write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Wrote machine-readable results to %s", args.json_out)

    if args.strict:
        had_failure = any(r.status in ("fail", "error") for r in runs)
        return 1 if had_failure else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
