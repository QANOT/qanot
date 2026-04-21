"""Period aggregation for expense_summary tool.

Reads rows from a Google Sheets expense sheet (expected schema:
Sana | Do'kon | Summa | Valyuta | Kategoriya | Izoh | Mahsulotlar)
and produces a summary dict:

  - total per currency
  - breakdown per category (amount + % of total per currency)
  - top 5 transactions by absolute amount (per currency)
  - period-over-period delta (same length window immediately before)

Period strings accepted:
  - "today"           - from midnight Asia/Tashkent
  - "yesterday"       - previous 24h
  - "week"            - last 7 days including today
  - "month"           - from 1st of the current month
  - "year"            - from Jan 1 of the current year
  - "YYYY-MM-DD..YYYY-MM-DD" - explicit range (inclusive)

Number formatting is Uzbek style: 150,000 so'm (thousands separator with
comma, currency name spelled out in UZS case).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

TASHKENT_TZ = timezone(timedelta(hours=5))

# Index positions matching the expense sheet schema
SANA = 0
DOKON = 1
SUMMA = 2
VALYUTA = 3
KATEGORIYA = 4
IZOH = 5
MAHSULOTLAR = 6


class PeriodError(ValueError):
    """Unparseable period string."""


@dataclass
class PeriodRange:
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def prev_window(self) -> "PeriodRange":
        """Same-length window immediately before this one."""
        d = self.days
        new_end = self.start - timedelta(days=1)
        new_start = new_end - timedelta(days=d - 1)
        return PeriodRange(new_start, new_end)


def parse_period(period: str, *, now: datetime | None = None) -> PeriodRange:
    """Normalize a period string to a concrete date range.

    `now` injectable for tests. Defaults to current time in Asia/Tashkent.
    """
    today = (now or datetime.now(TASHKENT_TZ)).date()
    p = (period or "").strip().lower()

    if p == "today":
        return PeriodRange(today, today)
    if p == "yesterday":
        y = today - timedelta(days=1)
        return PeriodRange(y, y)
    if p == "week":
        return PeriodRange(today - timedelta(days=6), today)
    if p == "month":
        return PeriodRange(today.replace(day=1), today)
    if p == "year":
        return PeriodRange(today.replace(month=1, day=1), today)

    if ".." in p:
        left, _, right = p.partition("..")
        try:
            start = date.fromisoformat(left.strip())
            end = date.fromisoformat(right.strip())
        except ValueError as e:
            raise PeriodError(
                f"Range period must be YYYY-MM-DD..YYYY-MM-DD, got {period!r}"
            ) from e
        if end < start:
            raise PeriodError(
                f"Period end {end} before start {start}; swap them."
            )
        return PeriodRange(start, end)

    raise PeriodError(
        f"Unknown period {period!r}. "
        f"Use: today, yesterday, week, month, year, or YYYY-MM-DD..YYYY-MM-DD."
    )


def _parse_amount(cell: Any) -> float | None:
    """Turn a Sheets cell into a float, accepting Uzbek/Russian number forms.

    Handles: '150,000', '150 000', '150000', '150000.50', '150,000.50'.
    Returns None if unparseable (e.g. empty cell, header row).
    """
    if cell is None or cell == "":
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    s = str(cell).strip()
    if not s:
        return None
    # Strip whitespace and currency-ish chars, keep digits + . , -
    cleaned = s.replace(" ", "").replace(" ", "")
    # Heuristic: if comma is the ONLY separator and is followed by exactly 3
    # digits → thousands separator (150,000). Otherwise treat as decimal.
    if "," in cleaned and "." not in cleaned:
        left, _, right = cleaned.rpartition(",")
        if len(right) == 3 and right.isdigit() and left.replace(",", "").replace("-", "").isdigit():
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_row_date(cell: Any) -> date | None:
    """Accept ISO YYYY-MM-DD or Sheets 'serial' numbers (skip latter).

    Sheets typed-as-date cells usually come back as strings in our get
    call, so ISO parsing covers the default case.
    """
    if cell is None or cell == "":
        return None
    s = str(cell).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def filter_rows(
    rows: list[list[Any]],
    period: PeriodRange,
    *,
    category: str | None = None,
) -> list[list[Any]]:
    """Return rows where date is in [period.start, period.end] and,
    optionally, category matches (case-insensitive).

    Skips the header row (index 0) by heuristic: if the first cell in row
    0 can't be parsed as a date, we skip it.
    """
    if not rows:
        return []
    data = rows
    first_date = _parse_row_date(rows[0][SANA]) if rows[0] else None
    if first_date is None:
        # Header detected — skip row 0
        data = rows[1:]

    out: list[list[Any]] = []
    wanted_cat = (category or "").strip().lower() or None
    for row in data:
        if not row or len(row) <= SUMMA:
            continue
        d = _parse_row_date(row[SANA])
        if d is None:
            continue
        if d < period.start or d > period.end:
            continue
        if wanted_cat:
            row_cat = (
                str(row[KATEGORIYA]).strip().lower()
                if len(row) > KATEGORIYA
                else ""
            )
            if row_cat != wanted_cat:
                continue
        out.append(row)
    return out


@dataclass
class Summary:
    period: PeriodRange
    total_by_currency: dict[str, float]
    by_category: dict[str, dict[str, float]]  # {category: {currency: total}}
    top_transactions: list[dict]  # compact row dicts
    entry_count: int
    prev_total_by_currency: dict[str, float]  # for delta reporting


def summarize(
    rows: list[list[Any]],
    period: PeriodRange,
    prev_rows: list[list[Any]] | None = None,
    *,
    category: str | None = None,
    top_n: int = 5,
) -> Summary:
    """Aggregate filtered rows over `period`. Optional `prev_rows` for delta.

    `rows` MUST already be the full sheet fetch (unfiltered) — we filter
    here so downstream tests can pass raw fixture data.
    """
    current = filter_rows(rows, period, category=category)
    by_currency: dict[str, float] = defaultdict(float)
    by_category: dict[str, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )

    for row in current:
        amount = _parse_amount(row[SUMMA])
        if amount is None:
            continue
        currency = (
            str(row[VALYUTA]).strip().upper()
            if len(row) > VALYUTA and row[VALYUTA]
            else "UZS"
        )
        cat = (
            str(row[KATEGORIYA]).strip()
            if len(row) > KATEGORIYA and row[KATEGORIYA]
            else "Boshqa"
        )
        by_currency[currency] += amount
        by_category[cat][currency] += amount

    # Top transactions — sorted by absolute amount, keep compact dict form
    enriched: list[tuple[float, dict]] = []
    for row in current:
        amount = _parse_amount(row[SUMMA])
        if amount is None:
            continue
        enriched.append(
            (
                abs(amount),
                {
                    "date": str(row[SANA]) if len(row) > SANA else "",
                    "vendor": str(row[DOKON]) if len(row) > DOKON else "",
                    "amount": amount,
                    "currency": (
                        str(row[VALYUTA]).upper()
                        if len(row) > VALYUTA and row[VALYUTA]
                        else "UZS"
                    ),
                    "category": (
                        str(row[KATEGORIYA])
                        if len(row) > KATEGORIYA and row[KATEGORIYA]
                        else "Boshqa"
                    ),
                },
            )
        )
    enriched.sort(key=lambda x: x[0], reverse=True)
    top = [e[1] for e in enriched[:top_n]]

    # Previous period for delta
    prev_totals: dict[str, float] = defaultdict(float)
    if prev_rows is not None:
        prev = filter_rows(prev_rows, period.prev_window(), category=category)
        for row in prev:
            amount = _parse_amount(row[SUMMA])
            if amount is None:
                continue
            currency = (
                str(row[VALYUTA]).strip().upper()
                if len(row) > VALYUTA and row[VALYUTA]
                else "UZS"
            )
            prev_totals[currency] += amount

    return Summary(
        period=period,
        total_by_currency=dict(by_currency),
        by_category={k: dict(v) for k, v in by_category.items()},
        top_transactions=top,
        entry_count=len(current),
        prev_total_by_currency=dict(prev_totals),
    )


def format_amount(amount: float, currency: str = "UZS") -> str:
    """Uzbek-style formatting: 150,000 so'm; 1,500 USD."""
    # UZS rarely has decimals in practice — drop them if amount is integer-like
    if currency.upper() == "UZS":
        if abs(amount - round(amount)) < 0.01:
            return f"{int(round(amount)):,} so'm"
        return f"{amount:,.2f} so'm"
    if currency.upper() == "USD":
        return f"${amount:,.2f}"
    if currency.upper() == "EUR":
        return f"€{amount:,.2f}"
    # Unknown currency — trailing code
    return f"{amount:,.2f} {currency.upper()}"


def summary_to_markdown(summary: Summary) -> str:
    """Human-readable report in Uzbek."""
    lines: list[str] = []
    days = summary.period.days
    period_label = (
        f"{summary.period.start.isoformat()}"
        if summary.period.start == summary.period.end
        else f"{summary.period.start.isoformat()} — {summary.period.end.isoformat()} "
        f"({days} kun)"
    )
    lines.append(f"**Davr:** {period_label}")
    lines.append(f"**Yozuvlar:** {summary.entry_count} ta")
    lines.append("")

    if not summary.total_by_currency:
        lines.append("_Bu davrda xarajat yo'q._")
        return "\n".join(lines)

    # Totals per currency with delta
    lines.append("**Jami:**")
    for cur, total in sorted(summary.total_by_currency.items()):
        prev = summary.prev_total_by_currency.get(cur, 0.0)
        delta_str = ""
        if prev > 0:
            change = total - prev
            pct = (change / prev) * 100
            sign = "+" if change >= 0 else ""
            delta_str = f" (oldingi davrdan {sign}{pct:.1f}%)"
        lines.append(f"- {format_amount(total, cur)}{delta_str}")

    # By category
    if summary.by_category:
        lines.append("")
        lines.append("**Kategoriya bo'yicha:**")
        for cat, by_cur in sorted(
            summary.by_category.items(),
            key=lambda x: -sum(x[1].values()),
        ):
            chunks = [format_amount(v, c) for c, v in sorted(by_cur.items())]
            lines.append(f"- {cat}: {', '.join(chunks)}")

    # Top transactions
    if summary.top_transactions:
        lines.append("")
        lines.append("**Eng katta xaridlar:**")
        for t in summary.top_transactions:
            lines.append(
                f"- {t['date']} — {t['vendor']} — "
                f"{format_amount(t['amount'], t['currency'])} "
                f"({t['category']})"
            )

    return "\n".join(lines)
