"""Detect cheque/ECS/NACH returns - critical NBFC red flag indicator.

Indian banking narrations for returns vary by bank:
- HDFC:  "CHQ RETURN MEMO", "CHQ INW RET", "RETURN MEMO"
- ICICI: "CHEQUE RETURN", "INWARD CHQ RETURN", "OUTWARD RETURN"
- SBI:   "RETURN OF CHEQUE", "CHQ RTN"
- BOB:   "RETURN CHEQUE", "CHQ RETURN"
- Axis:  "RTN CHQ", "CHEQUE RETURN CHARGES"

Inward = cheque YOU deposited bounced back (you received less money)
Outward = cheque YOU issued bounced (your beneficiary didn't get paid)

ECS/NACH returns = mandate bounce (typically EMI failure - very serious)
"""
import re
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Tuple
from parsers.base import ParsedStatement, Transaction


@dataclass
class ChequeReturn:
    date: date
    return_type: str          # "INWARD" / "OUTWARD" / "ECS_RETURN" / "NACH_RETURN"
    amount: float
    narration: str
    is_charge: bool = False   # True if this is a return CHARGE (penalty), not the bounce itself
    cheque_no: Optional[str] = None


# Pattern -> return type
RETURN_PATTERNS = [
    # ICICI retail: cheque deposited by account-holder bounced (drawer had no funds)
    # "REJECT:301822:FUNDS INSUFFICIENT" -> inward return
    (re.compile(r"^REJECT:\d+", re.I), "INWARD"),
    (re.compile(r"\bFUNDS\s*INSUFFICIENT\b", re.I), "INWARD"),
    # ICICI retail return charge: "RTN CHG-301822/FUNDS INSUFFICIENT/02.04.26"
    (re.compile(r"^RTN\s*CHG", re.I), "INWARD"),

    # Inward returns (you received a cheque that bounced)
    (re.compile(r"\bINWARD\b.*\b(CHEQUE|CHQ|RETURN|RETN|RTN)\b", re.I), "INWARD"),
    (re.compile(r"\b(CHQ|CHEQUE)\b.*\bINW\b.*\b(RET|RETURN|RTN)\b", re.I), "INWARD"),
    (re.compile(r"\bRETURN\b.*\bINWARD\b", re.I), "INWARD"),
    (re.compile(r"\bCHQ INW RET\b", re.I), "INWARD"),

    # Outward returns (your issued cheque bounced)
    (re.compile(r"\bOUTWARD\b.*\b(CHEQUE|CHQ|RETURN|RETN|RTN)\b", re.I), "OUTWARD"),
    (re.compile(r"\b(CHQ|CHEQUE)\b.*\bOUT\b.*\b(RET|RETURN|RTN)\b", re.I), "OUTWARD"),

    # ECS / NACH / ACH returns (mandate bounce - EMI failure)
    (re.compile(r"\bECS\b.*\b(RETURN|RETN|RTN|FAIL)\b", re.I), "ECS_RETURN"),
    (re.compile(r"\bNACH\b.*\b(RETURN|RETN|RTN|FAIL)\b", re.I), "NACH_RETURN"),
    (re.compile(r"\bACH\b.*\b(RETURN|RETN|RTN|FAIL)\b", re.I), "ACH_RETURN"),
    (re.compile(r"\bMANDATE\b.*\b(FAIL|RETURN|RETN)\b", re.I), "NACH_RETURN"),

    # Generic patterns (less specific - check last)
    (re.compile(r"\bRETURN MEMO\b", re.I), "OUTWARD"),
    (re.compile(r"\b(CHQ|CHEQUE)\s*(RETURN|RETN|RTN)\b", re.I), "OUTWARD"),
    (re.compile(r"\bDISHONOUR", re.I), "OUTWARD"),
    (re.compile(r"\bINSUFF.*(BAL|FUND)\b", re.I), "OUTWARD"),
    (re.compile(r"\bBOUNCE\b", re.I), "OUTWARD"),
    (re.compile(r"\bDR REVERSAL\b", re.I), "OUTWARD"),
]

# Charge patterns (penalty after bounce, not the bounce itself)
CHARGE_INDICATORS = re.compile(
    r"\b(CHARGES?|CHRG|CHG|FEE|PENALTY|GST|CGST|SGST|IGST)\b",
    re.I,
)


def detect_cheque_returns(stmt: ParsedStatement) -> List[ChequeReturn]:
    returns: List[ChequeReturn] = []
    for t in stmt.transactions:
        n = t.particulars or ""
        rtype = None
        for pat, label in RETURN_PATTERNS:
            if pat.search(n):
                rtype = label
                break
        if not rtype:
            continue
        # Is this the actual return or the charge?
        is_charge = bool(CHARGE_INDICATORS.search(n))
        amount = t.debit or t.credit or 0.0
        returns.append(ChequeReturn(
            date=t.date,
            return_type=rtype,
            amount=amount,
            narration=n,
            is_charge=is_charge,
            cheque_no=t.cheque_no,
        ))
    return returns


def returns_summary(returns: List[ChequeReturn]) -> dict:
    """Aggregate counts and amounts by return type (excluding charges)."""
    actual_returns = [r for r in returns if not r.is_charge]
    summary = {
        "inward_count": sum(1 for r in actual_returns if r.return_type == "INWARD"),
        "inward_amount": sum(r.amount for r in actual_returns if r.return_type == "INWARD"),
        "outward_count": sum(1 for r in actual_returns if r.return_type == "OUTWARD"),
        "outward_amount": sum(r.amount for r in actual_returns if r.return_type == "OUTWARD"),
        "ecs_nach_count": sum(1 for r in actual_returns if r.return_type in ("ECS_RETURN", "NACH_RETURN", "ACH_RETURN")),
        "ecs_nach_amount": sum(r.amount for r in actual_returns if r.return_type in ("ECS_RETURN", "NACH_RETURN", "ACH_RETURN")),
        "total_charges": sum(r.amount for r in returns if r.is_charge),
        "charge_count": sum(1 for r in returns if r.is_charge),
    }
    summary["total_count"] = summary["inward_count"] + summary["outward_count"] + summary["ecs_nach_count"]
    summary["total_amount"] = summary["inward_amount"] + summary["outward_amount"] + summary["ecs_nach_amount"]
    return summary
