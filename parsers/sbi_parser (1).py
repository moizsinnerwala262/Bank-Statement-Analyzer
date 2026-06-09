"""
sbi_parser.py  -  Standalone SBI (State Bank of India) net-banking PDF parser
================================================================================
Drop-in module for the BSA Tool. Mirrors the word/cell-position approach used in
the BoB parser, but exploits the fact that SBI's net-banking statement is a
fully ruled table -> pdfplumber.extract_tables() returns clean 8-column rows and
joins wrapped lines inside each cell, which sidesteps the description/ref
interleaving you get from raw text extraction.

Handles BOTH product variants:
  - CA / SB  (savings / current)            -> Account Description starts "CA"/"SB"
  - CC / OD  (cash credit, EB-MSME-CC etc.) -> Drawing Power > 0, negative balances

Public API (adapt names to your registry):
    is_sbi(pdf_path) -> bool
    parse(pdf_path)  -> {"meta": {...}, "transactions": [Txn, ...], "df": DataFrame}

Dependencies: pdfplumber, pandas  (stdlib: re, dataclasses, datetime)
"""

from __future__ import annotations
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional

import pdfplumber
import pandas as pd

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
_SBI_MARKERS = ("Account Description", "CIF No", "IFS Code", "MICR Code")

def is_sbi(pdf_path: str) -> bool:
    """Lightweight format detector for the parser registry."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            txt = (pdf.pages[0].extract_text() or "")
    except Exception:
        return False
    hits = sum(m in txt for m in _SBI_MARKERS)
    ifsc_sbi = bool(re.search(r"\bSBIN0\d{6}\b", txt))
    return hits >= 3 and ifsc_sbi


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Txn:
    txn_date: Optional[str]            # ISO yyyy-mm-dd
    value_date: Optional[str]
    description_raw: str               # cell text, newlines collapsed to space
    ref_no: str                        # cheque / instrument ref where present
    branch_code: str
    debit: float
    credit: float
    balance: float
    direction: str                     # 'debit' | 'credit'
    category: str                      # LOAN_EMI / INSURANCE / TRADE / INTERNAL / CASH / CHARGES / INTEREST / TRANSFER / OTHER
    channel: str                       # UPI / IMPS / NEFT / NETBANKING / NACH / CHEQUE / CASH / CHARGE
    counterparty: str                  # cleaned name (best effort)
    counterparty_acct: str             # account / vpa / instrument id where available
    recon_ok: bool = True              # running-balance check passed


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _num(s: Optional[str]) -> float:
    s = (s or "").replace(",", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0

def _iso(d: Optional[str]) -> Optional[str]:
    d = (d or "").strip()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d)
    if not m:
        return None
    dd, mm, yy = m.groups()
    try:
        return datetime(int(yy), int(mm), int(dd)).strftime("%Y-%m-%d")
    except ValueError:
        return None

def _flat(cell: Optional[str], sep: str = " ") -> str:
    """Collapse a multi-line cell. sep=' ' keeps name spacing; sep='' rejoins
    tokens that were split mid-word by column wrapping."""
    if not cell:
        return ""
    return sep.join(p for p in cell.split("\n") if p != "").strip()

def _clean_name(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip(" -/*")
    return s.upper()


# ---------------------------------------------------------------------------
# Counterparty / channel / category classification
# ---------------------------------------------------------------------------
# Combined narration is built from description + ref. We keep both a spaced form
# (names readable) and a no-space form (codes/rrn/accounts contiguous).

_LENDER_HINTS = ("BAJAJFINANCE", "BAJAJ FINANCE", "L&TFINANCE", "L&T FINANCE",
                 "FINANCELTD", "HDB", "FULLERTON", "TATACAP", "CHOLA")
_INSUR_HINTS  = ("HDFCLIF", "LIFEINS", "LIFE INS", "LIC", "INSUR", "MAXLIFE", "SBILIFE")

def _classify(desc_sp: str, ref_sp: str, direction: str,
              own_accts: set[str]) -> tuple[str, str, str, str]:
    """Return (category, channel, counterparty, counterparty_acct)."""
    # Spaced combine, then remove ONLY the wrap-spaces that sit between two
    # digits (e.g. rrn "1016214172 95" -> "101621417295"). Real name spaces
    # like "SAHIB TEXTILES" are kept because at least one side is a letter.
    sp = (desc_sp + " " + ref_sp).strip()
    sp = re.sub(r"(?<=\d)\s+(?=\d)", "", sp)
    up = sp.upper()

    # ---- bank charges / interest (no external party) ----
    if "DEBIT INTEREST" in up:
        return "INTEREST", "CHARGE", "SBI – CC INTEREST", ""
    if any(k in up for k in ("MIN BAL CHGS", "A/C KEEPING", "AMC DEBIT CARD",
                             "CHEQUE BOOK ISSUE", "INTER CITY CHARGES",
                             "ECS/ACH RETURN", "SMS CHARGE", "GST")):
        return "CHARGES", "CHARGE", "SBI – BANK CHARGES", ""

    # ---- outward-cheque return / reversal (your bounce signal: 'LE DT') ----
    if "LE DT" in up or "CHEQUE RETURN" in up or ("RETURN" in up and "CHQ" in up):
        chq_m = re.search(r"CHQ\s*(\d{4,6})", up)
        return ("CHEQUE_RETURN", "CHEQUE", "OUTWARD CHEQUE RETURN/REVERSAL",
                chq_m.group(1) if chq_m else "")

    # ---- NACH / ACH direct debit (loan EMI / insurance SI) ----
    if "ACHDR" in up or "NACH" in up or "DEBIT-ACH" in up:
        tail = _ach_tail(sp)
        cat = "OTHER"
        u = tail.upper().replace(" ", "")
        if any(h.replace(" ", "") in u for h in _LENDER_HINTS):
            cat = "LOAN_EMI"
        elif any(h.replace(" ", "") in u for h in _INSUR_HINTS):
            cat = "INSURANCE"
        return cat, "NACH", _clean_name(tail), ""

    # ---- UPI ----
    m = re.search(r"UPI/(?:CR|DR|REV)/(\d+)/([^/]+)/([A-Za-z]+)/([^/]+)/", sp)
    if m:
        name = _clean_name(m.group(2))
        vpa = re.sub(r"\s+", "", m.group(4))
        return ("TRANSFER", "UPI", name or "UPI PARTY", vpa)

    # ---- NEFT  (fields delimited by '*') ----
    if "NEFT*" in up:
        parts = sp.split("*")
        raw = parts[3] if len(parts) > 3 else ""
        # if name is the trailing field, ref text gets appended -> cut it off
        raw = re.split(r"\bTRANSFER (?:TO|FROM)\b|BATCHID", raw, flags=re.I)[0]
        name = _clean_name(raw)
        ifsc = parts[1].strip() if len(parts) > 1 else ""
        return ("TRADE", "NEFT", name or "NEFT PARTY", ifsc)

    # ---- IMPS  (slashes may carry stray wrap-spaces) ----
    m = re.search(r"IMPS/\s*\d+\s*/\s*[A-Za-z0-9]+-[xX]+\w*-\s*([^/]*)", sp)
    if m:
        name = _clean_name(m.group(1))
        return ("TRADE" if name else "TRANSFER", "IMPS",
                name or "IMPS PARTY", "")

    # ---- cheque clearing / withdrawal (must precede generic INB transfer) ----
    if "CLEARING" in up or "CHEQUE WDL" in up or re.search(r"\bChq\b", sp, re.I):
        name, chq = _cheque_payee(desc_sp, ref_sp)
        cat = "TRADE" if name and direction == "debit" else "TRANSFER"
        return (cat, "CHEQUE", name or "CHEQUE PARTY", chq)

    # ---- internet-banking inter-account transfer (TO/FROM <acct> <name>) ----
    m = re.search(r"TRANSFER (?:TO|FROM)\s*(\d+)\s+(.+?)\s*/", sp)
    if m:
        acct, name = m.group(1), _clean_name(m.group(2))
        if acct in own_accts or "PAHAL SILK MILLS" in name:
            return ("INTERNAL", "NETBANKING", name or "SELF", acct)
        return ("TRANSFER", "NETBANKING", name or "PARTY", acct)

    # ---- cash ----
    if "CASH DEPOSIT" in up:
        return ("CASH", "CASH", "CASH DEPOSIT (SELF)", "")

    return ("OTHER", "OTHER", _clean_name(sp)[:40] or "UNCLASSIFIED", "")


def _flat_join(a: str, b: str, sep: str) -> str:
    a = a or ""; b = b or ""
    return (a + " " + b).strip() if sep == " " else (a + b)

def _ach_tail(desc_sp: str) -> str:
    """From 'DEBIT-ACHDr NACH00000000016252 BAJAJ FINANCE-' pull 'BAJAJ FINANCE'."""
    s = desc_sp
    s = re.sub(r"DEBIT-?ACHDr", " ", s, flags=re.I)
    s = re.sub(r"\bTP ACH\b", " ", s, flags=re.I)
    s = re.sub(r"(NACH|SCBL|ICIC|SBIN)\w*\d+\w*", " ", s)   # mandate / sponsor codes
    s = re.sub(r"\b\d{6,}\b", " ", s)                       # stray long numbers
    return _clean_name(s)

def _cheque_payee(desc_sp: str, ref_sp: str) -> tuple[str, str]:
    """Extract payee + cheque no from the noisy CLEARING / WDL narration."""
    src = (desc_sp + " " + ref_sp)
    chq_m = re.search(r"-(\d{5,6})\b", src) or re.search(r"\b(\d{6})\b", src)
    chq = chq_m.group(1) if chq_m else ""
    s = src
    s = re.sub(r"TO CLEARING|CREDIT|CHEQUE WDL|CHEQUE|TRANSFER (TO|FROM)", " ", s, flags=re.I)
    s = re.sub(r"Chq\s*(No\.?)?", " ", s, flags=re.I)
    s = re.sub(r"Sess\s*\d+", " ", s, flags=re.I)
    s = re.sub(r"Seq\s*\d+", " ", s, flags=re.I)
    s = re.sub(r"LE\s*DT\s*\d+", " ", s, flags=re.I)
    s = re.sub(r"PrBr|JKB|\bDT\b", " ", s, flags=re.I)
    s = re.sub(r"\d{4,}", " ", s)        # cheque / account numbers
    s = re.sub(r"-\d+$", " ", s)
    return _clean_name(s), chq


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
def _parse_meta(first_page_text: str) -> dict:
    # SBI renders the label/value tab as a literal "(cid:9)" glyph; normalise it.
    t = re.sub(r"\(cid:\d+\)", "\t", first_page_text or "")
    def grab(label, pat=r"\s*:\s*([^\n]+)"):
        m = re.search(re.escape(label) + pat, t)
        return m.group(1).strip() if m else None
    acc_desc = grab("Account Description")
    dp = _num(grab("Drawing Power"))
    meta = {
        "bank": "SBI",
        "account_name": grab("Account Name"),
        "account_number": (grab("Account Number") or "").lstrip("0") or grab("Account Number"),
        "account_number_raw": grab("Account Number"),
        "account_description": acc_desc,
        "branch": grab("Branch"),
        "ifsc": grab("IFS Code"),
        "micr": grab("MICR Code"),
        "cif": grab("CIF No"),
        "drawing_power": dp,
        "interest_rate": _num(grab(r"Interest Rate(% p.a.)") or grab("Interest Rate")),
        "product_type": _product_type(acc_desc, dp),
    }
    ob = re.search(r"Opening Balance as on\s*(.+?)\s*:\s*([\-\d,\.]+)", t)
    if ob:
        meta["opening_balance_date"] = ob.group(1).strip()
        meta["opening_balance"] = _num(ob.group(2))
    per = re.search(r"Account Statement from\s*(.+?)\s*to\s*(.+?)(?:\n|$)", t)
    if per:
        meta["period_from"] = per.group(1).strip()
        meta["period_to"] = per.group(2).strip()
    return meta

def _product_type(acc_desc: Optional[str], drawing_power: float) -> str:
    d = (acc_desc or "").upper()
    if "CC" in d or "OD" in d or "CREDIT DISPEN" in d or drawing_power > 0:
        return "CC/OD"
    if d.startswith("CA"):
        return "CURRENT"
    if d.startswith("SB") or "SAVING" in d:
        return "SAVINGS"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Main parse
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}")

def parse(pdf_path: str) -> dict:
    with pdfplumber.open(pdf_path) as pdf:
        meta = _parse_meta(pdf.pages[0].extract_text() or "")
        raw_rows = []
        for pg in pdf.pages:
            for tbl in pg.extract_tables():
                for r in tbl:
                    if not r or len(r) < 8:
                        continue
                    c0 = (r[0] or "").strip()
                    if c0 == "Txn Date":          # repeated header
                        continue
                    if _DATE_RE.match(c0):
                        raw_rows.append(r)

    own = set()
    if meta.get("account_number_raw"):
        own.add(meta["account_number_raw"].lstrip("0"))
        own.add(meta["account_number_raw"])

    txns: list[Txn] = []
    bal = meta.get("opening_balance", 0.0)
    for r in raw_rows:
        debit, credit, shown = _num(r[5]), _num(r[6]), _num(r[7])
        direction = "credit" if credit > 0 else "debit"
        desc_sp = _flat(r[2], " ")
        ref_sp = _flat(r[3], " ")
        cat, chan, cp, cp_acct = _classify(desc_sp, ref_sp, direction, own)

        bal = bal - debit + credit
        recon = abs(bal - shown) <= 0.01
        if not recon:
            bal = shown   # trust statement, keep going

        txns.append(Txn(
            txn_date=_iso(r[0]), value_date=_iso(r[1]),
            description_raw=desc_sp, ref_no=_flat(r[3], " "),
            branch_code=(r[4] or "").strip(),
            debit=debit, credit=credit, balance=shown,
            direction=direction, category=cat, channel=chan,
            counterparty=cp, counterparty_acct=cp_acct, recon_ok=recon,
        ))

    df = pd.DataFrame([asdict(t) for t in txns])
    meta["closing_balance"] = txns[-1].balance if txns else meta.get("opening_balance")
    meta["txn_count"] = len(txns)
    meta["recon_mismatches"] = int((~df["recon_ok"]).sum()) if not df.empty else 0
    return {"meta": meta, "transactions": txns, "df": df}


if __name__ == "__main__":
    import sys, json
    res = parse(sys.argv[1])
    print(json.dumps(res["meta"], indent=2, default=str))
    print(res["df"].head(20).to_string())
