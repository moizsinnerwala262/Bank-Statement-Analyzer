"""ICICI Bank statement parser - uses pdfplumber's extract_tables(), which works
well on ICICI's structured layout. Cells contain embedded newlines we strip.
"""
import pdfplumber
import re
from datetime import datetime
from typing import Optional
from .base import BaseParser, Transaction, StatementMetadata, ParsedStatement


def _clean_cell(s):
    if not s:
        return ""
    return " ".join(s.replace("\n", " ").split()).strip()


def _parse_icici_date(s):
    """Parse '01/Jul/2025' (with /n breaks possibly) or '01/07/2025'."""
    if not s:
        return None
    s = _clean_cell(s).replace(" ", "")
    for fmt in ("%d/%b/%Y", "%d/%B/%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(s):
    if not s:
        return None
    s = _clean_cell(s).replace(",", "").replace(" ", "")
    if not s or s.lower() in ("nan", "none"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


class ICICIBankParser(BaseParser):
    bank_name = "ICICI Bank"

    @classmethod
    def match(cls, first_page_text: str) -> bool:
        text = first_page_text.upper().replace(" ", "")
        signals = [
            "DETAILEDSTATEMENT" in text,
            "ICIC0" in text,
            "A/CTYPE" in text or "A/CBRANCH" in text,
        ]
        return sum(signals) >= 2

    @classmethod
    def parse(cls, pdf_path: str, password: Optional[str] = None) -> ParsedStatement:
        meta = StatementMetadata(bank=cls.bank_name)
        transactions = []

        open_kwargs = {"password": password} if password else {}
        with pdfplumber.open(pdf_path, **open_kwargs) as pdf:
            first_text = pdf.pages[0].extract_text() or ""
            cls._extract_header(first_text, meta)

            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if not row or len(row) < 10:
                            continue
                        sl_str = _clean_cell(row[0] or "")
                        # Skip header rows and Page Total rows
                        if not sl_str.isdigit():
                            continue

                        tran_date = _parse_icici_date(row[3] or "")
                        if not tran_date:
                            continue

                        cheque_no = _clean_cell(row[5] or "")
                        narration = _clean_cell(row[6] or "")
                        withdrawal = _parse_amount(row[7])
                        deposit = _parse_amount(row[8])
                        balance = _parse_amount(row[9])

                        if balance is None:
                            continue

                        transactions.append(Transaction(
                            date=tran_date,
                            particulars=narration,
                            debit=withdrawal,
                            credit=deposit,
                            balance=balance,
                            cheque_no=cheque_no if cheque_no else None,
                            branch=None,
                        ))

            cls._extract_totals(pdf, meta)

        return ParsedStatement(metadata=meta, transactions=transactions, charges=[])

    @staticmethod
    def _extract_header(text, meta):
        m = re.search(r"Name:\s*([A-Z][A-Z0-9\s&\.\-,]+?)(?:\s+A/C\s*Branch|\n\s*A/C\s*Branch)", text, re.DOTALL)
        if m:
            meta.account_holder = re.sub(r"\s+", " ", m.group(1)).strip()
        m = re.search(r"A/C No:\s*(\d+)", text)
        if m:
            meta.account_no = m.group(1)
        m = re.search(r"IFSC Code:\s*(\S+)", text)
        if m:
            meta.ifsc = m.group(1)
        m = re.search(r"Cust ID:\s*(\d+)", text)
        if m:
            meta.customer_id = m.group(1)
        m = re.search(r"Transaction Period:\s*From\s*(\d{2}/\d{2}/\d{4})\s*To\s*(\d{2}/\d{2}/\d{4})", text)
        if m:
            meta.period_from = _parse_icici_date(m.group(1))
            meta.period_to = _parse_icici_date(m.group(2))
        m = re.search(r"A/C Type:\s*([A-Z]+)", text)
        if m:
            meta.account_type = m.group(1)
        m = re.search(r"A/C Branch:\s*([A-Z]+)", text)
        if m:
            meta.branch_address = m.group(1)

    @staticmethod
    def _extract_totals(pdf, meta):
        all_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        m = re.search(r"Opening Bal:\s*([\d,]+\.\d+)", all_text)
        if m:
            meta.opening_balance = float(m.group(1).replace(",", ""))
        m = re.search(r"Closing Bal:\s*([\d,]+\.\d+)", all_text)
        if m:
            meta.closing_balance = float(m.group(1).replace(",", ""))
        m = re.search(r"Withdrawls?:\s*([\d,]+\.\d+)", all_text)
        if m:
            meta.total_debit = float(m.group(1).replace(",", ""))
        m = re.search(r"Deposits:\s*([\d,]+\.\d+)", all_text)
        if m:
            meta.total_credit = float(m.group(1).replace(",", ""))
