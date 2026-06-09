"""Axis Bank statement parser."""
import pdfplumber
import re
from datetime import datetime, date
from typing import Optional
from .base import BaseParser, Transaction, StatementMetadata, ParsedStatement


def _parse_amount(s):
    if not s or not s.strip():
        return None
    s = s.strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


class AxisBankParser(BaseParser):
    bank_name = "Axis Bank"

    @classmethod
    def match(cls, first_page_text: str) -> bool:
        text = first_page_text.upper()
        signals = [
            "AXIS BANK" in text,
            "STATEMENT OF AXIS ACCOUNT" in text,
            "UTIB" in text,  # Axis IFSC prefix
        ]
        return sum(signals) >= 2

    @classmethod
    def parse(cls, pdf_path: str, password: Optional[str] = None) -> ParsedStatement:
        meta = StatementMetadata(bank=cls.bank_name)
        transactions = []
        opening_balance = None
        closing_balance = None

        open_kwargs = {"password": password} if password else {}
        with pdfplumber.open(pdf_path, **open_kwargs) as pdf:
            # --- Header extraction from page 1 ---
            first_text = pdf.pages[0].extract_text() or ""
            cls._extract_header(first_text, meta)

            # --- Transactions from all pages ---
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if not row or len(row) < 7:
                            continue
                        tran_date_str = (row[0] or "").strip()
                        particulars = (row[2] or "").strip().replace("\n", " ")
                        debit = _parse_amount(row[3])
                        credit = _parse_amount(row[4])
                        balance = _parse_amount(row[5])
                        branch = (row[6] or "").strip() if len(row) >= 7 else None
                        cheque = (row[1] or "").strip() if row[1] else None

                        if "OPENING BALANCE" in particulars.upper():
                            opening_balance = balance
                            continue
                        if "CLOSING BALANCE" in particulars.upper():
                            closing_balance = balance
                            continue

                        d = _parse_date(tran_date_str)
                        if d is None or balance is None:
                            continue

                        transactions.append(Transaction(
                            date=d,
                            particulars=particulars,
                            debit=debit,
                            credit=credit,
                            balance=balance,
                            cheque_no=cheque,
                            branch=branch,
                        ))

            # --- Totals: search all pages (totals may be on second-to-last page) ---
            for page in pdf.pages:
                t = page.extract_text() or ""
                if "TRANSACTION TOTAL" in t:
                    cls._extract_totals(t, meta)
                    break

        meta.opening_balance = opening_balance
        if closing_balance is not None:
            meta.closing_balance = closing_balance
        elif transactions:
            meta.closing_balance = transactions[-1].balance

        return ParsedStatement(metadata=meta, transactions=transactions, charges=[])

    @staticmethod
    def _extract_header(text, meta: StatementMetadata):
        lines = text.split("\n")
        # First non-empty line typically has account holder name
        for line in lines[:5]:
            line = line.strip()
            if line and "STATEMENT" not in line.upper() and "JOINT" not in line.upper():
                if not meta.account_holder:
                    meta.account_holder = line
                    break

        patterns = {
            "customer_id": r"Customer ID:\s*(\S+)",
            "ifsc": r"IFSC Code:\s*(\S+)",
            "micr": r"MICR Code:\s*(\S+)",
            "mobile": r"Registered Mobile No:\s*(\S+)",
            "email": r"Registered Email ID:\s*(\S+)",
            "pan": r"PAN:\s*(\S+)",
            "account_type": r"Scheme:\s*(\S[^\n]*?)(?:\s+CKYC|\s*$)",
        }
        for attr, pat in patterns.items():
            m = re.search(pat, text)
            if m:
                setattr(meta, attr, m.group(1).strip())

        # Account No + Period
        m = re.search(r"Statement of Axis Account No:\s*(\d+)\s+for\s+the\s+period\s+\(From:\s*(\d{2}-\d{2}-\d{4})\s+To:\s*(\d{2}-\d{2}-\d{4})\)", text)
        if m:
            meta.account_no = m.group(1)
            meta.period_from = _parse_date(m.group(2))
            meta.period_to = _parse_date(m.group(3))

        # Address: lines 2-6 typically (between name and Customer ID block)
        addr_lines = []
        for line in lines[1:8]:
            line = line.strip()
            if not line:
                continue
            if any(x in line.upper() for x in ["CUSTOMER ID", "IFSC", "MICR", "STATEMENT", "NOMINEE", "REGISTERED", "PAN:", "SCHEME", "CURRENCY"]):
                break
            addr_lines.append(line)
        if addr_lines:
            meta.address = ", ".join(addr_lines)

    @staticmethod
    def _extract_totals(text, meta: StatementMetadata):
        # Totals may span lines: "TRANSACTION TOTAL\n6279155.96\n6242829.69"
        m = re.search(r"TRANSACTION TOTAL[\s\n]+([\d,]+\.\d+)[\s\n]+([\d,]+\.\d+)", text)
        if m:
            meta.total_debit = float(m.group(1).replace(",", ""))
            meta.total_credit = float(m.group(2).replace(",", ""))
        m = re.search(r"CLOSING BALANCE[\s\n]+([\d,]+\.\d+)", text)
        if m:
            meta.closing_balance = float(m.group(1).replace(",", ""))
