"""Bank of Baroda statement parser - uses word-position extraction.
BOB's PDF has tricky layout: amounts are right-aligned, transactions can span
1-3 rows (narration may wrap), and rows are in REVERSE chronological order.

Column x-positions (from header analysis):
  TRAN DATE:    x0  ~15
  VALUE DATE:   x0  ~87
  NARRATION:    x0 ~165-360
  CHQ.NO.:      x0 ~365-450
  WITHDRAWAL:   amounts right-aligned at x1 ≤ 570
  DEPOSIT:      amounts right-aligned at x1 ∈ (570, 700)
  BALANCE:      amounts right-aligned at x1 ≥ 700  (suffixed with "Cr" or "Dr")
"""
import pdfplumber
import re
from collections import defaultdict
from datetime import datetime
from typing import Optional, List
from .base import BaseParser, Transaction, StatementMetadata, ParsedStatement


_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_AMOUNT_RE = re.compile(r"^[\d,]+\.\d{1,2}(?:Cr|Dr|CR|DR)?$")


def _is_date(s: str) -> bool:
    return bool(_DATE_RE.match(s.strip()))


def _is_amount(s: str) -> bool:
    return bool(_AMOUNT_RE.match(s.strip()))


def _parse_date(s: str):
    s = s.strip()
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return None


def _parse_amount(s: str):
    if not s:
        return None
    s = s.strip().replace(",", "")
    s = re.sub(r"(Cr|Dr|CR|DR)$", "", s)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# Column classification thresholds based on word x1 (right-edge)
def _classify_amount_column(x1: float) -> str:
    if x1 <= 575:
        return "withdrawal"
    if x1 <= 700:
        return "deposit"
    return "balance"


class BankOfBarodaParser(BaseParser):
    bank_name = "Bank of Baroda"

    @classmethod
    def match(cls, first_page_text: str) -> bool:
        text = first_page_text.upper().replace(" ", "")
        signals = [
            "BARB0" in text,                              # BOB IFSC prefix
            "BANKOFBARODA" in text,
            "STATEMENTPERIODFROM" in text,
            "MICRCODE" in text and "NOMINEEREG" in text,  # BOB-specific header phrases
        ]
        return sum(signals) >= 2

    @classmethod
    def parse(cls, pdf_path: str, password: Optional[str] = None) -> ParsedStatement:
        meta = StatementMetadata(bank=cls.bank_name)
        transactions: List[Transaction] = []

        open_kwargs = {"password": password} if password else {}
        with pdfplumber.open(pdf_path, **open_kwargs) as pdf:
            first_text = pdf.pages[0].extract_text() or ""
            cls._extract_header(first_text, meta)

            for page in pdf.pages:
                cls._parse_page(page, transactions)

        # BOB lists transactions in reverse chronological order overall AND within each day.
        # Reverse the parsed list first, then stable-sort by date so chronological order
        # is preserved both across days AND within the same day.
        transactions.reverse()
        transactions.sort(key=lambda t: t.date)

        # Derive opening/closing balance from first/last txn
        if transactions:
            first = transactions[0]
            meta.opening_balance = first.balance - (first.credit or 0) + (first.debit or 0)
            meta.closing_balance = transactions[-1].balance
            meta.total_debit = sum(t.debit for t in transactions if t.debit)
            meta.total_credit = sum(t.credit for t in transactions if t.credit)

        return ParsedStatement(metadata=meta, transactions=transactions, charges=[])

    @staticmethod
    def _extract_header(text: str, meta: StatementMetadata):
        m = re.search(r"Main Account Holder Name\s*:\s*([A-Z][A-Z0-9\s&\.\-,]+?)(?:Address|Joint|$)",
                     text, re.IGNORECASE | re.DOTALL)
        if m:
            meta.account_holder = re.sub(r"\s+", " ", m.group(1)).strip()
        m = re.search(r"Account No:\s*(\S+)", text)
        if m:
            meta.account_no = m.group(1)
        m = re.search(r"IFSC Code:\s*(\S+)", text)
        if m:
            meta.ifsc = m.group(1)
        m = re.search(r"MICR Code:\s*(\S+)", text)
        if m:
            meta.micr = m.group(1)
        m = re.search(r"Customer Id:\s*(\S+)", text)
        if m:
            meta.customer_id = m.group(1)
        m = re.search(r"Branch Name:\s*([^M\n]+?)(?:MICR|$)", text)
        if m:
            meta.branch_address = m.group(1).strip().rstrip(",")
        m = re.search(r"Statement Period from\s*(\d{2}/\d{2}/\d{4})\s*to\s*(\d{2}/\d{2}/\d{4})", text)
        if m:
            meta.period_from = _parse_date(m.group(1))
            meta.period_to = _parse_date(m.group(2))

    @classmethod
    def _parse_page(cls, page, transactions: List[Transaction]):
        words = page.extract_words()
        if not words:
            return

        # Group words by 'top' coordinate (rows)
        rows = defaultdict(list)
        for w in words:
            key = round(w["top"] / 3) * 3
            rows[key].append(w)
        sorted_rows = sorted(rows.items())

        current_txn = None  # dict accumulating fields for current txn

        for top, ws in sorted_rows:
            ws_sorted = sorted(ws, key=lambda x: x["x0"])
            row_text = " ".join(w["text"] for w in ws_sorted)

            # Skip non-transaction rows (headers, footers, page numbers)
            if any(skip in row_text for skip in [
                "TRAN DATE", "WITHDRAWAL", "DEPOSIT(CR)", "BALANCE(INR)",
                "OMEGA LOGISTICS AND CONSULTANCY Account",
                "Contact-Us@", "This is computer-generated",
                "Statement of transactions",
                "Page", "Your safety", "Bank will never",
                "#StaySafe", "Account No:", "Customer Id:", "Branch Name:",
                "IFSC Code:", "MICR Code:", "Nominee Reg",
                "Main Account Holder", "Joint Account Holder",
                "Address", "Statement Period",
                "Your Account Statement as on",
                "do not share",
            ]):
                continue

            # Identify pieces in this row
            tran_date = None
            value_date = None
            narration_parts = []
            cheque_no = None
            amounts = {"withdrawal": None, "deposit": None, "balance": None}

            for w in ws_sorted:
                text = w["text"]
                x0, x1 = w["x0"], w["x1"]

                if _is_date(text):
                    # Could be tran_date (x0 ~15) or value_date (x0 ~87)
                    if x0 < 70:
                        tran_date = _parse_date(text)
                    elif x0 < 150:
                        value_date = _parse_date(text)
                    elif x0 > 600:
                        # Could be value-date on continuation line (right of balance area)
                        # Ignore - we don't need it
                        pass
                elif _is_amount(text):
                    col = _classify_amount_column(x1)
                    amt = _parse_amount(text)
                    if amt is not None:
                        amounts[col] = amt
                elif 160 <= x0 <= 360:
                    narration_parts.append(text)
                elif 360 <= x0 <= 460 and text.isdigit():
                    cheque_no = text

            narration = " ".join(narration_parts).strip()

            # New transaction starts when we see a tran_date
            if tran_date is not None:
                if current_txn is not None:
                    cls._finalize_txn(current_txn, transactions)
                current_txn = {
                    "tran_date": tran_date,
                    "value_date": value_date,
                    "narration_parts": [narration] if narration else [],
                    "cheque_no": cheque_no,
                    "withdrawal": amounts["withdrawal"],
                    "deposit": amounts["deposit"],
                    "balance": amounts["balance"],
                }
            else:
                # Continuation line for previous transaction
                if current_txn is not None:
                    if narration:
                        current_txn["narration_parts"].append(narration)
                    if cheque_no and not current_txn["cheque_no"]:
                        current_txn["cheque_no"] = cheque_no
                    # Take any amounts not yet set
                    for col in ("withdrawal", "deposit", "balance"):
                        if amounts[col] is not None and current_txn[col] is None:
                            current_txn[col] = amounts[col]

        # Finalize last transaction at end of page
        if current_txn is not None:
            cls._finalize_txn(current_txn, transactions)

    @staticmethod
    def _finalize_txn(txn_dict: dict, transactions: List[Transaction]):
        if txn_dict["balance"] is None or txn_dict["tran_date"] is None:
            return
        # Need EITHER withdrawal or deposit (else it's not a real txn)
        if txn_dict["withdrawal"] is None and txn_dict["deposit"] is None:
            return
        narration = " ".join(txn_dict["narration_parts"]).strip()
        narration = re.sub(r"\s+", " ", narration)
        transactions.append(Transaction(
            date=txn_dict["tran_date"],
            particulars=narration,
            debit=txn_dict["withdrawal"],
            credit=txn_dict["deposit"],
            balance=txn_dict["balance"],
            cheque_no=txn_dict["cheque_no"],
            branch=None,
        ))
