"""Monthly aggregation - critical for credit underwriting cash-flow view.

Outputs per-month metrics:
  - Total Cr Amount, Cr Txn Count
  - Total Dr Amount, Dr Txn Count
  - Bank Charges (₹ + count)
  - Cheque/ECS Returns (count, separated by inward/outward)
  - Opening, Closing balance
  - ABB (Avg) for the month
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Tuple, List, Optional
from parsers.base import ParsedStatement
from analyzers.returns import detect_cheque_returns


@dataclass
class MonthlyStats:
    year: int
    month: int
    cr_amount: float = 0.0
    cr_count: int = 0
    dr_amount: float = 0.0
    dr_count: int = 0
    bank_charges_amount: float = 0.0
    bank_charges_count: int = 0
    inward_returns_count: int = 0
    outward_returns_count: int = 0
    ecs_nach_returns_count: int = 0
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    days_in_period: int = 0


# Bank charge narration patterns (broad detection for monthly aggregation)
_CHARGE_PATTERNS = re.compile(
    r"\b(?:"
    r"BANK CHARGES?|SERVICE CHARGE|SMS CHARGES|SMS-USG-CRG|"
    r"DCARDFEE|DEBIT CARD FEE|ANNUAL FEE|CARD FEE|"
    r"VETTING CHARGE|TDS U/S 194N|"
    r"CHARGES FOR|Charges for PORD|"
    r"IMPS Chg|NEFT CHARGES|RTGS CHARGES|N chg|"
    r"GST ON|CGST|SGST|IGST|"
    r"NACH (CHARGES|FEE|FAILURE)|ECS (CHARGES|FEE)|"
    r"CHQ (BOOK|RETURN|RETN) CHARGES?|"
    r"AMC|ACCOUNT MAINTENANCE"
    r")\b",
    re.IGNORECASE,
)


def is_bank_charge(narration: str, amount: float = 0) -> bool:
    """Return True if narration looks like a bank-imposed charge.
    Small fixed amounts (<100) are more likely charges, but we don't gate on that.
    """
    if not narration:
        return False
    return bool(_CHARGE_PATTERNS.search(narration))


def compute_monthly_summary(stmt: ParsedStatement) -> List[MonthlyStats]:
    """Build a chronologically sorted list of MonthlyStats covering the statement period."""
    by_month: Dict[Tuple[int, int], MonthlyStats] = {}

    # Detect returns once and bucket by month
    returns = detect_cheque_returns(stmt)
    return_by_month = defaultdict(lambda: {"INWARD": 0, "OUTWARD": 0, "ECS_NACH": 0})
    for r in returns:
        if r.is_charge:
            continue
        key = (r.date.year, r.date.month)
        if r.return_type == "INWARD":
            return_by_month[key]["INWARD"] += 1
        elif r.return_type == "OUTWARD":
            return_by_month[key]["OUTWARD"] += 1
        else:
            return_by_month[key]["ECS_NACH"] += 1

    # Walk transactions once
    for t in stmt.transactions:
        key = (t.date.year, t.date.month)
        if key not in by_month:
            by_month[key] = MonthlyStats(year=t.date.year, month=t.date.month)
        m = by_month[key]
        if t.debit:
            m.dr_amount += t.debit
            m.dr_count += 1
            if is_bank_charge(t.particulars, t.debit):
                m.bank_charges_amount += t.debit
                m.bank_charges_count += 1
        if t.credit:
            m.cr_amount += t.credit
            m.cr_count += 1

    # Stamp opening/closing balances for each month
    txns_by_month = defaultdict(list)
    for t in stmt.transactions:
        txns_by_month[(t.date.year, t.date.month)].append(t)

    for key, m in by_month.items():
        txns = txns_by_month[key]
        if not txns:
            continue
        first = txns[0]
        last = txns[-1]
        # Opening = balance after first txn + first.debit - first.credit
        m.opening_balance = first.balance - (first.credit or 0) + (first.debit or 0)
        m.closing_balance = last.balance
        m.days_in_period = (last.date - first.date).days + 1

    # Fill in returns counts
    for key, counts in return_by_month.items():
        if key in by_month:
            by_month[key].inward_returns_count = counts["INWARD"]
            by_month[key].outward_returns_count = counts["OUTWARD"]
            by_month[key].ecs_nach_returns_count = counts["ECS_NACH"]

    # Return sorted
    return sorted(by_month.values(), key=lambda m: (m.year, m.month))
