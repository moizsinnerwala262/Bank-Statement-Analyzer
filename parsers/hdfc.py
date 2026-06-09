"""HDFC Bank statement parser.

HDFC PDFs don't work reliably with pdfplumber's extract_tables() — columns
collapse. We parse word-by-word using x-coordinate positions to assign each
amount to the correct column (Withdrawal / Deposit / Balance).
"""
import pdfplumber
import re
from datetime import datetime, date
from typing import Optional, List, Dict
from .base import BaseParser, Transaction, StatementMetadata, ParsedStatement


DATE_DDMMYY = re.compile(r"^(\d{2})/(\d{2})/(\d{2})$")
AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")


def _parse_ddmmyy(s):
    m = DATE_DDMMYY.match(s.strip())
    if not m:
        return None
    dd, mm, yy = m.groups()
    try:
        year = 2000 + int(yy)
        return date(year, int(mm), int(dd))
    except ValueError:
        return None


def _parse_ddmmyyyy(s):
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(s):
    if not s:
        return None
    s = s.strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# Lines we should never treat as transaction or narration-continuation
SKIP_KEYWORDS = (
    "HDFCBANKLIMITED", "Closingbalanceincludes",
    "Contentsofthisstatement", "PageNo.", "AccountBranch",
    "RTGS/NEFTIFSC", "Date Narration", "Statementof",
    "From :", "JOINTHOLDERS", "Nomination",
    "STATEMENTSUMMARY", "OpeningBalance", "GeneratedOn",
    "ThisisaComputer", "Address :", "City :", "State :",
    "Phoneno.", "Email :", "CustID", "AccountNo",
    "A/COpenDate", "AccountStatus", "BranchCode", "AccountType",
    "Currency", "ODLimit",
)


class HDFCBankParser(BaseParser):
    bank_name = "HDFC Bank"

    # X-coordinate boundaries (approximate, in PDF points)
    WITHDRAWAL_X_MAX = 480
    DEPOSIT_X_MAX = 555

    @classmethod
    def match(cls, first_page_text: str) -> bool:
        text = first_page_text.upper().replace(" ", "")
        # Header-only phrases unique to HDFC layout (not narration text)
        signals = [
            "ACCOUNTBRANCH" in text and "ACCOUNTNO" in text,
            "JOINTHOLDERS" in text or "STATEMENTOFACCOUNT" in text,
            "RTGS/NEFTIFSC" in text,
            "ACCOUNTSTATUS" in text,
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

            current_txn: Optional[Transaction] = None

            for page in pdf.pages:
                words = page.extract_words()
                if not words:
                    continue

                # Group words into lines by 'top' coordinate (±3 pixel tolerance)
                lines: Dict[int, List[dict]] = {}
                for w in words:
                    key_top = round(w["top"])
                    matched = None
                    for k in lines:
                        if abs(k - key_top) <= 3:
                            matched = k
                            break
                    if matched is None:
                        lines[key_top] = []
                        matched = key_top
                    lines[matched].append(w)

                for top in sorted(lines.keys()):
                    line_words = sorted(lines[top], key=lambda w: w["x0"])
                    if not line_words:
                        continue
                    line_text = " ".join(w["text"] for w in line_words)

                    if any(s in line_text for s in SKIP_KEYWORDS):
                        continue

                    first_word = line_words[0]["text"]
                    txn_date = _parse_ddmmyy(first_word)

                    if txn_date is not None:
                        if current_txn is not None:
                            transactions.append(current_txn)
                            current_txn = None
                        current_txn = cls._parse_txn_line(line_words, txn_date)
                    else:
                        # Continuation of previous transaction's narration
                        if current_txn is not None:
                            extra = " ".join(
                                w["text"] for w in line_words
                                if not AMOUNT_RE.match(w["text"])
                                and not DATE_DDMMYY.match(w["text"])
                            ).strip()
                            if extra and len(extra) > 1:
                                current_txn.particulars = (current_txn.particulars + " " + extra).strip()

            if current_txn is not None:
                transactions.append(current_txn)

            if transactions:
                meta.closing_balance = transactions[-1].balance
                first = transactions[0]
                net = (first.credit or 0) - (first.debit or 0)
                if first.balance is not None:
                    meta.opening_balance = first.balance - net

            meta.total_debit = sum(t.debit for t in transactions if t.debit)
            meta.total_credit = sum(t.credit for t in transactions if t.credit)

        return ParsedStatement(metadata=meta, transactions=transactions, charges=[])

    @classmethod
    def _parse_txn_line(cls, line_words, txn_date) -> Optional[Transaction]:
        amounts_in_line = [(i, w) for i, w in enumerate(line_words) if AMOUNT_RE.match(w["text"])]
        if len(amounts_in_line) < 2:
            return None

        last_amounts = amounts_in_line[-2:]

        bal_word = last_amounts[-1][1]
        balance = _parse_amount(bal_word["text"])

        amt_idx, amt_word = last_amounts[0]
        amt_value = _parse_amount(amt_word["text"])
        x_mid = (amt_word["x0"] + amt_word["x1"]) / 2

        debit = None
        credit = None
        if x_mid < cls.WITHDRAWAL_X_MAX:
            debit = amt_value
        elif x_mid < cls.DEPOSIT_X_MAX:
            credit = amt_value
        else:
            return None

        middle = line_words[1:amt_idx]

        value_dt_idx = None
        for i in range(len(middle) - 1, -1, -1):
            if _parse_ddmmyy(middle[i]["text"]):
                value_dt_idx = i
                break

        cheque_no = None
        narration_tokens = middle
        if value_dt_idx is not None and value_dt_idx > 0:
            ref_token = middle[value_dt_idx - 1]
            if re.match(r"^[\dA-Z]{10,}$", ref_token["text"]) or ref_token["text"].startswith("HDFCR"):
                cheque_no = ref_token["text"]
                narration_tokens = middle[:value_dt_idx - 1]
            else:
                narration_tokens = middle[:value_dt_idx]

        narration = " ".join(w["text"] for w in narration_tokens).strip()

        return Transaction(
            date=txn_date,
            particulars=narration,
            debit=debit,
            credit=credit,
            balance=balance,
            cheque_no=cheque_no,
            branch=None,
        )

    @staticmethod
    def _extract_header(text, meta: StatementMetadata):
        m = re.search(r"AccountNo\s*:\s*(\d+)", text)
        if m:
            meta.account_no = m.group(1)
        m = re.search(r"RTGS/NEFTIFSC\s*:\s*(\S+)", text)
        if m:
            meta.ifsc = m.group(1)
        m = re.search(r"MICR\s*:\s*(\d+)", text)
        if m:
            meta.micr = m.group(1)
        m = re.search(r"CustID\s*:\s*(\d+)", text)
        if m:
            meta.customer_id = m.group(1)
        m = re.search(r"Email\s*:\s*(\S+@\S+)", text)
        if m:
            meta.email = m.group(1)
        m = re.search(r"AccountType\s*:\s*([A-Z0-9\(\)\s]+?)(?:\n|$)", text)
        if m:
            meta.account_type = m.group(1).strip()
        m = re.search(r"From\s*:\s*(\d{2}/\d{2}/\d{4})\s+To\s*:\s*(\d{2}/\d{2}/\d{4})", text)
        if m:
            meta.period_from = _parse_ddmmyyyy(m.group(1))
            meta.period_to = _parse_ddmmyyyy(m.group(2))
        m = re.search(r"\n(M/S\.?\s+[A-Z0-9&\s\.\-,]+?)\n", text)
        if m:
            meta.account_holder = m.group(1).strip()
        m = re.search(r"AccountBranch\s*:\s*([A-Z]+)", text)
        if m:
            meta.branch_address = m.group(1).strip()
