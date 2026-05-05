# Qanot Eval Harness

Behavior tests for the agent. Different from unit tests in `tests/` (which
check function logic) — these check **how the bot talks to users**:
vocabulary, hygiene, hallucinations, schema-term leakage, presentation
rules, and other things that don't have a clean function signature.

## How it works

1. **Cases** (`evals/cases/<domain>/*.yaml`) define test scenarios.
   Each case has a user message, optional context, and a rubric of
   pass/fail criteria with severity tiers (`critical` / `important` / `nice`).

2. **Recordings** (`evals/recordings/<domain>__<id>.json`) capture the
   agent's actual response to a case. Captured manually in v1 (paste
   from production); fixture-mode auto-capture in v2.

3. **Judge** (`evals/judge.py`) is Claude Sonnet running with a strict
   rubric prompt. Each rubric item is scored pass/fail/n/a; weighted
   score 0–100 produced; any critical fail = case fails.

4. **Runner** (`evals/runner.py`) orchestrates: load all cases, load
   each one's recording, run the judge in parallel, print a report.
   `--strict` exits 1 on any failure (CI mode).

## Running locally

```bash
# All cases
python -m evals.runner

# Filter
python -m evals.runner --case topkey
python -m evals.runner --tag regression

# Verbose (show passing rubric items, not just failures)
python -m evals.runner -v

# CI mode (exit 1 on failure)
python -m evals.runner --strict

# Machine-readable output for further processing
python -m evals.runner --json results.json
```

Requires `ANTHROPIC_API_KEY` in the environment.

Cost: each case = one Sonnet call (~500-2k tokens total). 6 cases ≈ $0.05/run.

## Adding a new case

1. Pick a domain — `topkey`, `absmarket`, `presentation`, `basic`, or
   create a new one (just create the directory). Use lowercase, no spaces.

2. Create `evals/cases/<domain>/<short_id>.yaml`:

   ```yaml
   id: my_test_case
   description: |
     One paragraph: what this test prevents from regressing.
   input:
     user_message: |
       Whatever the user actually types.
     context: |
       Optional. Background the judge needs to know — user role,
       prior turns summary, expected ground-truth answer if any.
   rubric:
     - severity: critical
       criterion: "Response does X (binary, narrow, easy to verify)"
     - severity: important
       criterion: "Response uses Y vocabulary"
     - severity: nice
       criterion: "Response is concise"
   tags: [domain, regression, presentation]
   ```

   **Rubric writing tips**:
   - One concrete idea per criterion. "Doesn't mention X" reads better
     than "Maintains professional tone."
   - `critical` = production-incident-class. Any fail blocks the PR.
   - `important` = quality bar. Weighted into score but not gating.
   - `nice` = polish. Tiny weight.

3. Capture a recording. Easiest: ask the bot the question on Telegram,
   copy the response, save to `evals/recordings/<domain>__<id>.json`:

   ```json
   {
     "case_id": "<domain>/<id>",
     "recorded_at": "2026-05-05T20:30:00+05:00",
     "agent_version": "<git-sha-or-branch>",
     "source": "where the recording came from",
     "notes": "any context — was this expected to pass or fail?",
     "response": "the bot's full reply, exactly as sent"
   }
   ```

4. Run the case to verify the judge behaves as expected:
   ```bash
   python -m evals.runner --case <domain>/<id> -v
   ```

   For regression tests, intentionally start with a recording that
   captures the failure mode you want to prevent. The judge should fail
   it. Then once the fix ships, re-record and the judge should pass.

## Calibration (v2)

The judge is currently uncalibrated — we trust Sonnet's judgement but
haven't measured it against human labels. To calibrate:

1. Have a human label 30+ cases (pass/fail per rubric item).
2. Run the judge on the same cases.
3. Compute Spearman correlation. Target ≥ 0.80.
4. If lower: tune the rubric language or judge prompt until it passes.

This is the difference between "we have an eval" and "we have an eval
we can trust." Worth doing before treating low-margin scores as truth.

## What's NOT in v1

- **Live agent invocation in CI** — recordings are captured manually.
  v2 will add a fixture-mode harness that runs the actual agent loop
  against mocked external services (topkey, absmarket, telegram).
  Until then, "did this PR change the agent's behavior?" requires
  re-recording affected cases by hand.
- **Production failure auto-pipeline** — when a user reports a bad
  response, manually distill it into a case + recording. Future:
  a queue from the dashboard.
- **Trajectory eval** — currently we score the final response. Tool
  call sequences (was the right tool used? in the right order?) need a
  separate eval surface, planned for v2.
