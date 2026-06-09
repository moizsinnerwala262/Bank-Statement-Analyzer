"""ICICI Bank retail/iMobile 'Account statement' parser.

This is a DIFFERENT format from the ICICI corporate statement (icici.py):
  - Title "Account statement", "Total effective available" balance block
  - 8-column table: S.no | Transaction ID | Transaction date | Cheque No |
                    Description | Withdrawal (Dr) | Deposit (Cr) | Available Balance
  - Description wraps mid-word across lines (newlines must be stripped, not spaced)
  - Transactions in CHRONOLOGICAL order (oldest first)
  - Date format: "01-Apr-2026" (may render with embedded newline)
  - Current accounts can run NEGATIVE (overdraft) - balances may be negative

extract_tables() works cleanly here, unlike BOB.
"""
import pdfplumber
import re
from datetime import datetime
from typing import Optional, List
from .base import BaseParser, Transaction, StatementMetadata, ParsedStatement


def _clean_multiline(s: str) -> str:
    """ICICI wraps description mid-word; remove newlines without inserting spaces."""
    if s is None:
        return ""
    # Remove the newline characters (ICICI breaks mid-token), collapse stray double spaces
    cleaned = s.replace("\n", "")
    cleaned = re.sub(r"[ ]{2,}", " ", cleaned).strip()
    return cleaned


def _parse_date(s: str):
    if not s:
        return None
    s = s.replace("\n", "").replace(" ", "").strip()
    # e.g. "01-Apr-2026"
    for fmt in ("%d-%b-%Y", "%d-%b%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(s: str):
    if not s:
        return None
    s = str(s).replace("\n", "").replace(",", "").replace(" ", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


class ICICIRetailParser(BaseParser):
    bank_name = "ICICI Bank"

    @classmethod
    def match(cls, first_page_text: str) -> bool:
        # Normalize: remove all whitespace so "A ccount" == "Account"
        t = re.sub(r"\s+", "", first_page_text).upper()
        return ("ICIC0" in t
                and "ACCOUNTSTATEMENT" in t
                and "TOTALEFFECTIVE" in t)

    @classmethod
    def parse(cls, pdf_path: str, password: Optional[str] = None) -> ParsedStatement:
        meta = StatementMetadata(bank=cls.bank_name)
        transactions: List[Transaction] = []

        open_kwargs = {"password": password} if password else {}
        with pdfplumber.open(pdf_path, **open_kwargs) as pdf:
            first_text = pdf.pages[0].extract_text() or ""
            cls._extract_header(first_text, meta)

            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if not table:
                        continue
                    for row in table:
                        if len(row) < 8:
                            continue
                        # Skip header rows: first cell is "S.no" or empty, not a number
                        sno = (row[0] or "").replace("\n", "").strip()
                        if not sno.isdigit():
                            continue
                        cls._process_row(row, transactions)

        # Already chronological. Sort to be safe (stable).
        transactions.sort(key=lambda t: t.date)

        if transactions:
            first = transactions[0]
            meta.opening_balance = first.balance - (first.credit or 0) + (first.debit or 0)
            meta.closing_balance = transactions[-1].balance
            meta.total_debit = sum(t.debit for t in transactions if t.debit)
            meta.total_credit = sum(t.credit for t in transactions if t.credit)

        return ParsedStatement(metadata=meta, transactions=transactions, charges=[])

    @staticmethod
    def _extract_header(text: str, meta: StatementMetadata):
        m = re.search(r"Account name:\s*([A-Z][A-Z0-9\s&\.\-,]+?)(?:\n|Account type)", text, re.I)
        if m:
            meta.account_holder = re.sub(r"\s+", " ", m.group(1)).strip()
        m = re.search(r"Account number:\s*(\d+)", text)
        if m:
            meta.account_no = m.group(1)
        m = re.search(r"IFSC code:\s*(\S+)", text)
        if m:
            meta.ifsc = m.group(1)
        m = re.search(r"Customer ID:\s*(\S+)", text)
        if m:
            meta.customer_id = m.group(1)
        m = re.search(r"Account type:\s*([A-Za-z\s]+?)(?:\n|Account currency)", text)
        if m:
            meta.account_type = m.group(1).strip()
        m = re.search(r"from\s*(\d{2}\s*\w+\s*'?\d{2})\s*-\s*(\d{2}\s*\w+\s*'?\d{2})", text)
        if m:
            meta.period_from = _ICICIRetail_parse_period(m.group(1))
            meta.period_to = _ICICIRetail_parse_period(m.group(2))

    @staticmethod
    def _process_row(row, transactions: List[Transaction]):
        # Expect 8 columns; be defensive about length
        if len(row) < 8:
            return
        sno, txn_id, txn_date, cheque_no, desc, withdrawal, deposit, balance = row[:8]

        date = _parse_date(txn_date)
        if date is None:
            return
        bal = _parse_amount(balance)
        if bal is None:
            return
        dr = _parse_amount(withdrawal)
        cr = _parse_amount(deposit)
        if dr is None and cr is None:
            return
        narration = _clean_multiline(desc)
        cheque = _clean_multiline(cheque_no) if cheque_no else None

        transactions.append(Transaction(
            date=date,
            particulars=narration,
            debit=dr,
            credit=cr,
            balance=bal,
            cheque_no=cheque or None,
            branch=None,
        ))


def _ICICIRetail_parse_period(s: str):
    s = s.replace("'", " ").strip()
    s = re.sub(r"\s+", " ", s)
    for fmt in ("%d %b %y", "%d %B %y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
