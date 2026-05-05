"""Unit tests for the eval harness — judge scoring logic + case loading.

Doesn't call the real Anthropic API; mocks the client where needed.
For the live judge behaviour test, run `python -m evals.runner` with
ANTHROPIC_API_KEY set.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evals.judge import (
    JudgeVerdict, RubricItem, RubricItemResult,
    _parse_judge_output, _score_from_results, judge,
)
from evals import runner


# ── _parse_judge_output ─────────────────────────────────────────


def test_parse_plain_json():
    raw = '{"rubric_results": [], "summary": "ok"}'
    assert _parse_judge_output(raw) == {"rubric_results": [], "summary": "ok"}


def test_parse_strips_code_fences():
    raw = '```json\n{"rubric_results": [], "summary": "fenced"}\n```'
    assert _parse_judge_output(raw) == {"rubric_results": [], "summary": "fenced"}


def test_parse_strips_unmarked_fences():
    raw = '```\n{"rubric_results": [], "summary": "x"}\n```'
    assert _parse_judge_output(raw)["summary"] == "x"


def test_parse_invalid_json_raises():
    with pytest.raises((ValueError,)):
        _parse_judge_output("not json")


# ── _score_from_results ─────────────────────────────────────────


def test_score_all_pass():
    results = [
        RubricItemResult("a", "critical", "pass", ""),
        RubricItemResult("b", "important", "pass", ""),
        RubricItemResult("c", "nice", "pass", ""),
    ]
    assert _score_from_results(results) == 100.0


def test_score_critical_fail_zeros_out():
    results = [
        RubricItemResult("a", "critical", "fail", ""),
        RubricItemResult("b", "important", "pass", ""),
        RubricItemResult("c", "nice", "pass", ""),
    ]
    assert _score_from_results(results) == 0.0


def test_score_important_fail_partial():
    results = [
        RubricItemResult("a", "critical", "pass", ""),
        RubricItemResult("b", "important", "fail", ""),
    ]
    # weights: critical=1.0 (pass=earned), important=0.5 (fail=not earned)
    # earned=1.0, total=1.5 → 66.7
    assert _score_from_results(results) == 66.7


def test_score_na_excluded_from_denominator():
    results = [
        RubricItemResult("a", "critical", "pass", ""),
        RubricItemResult("b", "important", "n/a", ""),
        RubricItemResult("c", "nice", "n/a", ""),
    ]
    # Only critical=1.0 counts. earned=1.0, total=1.0 → 100
    assert _score_from_results(results) == 100.0


def test_score_empty_results_is_full():
    """No criteria evaluated = no signal of failure → don't fail by default."""
    assert _score_from_results([]) == 100.0


# ── case loading ─────────────────────────────────────────────────


def test_load_real_cases_succeeds():
    """Smoke test: the YAML files in evals/cases/ all parse and have rubrics."""
    cases = runner.load_cases()
    assert len(cases) >= 4, f"expected ≥4 cases shipped, got {len(cases)}"
    for c in cases:
        assert c.id, f"case has empty id: {c.path}"
        assert c.user_message, f"case {c.full_id} has empty user_message"
        assert c.rubric, f"case {c.full_id} has no rubric items"
        for item in c.rubric:
            assert item.severity in ("critical", "important", "nice")
            assert item.criterion.strip(), f"empty criterion in {c.full_id}"


def test_recordings_match_at_least_some_cases():
    """At least the cases we shipped should have recordings."""
    cases = runner.load_cases()
    recordings_dir = runner.RECORDINGS_DIR
    found = 0
    for c in cases:
        if (recordings_dir / c.recording_filename).exists():
            found += 1
    assert found >= 4, f"expected ≥4 recordings to ship with the harness, got {found}"


def test_recording_files_are_valid_json():
    for path in runner.RECORDINGS_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "response" in data, f"{path} missing 'response' field"
        assert isinstance(data["response"], str)


# ── judge with mocked client ────────────────────────────────────


def _mock_client(json_response: str) -> MagicMock:
    """Build a mock anthropic.Anthropic client that returns json_response
    as the model's text reply."""
    client = MagicMock()
    msg = MagicMock()
    block = MagicMock()
    block.text = json_response
    msg.content = [block]
    client.messages.create.return_value = msg
    return client


def test_judge_passes_when_all_rubric_pass():
    client = _mock_client(json.dumps({
        "rubric_results": [
            {"criterion": "Says salom", "result": "pass", "reason": "yes"},
        ],
        "summary": "all good",
    }))
    rubric = [RubricItem("Says salom", "critical")]
    v = judge("test/case", "Hi", "Salom!", rubric, client=client)
    assert v.verdict == "pass"
    assert v.score == 100.0
    assert len(v.rubric_results) == 1
    assert v.rubric_results[0].result == "pass"


def test_judge_fails_on_critical():
    client = _mock_client(json.dumps({
        "rubric_results": [
            {"criterion": "No tool names leaked", "result": "fail",
             "reason": "mentioned topkey_get_task"},
            {"criterion": "Uzbek language", "result": "pass", "reason": ""},
        ],
        "summary": "leaked tool name",
    }))
    rubric = [
        RubricItem("No tool names leaked", "critical"),
        RubricItem("Uzbek language", "important"),
    ]
    v = judge("test/case", "msg", "I called topkey_get_task...", rubric, client=client)
    assert v.verdict == "fail"
    assert v.score == 0.0
    assert v.has_critical_failure


def test_judge_fills_in_missing_rubric_items_as_na():
    client = _mock_client(json.dumps({
        "rubric_results": [
            {"criterion": "First", "result": "pass", "reason": ""},
            # Judge forgot the second criterion entirely
        ],
        "summary": "partial",
    }))
    rubric = [
        RubricItem("First", "important"),
        RubricItem("Second", "important"),
    ]
    v = judge("test/case", "msg", "resp", rubric, client=client)
    assert len(v.rubric_results) == 2
    second = next(r for r in v.rubric_results if r.criterion == "Second")
    assert second.result == "n/a"


def test_judge_handles_unparseable_output():
    client = _mock_client("not json at all, just prose")
    # _mock_client returns the same response every time, so all retries fail.
    rubric = [RubricItem("anything", "critical")]
    v = judge("test/case", "msg", "resp", rubric, client=client)
    assert v.verdict == "judge_error"
    assert v.score == 0.0


def test_judge_ignores_unknown_criterion_from_judge():
    """Defensive: if the judge invents a criterion not in the rubric,
    we drop it rather than crash."""
    client = _mock_client(json.dumps({
        "rubric_results": [
            {"criterion": "Says salom", "result": "pass", "reason": ""},
            {"criterion": "Made-up criterion", "result": "fail", "reason": ""},
        ],
        "summary": "judge invented a criterion",
    }))
    rubric = [RubricItem("Says salom", "important")]
    v = judge("test/case", "Hi", "Salom!", rubric, client=client)
    # Only 1 valid result kept; "Made-up" dropped silently.
    assert len(v.rubric_results) == 1
    assert v.rubric_results[0].criterion == "Says salom"
