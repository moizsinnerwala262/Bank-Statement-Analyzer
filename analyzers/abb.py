"""ABB (Average Bank Balance) calculator."""
from datetime import date, timedelta
from calendar import monthrange
from dataclasses import dataclass
from typing import List, Dict, Tuple
from parsers.base import ParsedStatement


@dataclass
class ABBResult:
    daily_eod: Dict[date, float]
    is_carry_fwd: Dict[date, bool]
    months: List[Tuple[int, int]]
    period_start: date
    period_end: date
    overall_abb: float
    overall_min: float
    overall_max: float
    days_below_1000: int
    days_below_500: int
    days_carry_fwd: int


def compute_abb(stmt: ParsedStatement) -> ABBResult:
    txns = stmt.transactions
    if not txns:
        raise ValueError("No transactions to analyze")

    period_start = stmt.metadata.period_from or txns[0].date
    period_end = stmt.metadata.period_to or txns[-1].date
    opening = stmt.metadata.opening_balance
    if opening is None:
        # fall back to first txn balance reverse-engineered, else 0
        first = txns[0]
        opening = first.balance + (first.debit or 0) - (first.credit or 0)

    # Last balance of each day (txns already chronological from parser)
    last_bal_by_date: Dict[date, float] = {}
    for t in txns:
        if t.balance is not None:
            last_bal_by_date[t.date] = t.balance

    daily_eod: Dict[date, float] = {}
    is_carry_fwd: Dict[date, bool] = {}
    prev = opening
    cur = period_start
    while cur <= period_end:
        if cur in last_bal_by_date:
            daily_eod[cur] = last_bal_by_date[cur]
            is_carry_fwd[cur] = False
        else:
            daily_eod[cur] = prev
            is_carry_fwd[cur] = True
        prev = daily_eod[cur]
        cur += timedelta(days=1)

    months: List[Tuple[int, int]] = []
    cur = date(period_start.year, period_start.month, 1)
    while cur <= period_end:
        months.append((cur.year, cur.month))
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)

    balances = list(daily_eod.values())
    return ABBResult(
        daily_eod=daily_eod,
        is_carry_fwd=is_carry_fwd,
        months=months,
        period_start=period_start,
        period_end=period_end,
        overall_abb=sum(balances) / len(balances),
        overall_min=min(balances),
        overall_max=max(balances),
        days_below_1000=sum(1 for b in balances if b < 1000),
        days_below_500=sum(1 for b in balances if b < 500),
        days_carry_fwd=sum(1 for v in is_carry_fwd.values() if v),
    )
