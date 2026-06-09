"""Approximate loan principal estimation from EMI data.

This is approximation, not exact. We don't know the original principal, interest rate,
tenure, or start date. We use industry-standard assumptions by loan-type heuristic.

Methodology:
1. Classify loan type from lender keywords + EMI amount tier.
2. Apply rate/tenure ranges typical for that loan type.
3. Reverse-EMI math: P = EMI × [(1+r)^n - 1] / [r × (1+r)^n]
4. Report low/mid/high principal estimate so the user sees the uncertainty.

LOAN_PROFILES uses live-market ranges as of 2024-25. Update these constants if rates shift.

KEY DISCLAIMERS (always shown to user):
- Assumes fixed-rate, reducing-balance amortization.
- Tenure assumed - not derived from data (we don't see loan start date).
- Rate assumed - actual rate could be ±2-3% from these bands.
- Original principal estimate; current outstanding is lower (paid down).
"""
import math
import re
from dataclasses import dataclass
from typing import Optional, List
from analyzers.emi import DetectedEMI


# Live-market rates and typical tenures for Indian retail/SME credit (2024-25).
LOAN_PROFILES = {
    "vehicle_cv": {
        "rate_low": 0.095, "rate_high": 0.13,
        "tenure_low": 48, "tenure_high": 60,
        "label": "Commercial Vehicle (Truck/Bus)",
    },
    "vehicle_pv": {
        "rate_low": 0.085, "rate_high": 0.11,
        "tenure_low": 60, "tenure_high": 84,
        "label": "Personal Vehicle (Car)",
    },
    "two_wheeler": {
        "rate_low": 0.11, "rate_high": 0.20,
        "tenure_low": 24, "tenure_high": 48,
        "label": "Two-Wheeler",
    },
    "working_capital": {
        "rate_low": 0.11, "rate_high": 0.15,
        "tenure_low": 36, "tenure_high": 60,
        "label": "Working Capital / Business Loan",
    },
    "home_loan": {
        "rate_low": 0.085, "rate_high": 0.10,
        "tenure_low": 180, "tenure_high": 300,
        "label": "Home Loan",
    },
    "personal_loan": {
        "rate_low": 0.115, "rate_high": 0.18,
        "tenure_low": 24, "tenure_high": 60,
        "label": "Personal Loan",
    },
    "credit_card_emi": {
        "rate_low": 0.13, "rate_high": 0.20,
        "tenure_low": 6, "tenure_high": 24,
        "label": "Credit Card EMI",
    },
    "gold_loan": {
        "rate_low": 0.09, "rate_high": 0.16,
        "tenure_low": 12, "tenure_high": 36,
        "label": "Gold Loan",
    },
}


@dataclass
class LoanEstimate:
    loan_type: str           # human-readable label
    loan_type_key: str       # internal key
    principal_low: float
    principal_mid: float
    principal_high: float
    rate_low: float          # decimal e.g. 0.10 = 10%
    rate_high: float
    tenure_low: int          # months
    tenure_high: int
    confidence: str          # "High" / "Medium" / "Low"
    notes: str               # human-readable assumption note


def estimate_principal(emi: float, annual_rate: float, tenure_months: int) -> float:
    """Reverse EMI calculation: P = EMI × [(1+r)^n - 1] / [r × (1+r)^n]"""
    r = annual_rate / 12.0
    if r == 0:
        return emi * tenure_months
    factor = math.pow(1 + r, tenure_months)
    return emi * (factor - 1) / (r * factor)


# Lender name patterns that strongly indicate a loan type
_HOME_LOAN_LENDERS = re.compile(r"\b(HDFC HOME|LIC HOUSING|HDFC LTD|HOME LOAN|HOUSING FINANCE|PNB HOUSING|INDIA BULLS HOUSING|DHFL)\b", re.I)
_VEHICLE_CV_LENDERS = re.compile(r"\b(TATA MOTORS|MAHINDRA FIN|TVS CREDIT|SUNDARAM FIN|SHRIRAM CITY|SHRIRAM TRANS|CHOLAMANDALAM|CHOLA|HINDUJA LEYLAND|HINDUJA|KOGTA FIN|IIFL FIN|PROFECTUS)\b", re.I)
_TWO_WHEELER_LENDERS = re.compile(r"\b(BAJAJ AUTO FIN|HERO FIN|HONDA FIN|TVS MOT)\b", re.I)
_GOLD_LOAN_LENDERS = re.compile(r"\b(MUTHOOT|MANAPPURAM|IIFL GOLD|GOLD LOAN)\b", re.I)
_CARD_LENDERS = re.compile(r"\b(CARD|CARDS|CITI CARD|ONE CARD|SLICE|UNI CARD)\b", re.I)


def classify_loan_type(emi: DetectedEMI) -> tuple:
    """Returns (loan_type_key, confidence)."""
    lender = (emi.lender or "").upper()
    avg = emi.avg_emi

    # Strong signals from lender name
    if _HOME_LOAN_LENDERS.search(lender):
        return "home_loan", "High"
    if _VEHICLE_CV_LENDERS.search(lender):
        return "vehicle_cv", "High"
    if _TWO_WHEELER_LENDERS.search(lender):
        return "two_wheeler", "High"
    if _GOLD_LOAN_LENDERS.search(lender):
        return "gold_loan", "High"
    if _CARD_LENDERS.search(lender):
        return "credit_card_emi", "High"

    # BOB internal loan recoveries (OMEGA's fleet finance)
    if "BOB LOAN" in lender:
        if avg >= 300000:
            return "working_capital", "Medium"
        return "vehicle_cv", "Medium"

    # AU Small Finance / Bajaj Finance / Tata Capital → mostly SME / vehicle
    if "AU SMALL FINANCE" in lender:
        return "working_capital", "Medium"
    if "BAJAJ FINANCE" in lender:
        if avg >= 50000:
            return "personal_loan", "Low"  # could also be CV
        return "personal_loan", "Medium"
    if "TATA CAPITAL" in lender or "TATA AIG" in lender:
        return "personal_loan", "Low"

    # IDFC First Bank with mid-high EMI → likely vehicle (truck)
    if "IDFC FIRST" in lender:
        if avg >= 30000:
            return "vehicle_cv", "Medium"
        return "personal_loan", "Low"

    # Mode-based heuristics for ACH-DR / ACH / NACH
    mode = (emi.mode or "").upper()
    if mode in ("ACH", "ACH-DR", "NACH", "BOB-LOAN"):
        # Tier by amount
        if avg < 5000:
            return "two_wheeler", "Low"
        if avg < 20000:
            return "personal_loan", "Low"
        if avg < 100000:
            return "vehicle_cv", "Low"
        return "working_capital", "Low"

    # Default
    return "personal_loan", "Low"


def estimate_loan(emi: DetectedEMI) -> LoanEstimate:
    """Build a LoanEstimate dataclass from a DetectedEMI.

    Uses (total_paid / months_seen) as the monthly EMI for principal calculation.
    This handles cases where the bank splits one EMI into 2 partial debits (common
    in OD/CC facilities when balance is insufficient) - we get the true monthly
    obligation, not the average per-transaction.
    """
    loan_type_key, confidence = classify_loan_type(emi)
    profile = LOAN_PROFILES[loan_type_key]

    # True monthly obligation
    monthly_emi = emi.total_paid / emi.months_seen if emi.months_seen > 0 else emi.avg_emi

    # Low principal estimate = high rate + low tenure (conservative for credit)
    p_low = estimate_principal(monthly_emi, profile["rate_high"], profile["tenure_low"])
    # High principal = low rate + long tenure
    p_high = estimate_principal(monthly_emi, profile["rate_low"], profile["tenure_high"])
    # Mid: midpoint of rate & tenure
    r_mid = (profile["rate_low"] + profile["rate_high"]) / 2
    t_mid = (profile["tenure_low"] + profile["tenure_high"]) // 2
    p_mid = estimate_principal(monthly_emi, r_mid, t_mid)

    notes = (f"Monthly EMI: ₹{monthly_emi:,.0f}. Assumed {profile['rate_low']*100:.1f}-{profile['rate_high']*100:.1f}% p.a., "
             f"{profile['tenure_low']}-{profile['tenure_high']} mo tenure.")

    return LoanEstimate(
        loan_type=profile["label"],
        loan_type_key=loan_type_key,
        principal_low=p_low,
        principal_mid=p_mid,
        principal_high=p_high,
        rate_low=profile["rate_low"],
        rate_high=profile["rate_high"],
        tenure_low=profile["tenure_low"],
        tenure_high=profile["tenure_high"],
        confidence=confidence,
        notes=notes,
    )


def estimate_all_loans(emis: List[DetectedEMI]) -> List[LoanEstimate]:
    return [estimate_loan(e) for e in emis]


def total_exposure(emis: List[DetectedEMI]) -> dict:
    """Aggregate total estimated debt exposure across all detected EMIs.
    Returns dict with total_emi_monthly, total_principal_{low,mid,high}, count."""
    if not emis:
        return {
            "total_monthly_emi": 0.0,
            "total_principal_low": 0.0,
            "total_principal_mid": 0.0,
            "total_principal_high": 0.0,
            "count": 0,
        }
    estimates = estimate_all_loans(emis)
    total_monthly = sum(
        (e.total_paid / e.months_seen if e.months_seen > 0 else e.avg_emi)
        for e in emis
    )
    return {
        "total_monthly_emi": total_monthly,
        "total_principal_low": sum(le.principal_low for le in estimates),
        "total_principal_mid": sum(le.principal_mid for le in estimates),
        "total_principal_high": sum(le.principal_high for le in estimates),
        "count": len(emis),
    }
