"""Tests for daily cost budget enforcement."""

import pytest

from qanot.context import CostTracker


@pytest.fixture
def tracker(tmp_path):
    return CostTracker(str(tmp_path))


class TestDailyBudget:
    """Test daily cost budget tracking and enforcement."""

    def test_no_budget_always_allowed(self, tracker):
        allowed, spent, budget = tracker.check_budget("user1", 0.0)
        assert allowed is True

    def test_under_budget_allowed(self, tracker):
        tracker.add_usage("user1", cost=0.05)
        allowed, spent, budget = tracker.check_budget("user1", 1.0)
        assert allowed is True
        assert abs(spent - 0.05) < 0.001

    def test_over_budget_blocked(self, tracker):
        tracker.add_usage("user1", cost=1.50)
        allowed, spent, budget = tracker.check_budget("user1", 1.0)
        assert allowed is False
        assert spent >= 1.0

    def test_exact_budget_blocked(self, tracker):
        tracker.add_usage("user1", cost=1.0)
        allowed, _, _ = tracker.check_budget("user1", 1.0)
        assert allowed is False

    def test_multiple_users_independent(self, tracker):
        tracker.add_usage("user1", cost=2.0)
        tracker.add_usage("user2", cost=0.1)
        assert tracker.check_budget("user1", 1.0)[0] is False
        assert tracker.check_budget("user2", 1.0)[0] is True

    def test_daily_cost_accumulates(self, tracker):
        tracker.add_usage("user1", cost=0.3)
        tracker.add_usage("user1", cost=0.4)
        tracker.add_usage("user1", cost=0.4)
        allowed, spent, _ = tracker.check_budget("user1", 1.0)
        assert allowed is False
        assert abs(spent - 1.1) < 0.001


class TestBudgetWarning:
    """Test budget warning messages."""

    def test_no_budget_no_warning(self, tracker):
        assert tracker.get_budget_warning("user1", 0.0) is None

    def test_low_usage_no_warning(self, tracker):
        tracker.add_usage("user1", cost=0.1)
        assert tracker.get_budget_warning("user1", 1.0) is None

    def test_warning_at_80_percent(self, tracker):
        tracker.add_usage("user1", cost=0.85)
        warning = tracker.get_budget_warning("user1", 1.0, warning_pct=80)
        assert warning is not None
        assert "85%" in warning

    def test_exceeded_message(self, tracker):
        tracker.add_usage("user1", cost=1.5)
        warning = tracker.get_budget_warning("user1", 1.0)
        assert warning is not None
        assert "tugadi" in warning

    def test_custom_warning_threshold(self, tracker):
        tracker.add_usage("user1", cost=0.5)
        # 50% of $1.0 — should not warn at 80%
        assert tracker.get_budget_warning("user1", 1.0, warning_pct=80) is None
        # Should warn at 40%
        assert tracker.get_budget_warning("user1", 1.0, warning_pct=40) is not None
