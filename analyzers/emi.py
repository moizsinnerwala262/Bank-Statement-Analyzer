"""EMI / recurring obligation detector."""
import re
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional
from parsers.base import ParsedStatement, Transaction


LENDER_PATTERNS = [
    (r"BAJAJ FIN", "Bajaj Finance"),
    (r"HDFC BANK LIM|HDFC LTD|HDFC HOME", "HDFC Bank / HDFC Ltd"),
    (r"ICICI BANK|ICICI HOME|ICICI HOUS", "ICICI Bank / ICICI Home Finance"),
    (r"ADITYABIRLA|ADITYA BIRLA|ABFL|ABWSPL|ABCL", "Aditya Birla Finance / Capital"),
    (r"TATA CAPITAL|TCFSL|TCHFL", "Tata Capital"),
    (r"MAHINDRA FINAN|MMFSL|MAHINDRA FINANCE", "Mahindra & Mahindra Finance"),
    (r"L\&T FIN|LTF|L AND T FIN|L T FIN|LT FINANCE", "L&T Finance"),
    (r"CHOLAMANDALAM|CHOLA", "Cholamandalam"),
    (r"MUTHOOT", "Muthoot Finance"),
    (r"MANAPPURAM", "Manappuram Finance"),
    (r"POONAWALLA", "Poonawalla Fincorp"),
    (r"IDFC FIRST|IDFC BANK", "IDFC First Bank"),
    (r"INDUSIND BANK", "IndusInd Bank"),
    (r"AXIS BANK LOAN|AXIS FIN", "Axis Bank Loan"),
    (r"KOTAK MAH", "Kotak Mahindra"),
    (r"YES BANK", "Yes Bank"),
    (r"SBI HOME|SBI LOAN", "SBI Loan"),
    (r"HERO FIN|HEROFIN", "Hero FinCorp"),
    (r"CAPITAL FLOAT", "Capital Float"),
    (r"LENDINGKART", "Lendingkart"),
    (r"HOMEFIRST", "HomeFirst Finance"),
    (r"PIRAMAL", "Piramal Finance"),
    (r"FULLERTON|SMFG", "Fullerton / SMFG"),
    (r"DHFL", "DHFL"),
    (r"STANDARD CHARTERED", "Standard Chartered"),
    (r"AMERICAN EXPRESS", "American Express"),
    (r"BANDHAN BANK", "Bandhan Bank"),
    (r"FEDERAL BANK", "Federal Bank"),
    (r"RBL BANK", "RBL Bank"),
    (r"AU SMALL", "AU Small Finance Bank"),
    (r"EQUITAS", "Equitas Small Finance Bank"),
    (r"UJJIVAN", "Ujjivan Small Finance Bank"),
    (r"HDB FIN", "HDB Financial Services"),
    (r"SUNDARAM FIN|SHRIRAM TRANS|SHRIRAM FIN", "Sundaram Finance / Shriram"),
    (r"KMBLDRAOPE", "Kotak Mahindra Bank (DRA)"),
]

EMI_EXCLUSIONS = [
    r"CRED CLUB|CRED GOLD|CRED CCBP|CRED STORE|CRED WALLET|CRED DIGI",
    r"NETFLIX|HOTSTAR|JIOHOTSTA|KUKUFM|POCKET FM|SPOTIFY|PRIME",
    r"GOOGLE PL|GOOGLE PA|GOOGLE INDIA",
    r"WWW MONEY",
    r"INSURANCE|LIC PREMIUM|HDFC LIFE|ICICI PRU|TATA AIA|MAX LIFE",
    r"NBSM",  # Net Banking SIP / Mutual Fund
    r"ZERODHA|GROWW|UPSTOX",
    r"NSDL PAYM",
]

EMI_PREFIXES = [
    re.compile(r"^ACH-DR", re.IGNORECASE),      # Axis format
    re.compile(r"^ACH/", re.IGNORECASE),        # ICICI format
    re.compile(r"^ECS/", re.IGNORECASE),
    re.compile(r"^NACH/", re.IGNORECASE),
    re.compile(r"^EMANDATE", re.IGNORECASE),
    re.compile(r"^E-MANDATE", re.IGNORECASE),
    re.compile(r"^Loan Recovery", re.IGNORECASE),  # BOB internal loan recovery
    re.compile(r"^CMS/", re.IGNORECASE),           # ICICI retail - some loan EMIs route via CMS
]


@dataclass
class DetectedEMI:
    lender: str
    norm_mandate: str
    mode: str  # ACH-DR / ECS / NACH
    typical_day: int
    day_variance: int
    months_seen: int
    first_date: date
    last_date: date
    min_emi: float
    max_emi: float
    avg_emi: float
    amt_variance_pct: float
    total_paid: float
    items: List[dict] = field(default_factory=list)
    flag: str = ""


def _is_emi_prefix(part: str) -> bool:
    return any(p.match(part) for p in EMI_PREFIXES)


def _is_excluded(part: str) -> bool:
    p = part.upper()
    return any(re.search(pat, p) for pat in EMI_EXCLUSIONS)


def _get_lender(part: str) -> Optional[str]:
    # BOB internal loan recovery: "Loan Recovery For18670600002810"
    m = re.match(r"Loan Recovery For\s*(\d+)", part, re.IGNORECASE)
    if m:
        return f"BOB Loan A/C ...{m.group(1)[-4:]}"
    p = part.upper()
    for pat, name in LENDER_PATTERNS:
        if re.search(pat, p):
            return name
    # Fallback: ICICI ACH/CMS recurring debit with no recognized lender.
    # e.g. "ACH/CYCLE/ICIC0009012600051377/GS17..." -> "Cycle (ACH mandate)"
    m = re.match(r"ACH/([A-Za-z0-9 ]+?)/(?:ICIC|[A-Z]{4})\d", part, re.IGNORECASE)
    if m:
        label = re.sub(r"^TP\s+ACH\s+", "", m.group(1).strip(), flags=re.IGNORECASE)
        if label and label.upper() not in ("TP", "ACH"):
            return f"{label.title()} (ACH mandate)"
    return None


def _get_normalized_mandate(part: str):
    # BOB: use full loan account # as the unique mandate
    m = re.match(r"Loan Recovery For\s*(\d+)", part, re.IGNORECASE)
    if m:
        return m.group(1), m.group(1)
    raw = None
    # Axis: ACH-DR-LENDERNAME<digits>-UTIB...
    m = re.search(r"ACH-DR-(.+?)-UTIB", part, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
    # ICICI: ACH/LENDERNAME/ICIC<mandate_id>/<ref>
    if not raw:
        m = re.search(r"ACH/([^/]+)/(ICIC\d+|[A-Z]+\d{8,})/", part, re.IGNORECASE)
        if m:
            raw = f"{m.group(1).strip()}-{m.group(2).strip()}"
    # ICICI retail: CMS/<mandate>/<LENDER NAME>
    if not raw:
        m = re.search(r"CMS/(\d+)/(.+)$", part, re.IGNORECASE)
        if m:
            raw = f"{m.group(2).strip()}-{m.group(1).strip()}"
    if not raw:
        m = re.search(r"ECS/(\w+)/", part, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
    if not raw:
        m = re.search(r"NACH/(\w+)/", part, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
    if not raw:
        return None, None
    normalized = re.sub(r"\d{5,}", "", raw).strip()
    return raw, normalized


def _get_mode(part: str) -> str:
    p = part.upper()
    if p.startswith("ACH-DR"):
        return "ACH-DR"
    if p.startswith("ACH/"):
        return "ACH"
    if p.startswith("ECS"):
        return "ECS"
    if p.startswith("NACH"):
        return "NACH"
    if p.startswith("LOAN RECOVERY"):
        return "BOB-LOAN"
    if "MANDATE" in p:
        return "EMANDATE"
    return "OTHER"


def detect_emis(stmt: ParsedStatement) -> List[DetectedEMI]:
    candidates = []
    for t in stmt.transactions:
        if t.debit is None or t.debit <= 0:
            continue
        if not _is_emi_prefix(t.particulars):
            continue
        if _is_excluded(t.particulars):
            continue
        lender = _get_lender(t.particulars)
        if not lender:
            continue
        raw_m, norm_m = _get_normalized_mandate(t.particulars)
        candidates.append({
            "date": t.date,
            "amount": t.debit,
            "particulars": t.particulars,
            "lender": lender,
            "raw_mandate": raw_m,
            "norm_mandate": norm_m,
        })

    groups = defaultdict(list)
    for c in candidates:
        key = (c["lender"], c["norm_mandate"] or "")
        groups[key].append(c)

    results: List[DetectedEMI] = []
    for (lender, norm_m), items in groups.items():
        items.sort(key=lambda x: x["date"])
        months_seen = {(it["date"].year, it["date"].month) for it in items}
        days = [it["date"].day for it in items]
        amounts = [it["amount"] for it in items]

        if len(months_seen) < 2:
            continue
        if max(days) - min(days) > 7:
            continue

        amt_min, amt_max = min(amounts), max(amounts)
        amt_avg = sum(amounts) / len(amounts)
        var_pct = (amt_max - amt_min) / amt_avg * 100 if amt_avg else 0
        if var_pct > 250:
            continue

        typical_day = Counter(days).most_common(1)[0][0]
        mode = _get_mode(items[0]["particulars"])

        if var_pct < 5:
            flag = "Fixed EMI"
        elif var_pct < 25:
            flag = "Minor variance"
        else:
            flag = "High variance - check top-up / floating rate / loan stacking"

        results.append(DetectedEMI(
            lender=lender,
            norm_mandate=norm_m or "",
            mode=mode,
            typical_day=typical_day,
            day_variance=max(days) - min(days),
            months_seen=len(months_seen),
            first_date=items[0]["date"],
            last_date=items[-1]["date"],
            min_emi=amt_min,
            max_emi=amt_max,
            avg_emi=amt_avg,
            amt_variance_pct=var_pct,
            total_paid=sum(amounts),
            items=[{"date": it["date"], "amount": it["amount"]} for it in items],
            flag=flag,
        ))

    results.sort(key=lambda x: -x.avg_emi)
    return results
