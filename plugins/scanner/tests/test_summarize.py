"""Period parsing + row aggregation + markdown formatting."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from engine.summarize import (  # noqa: E402
    PeriodError,
    PeriodRange,
    _parse_amount,
    _parse_row_date,
    filter_rows,
    format_amount,
    parse_period,
    summarize,
    summary_to_markdown,
)


TASHKENT_TZ = timezone(timedelta(hours=5))
FIXED_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=TASHKENT_TZ)


# ── parse_period ─────────────────────────────────────────────────


def test_today():
    r = parse_period("today", now=FIXED_NOW)
    assert r.start == date(2026, 4, 21)
    assert r.end == date(2026, 4, 21)
    assert r.days == 1


def test_yesterday():
    r = parse_period("yesterday", now=FIXED_NOW)
    assert r.start == date(2026, 4, 20)
    assert r.end == date(2026, 4, 20)


def test_week_is_last_7_days_including_today():
    r = parse_period("week", now=FIXED_NOW)
    assert r.start == date(2026, 4, 15)
    assert r.end == date(2026, 4, 21)
    assert r.days == 7


def test_month_starts_from_first():
    r = parse_period("month", now=FIXED_NOW)
    assert r.start == date(2026, 4, 1)
    assert r.end == date(2026, 4, 21)


def test_year_starts_from_jan_1():
    r = parse_period("year", now=FIXED_NOW)
    assert r.start == date(2026, 1, 1)
    assert r.end == date(2026, 4, 21)


def test_explicit_range():
    r = parse_period("2026-03-01..2026-03-31", now=FIXED_NOW)
    assert r.start == date(2026, 3, 1)
    assert r.end == date(2026, 3, 31)
    assert r.days == 31


def test_explicit_range_end_before_start_errors():
    with pytest.raises(PeriodError):
        parse_period("2026-04-30..2026-04-01")


def test_unknown_period_errors():
    with pytest.raises(PeriodError):
        parse_period("last-quarter")


def test_case_insensitive():
    r = parse_period("TODAY", now=FIXED_NOW)
    assert r.start == r.end == date(2026, 4, 21)


def test_prev_window_same_length():
    r = PeriodRange(date(2026, 4, 15), date(2026, 4, 21))
    prev = r.prev_window()
    assert prev.days == r.days == 7
    assert prev.end == date(2026, 4, 14)
    assert prev.start == date(2026, 4, 8)


# ── _parse_amount ────────────────────────────────────────────────


def test_parse_integer():
    assert _parse_amount(150000) == 150000.0


def test_parse_float_direct():
    assert _parse_amount(150000.5) == 150000.5


def test_parse_uzbek_thousands_comma():
    # '150,000' in Uzbek style should parse as 150000, not 150.0
    assert _parse_amount("150,000") == 150000.0


def test_parse_uzbek_thousands_space():
    assert _parse_amount("150 000") == 150000.0


def test_parse_european_decimal_comma():
    # '150,50' (European decimal) — ambiguous, we prefer thousands interpretation
    # when right side has 3 digits. With only 2 right-side digits it's treated as decimal.
    assert _parse_amount("150,50") == 150.50


def test_parse_mixed_separators():
    assert _parse_amount("1,500.75") == 1500.75


def test_parse_empty_returns_none():
    assert _parse_amount("") is None
    assert _parse_amount(None) is None
    assert _parse_amount("abc") is None


# ── filter_rows ──────────────────────────────────────────────────


@pytest.fixture
def sample_rows():
    """Schema: Sana | Do'kon | Summa | Valyuta | Kategoriya | Izoh | Mahsulotlar"""
    return [
        ["Sana", "Do'kon", "Summa", "Valyuta", "Kategoriya", "Izoh", "Mahsulotlar"],
        ["2026-04-21", "Korzinka", 156000, "UZS", "Oziq-ovqat", "", "non; sut"],
        ["2026-04-21", "Yandex Go", 25000, "UZS", "Transport", "", ""],
        ["2026-04-20", "Evos", 45000, "UZS", "Restoran", "", ""],
        ["2026-04-15", "Korzinka", 120000, "UZS", "Oziq-ovqat", "", ""],
        ["2026-04-01", "Uztelecom", 200000, "UZS", "Kommunal", "aprel", ""],
        ["2026-03-28", "Korzinka", 90000, "UZS", "Oziq-ovqat", "", ""],
        ["2026-04-20", "Amazon", 35, "USD", "Tovar", "kitob", ""],
    ]


def test_filter_skips_header(sample_rows):
    r = PeriodRange(date(2026, 4, 21), date(2026, 4, 21))
    filtered = filter_rows(sample_rows, r)
    # Header is auto-skipped; only the 2 rows from 2026-04-21
    assert len(filtered) == 2
    vendors = {row[1] for row in filtered}
    assert vendors == {"Korzinka", "Yandex Go"}


def test_filter_inclusive_end(sample_rows):
    r = PeriodRange(date(2026, 4, 20), date(2026, 4, 21))
    filtered = filter_rows(sample_rows, r)
    assert len(filtered) == 4  # 2 on 04-21 + 2 on 04-20


def test_filter_by_category(sample_rows):
    r = PeriodRange(date(2026, 4, 1), date(2026, 4, 30))
    filtered = filter_rows(sample_rows, r, category="Oziq-ovqat")
    assert len(filtered) == 2
    assert all(row[4] == "Oziq-ovqat" for row in filtered)


# ── summarize ────────────────────────────────────────────────────


def test_summarize_month_totals(sample_rows):
    r = PeriodRange(date(2026, 4, 1), date(2026, 4, 30))
    s = summarize(sample_rows, r)
    assert s.entry_count == 6  # all April rows (excluding March 28 + header)
    assert s.total_by_currency["UZS"] == 156000 + 25000 + 45000 + 120000 + 200000
    assert s.total_by_currency["USD"] == 35


def test_summarize_by_category_breakdown(sample_rows):
    r = PeriodRange(date(2026, 4, 1), date(2026, 4, 30))
    s = summarize(sample_rows, r)
    assert s.by_category["Oziq-ovqat"]["UZS"] == 156000 + 120000
    assert s.by_category["Transport"]["UZS"] == 25000
    assert s.by_category["Tovar"]["USD"] == 35


def test_summarize_top_transactions_sorted(sample_rows):
    r = PeriodRange(date(2026, 4, 1), date(2026, 4, 30))
    s = summarize(sample_rows, r, top_n=3)
    assert len(s.top_transactions) == 3
    # Biggest amount first
    assert s.top_transactions[0]["vendor"] == "Uztelecom"
    assert s.top_transactions[0]["amount"] == 200000


def test_summarize_prev_window_delta(sample_rows):
    # Period: week 04-15 to 04-21 (7 days)
    r = PeriodRange(date(2026, 4, 15), date(2026, 4, 21))
    # Previous 7 days: 04-08 to 04-14 — no rows in fixture
    s = summarize(sample_rows, r, prev_rows=sample_rows)
    # Current week has 4 UZS rows: 156k + 25k + 45k + 120k
    assert s.total_by_currency["UZS"] == 346000
    # Previous week (04-08 to 04-14) has none — the March 28 row is outside
    assert s.prev_total_by_currency.get("UZS", 0) == 0


def test_summarize_empty_period_returns_zero():
    r = PeriodRange(date(2099, 1, 1), date(2099, 1, 31))
    s = summarize([["Sana", "Do'kon", "Summa"]], r)
    assert s.entry_count == 0
    assert s.total_by_currency == {}


# ── format_amount ────────────────────────────────────────────────


def test_format_uzs_integer_drops_decimals():
    assert format_amount(150000, "UZS") == "150,000 so'm"


def test_format_uzs_with_decimals():
    assert format_amount(150000.75, "UZS") == "150,000.75 so'm"


def test_format_usd_currency_symbol():
    assert format_amount(35.50, "USD") == "$35.50"


def test_format_unknown_currency_code_suffix():
    assert format_amount(100, "RUB") == "100.00 RUB"


# ── summary_to_markdown smoke ────────────────────────────────────


def test_markdown_contains_uzbek_labels(sample_rows):
    r = PeriodRange(date(2026, 4, 1), date(2026, 4, 30))
    s = summarize(sample_rows, r)
    md = summary_to_markdown(s)
    assert "Davr" in md
    assert "Jami" in md
    assert "Kategoriya" in md
    assert "so'm" in md  # UZS should format with so'm


def test_markdown_empty_period():
    r = PeriodRange(date(2099, 1, 1), date(2099, 1, 31))
    s = summarize([["Sana", "Do'kon", "Summa"]], r)
    md = summary_to_markdown(s)
    assert "xarajat yo'q" in md
