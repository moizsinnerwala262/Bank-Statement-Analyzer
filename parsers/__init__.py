"""Parser registry - auto-detects bank from PDF and dispatches."""
import pdfplumber
from datetime import date
from typing import Optional, Type, List
from .base import BaseParser, ParsedStatement, StatementMetadata
from .axis import AxisBankParser
from .hdfc import HDFCBankParser
from .icici import ICICIBankParser
from .icici_retail import ICICIRetailParser
from .bob import BankOfBarodaParser

# Register all parsers here. Add new banks by importing and appending.
REGISTERED_PARSERS: List[Type[BaseParser]] = [
    AxisBankParser,
    ICICIRetailParser,   # new retail/iMobile format - check before corporate
    ICICIBankParser,
    BankOfBarodaParser,
    HDFCBankParser,
    # SBIBankParser,     # to add
    # KotakBankParser,   # to add
]


class UnsupportedBankError(Exception):
    pass


class PasswordRequiredError(Exception):
    pass


def detect_bank(pdf_path: str, password: Optional[str] = None) -> Optional[Type[BaseParser]]:
    """Returns the parser class that matches, or None."""
    open_kwargs = {"password": password} if password else {}
    try:
        with pdfplumber.open(pdf_path, **open_kwargs) as pdf:
            if not pdf.pages:
                return None
            first_text = pdf.pages[0].extract_text() or ""
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypted" in msg:
            raise PasswordRequiredError("PDF is password-protected. Please provide password.")
        raise
    for parser_cls in REGISTERED_PARSERS:
        try:
            if parser_cls.match(first_text):
                return parser_cls
        except Exception:
            continue
    return None


def parse(pdf_path: str, password: Optional[str] = None) -> ParsedStatement:
    parser_cls = detect_bank(pdf_path, password)
    if parser_cls is None:
        supported = ", ".join(p.bank_name for p in REGISTERED_PARSERS)
        raise UnsupportedBankError(
            f"Could not detect bank format. Currently supported: {supported}"
        )
    return parser_cls.parse(pdf_path, password)


def supported_banks() -> List[str]:
    return [p.bank_name for p in REGISTERED_PARSERS]


class AccountMismatchError(Exception):
    pass


def combine_statements(stmts: List[ParsedStatement]) -> ParsedStatement:
    """Combine multiple monthly statements of the SAME account into one.
    Verifies same account_no, sorts by period, merges transactions chronologically.
    """
    if not stmts:
        raise ValueError("No statements to combine")
    if len(stmts) == 1:
        return stmts[0]

    # Verify same account (warn on mismatch but allow if any unset)
    accounts = set(s.metadata.account_no for s in stmts if s.metadata.account_no)
    if len(accounts) > 1:
        raise AccountMismatchError(
            f"Statements are for different accounts: {sorted(accounts)}. "
            f"Combine only works on monthly statements of the same account."
        )

    # Sort by period_from (or transaction first date as fallback)
    def sort_key(s):
        if s.metadata.period_from:
            return s.metadata.period_from
        if s.transactions:
            return s.transactions[0].date
        return date.max
    stmts = sorted(stmts, key=sort_key)

    base = stmts[0].metadata
    combined = StatementMetadata(
        bank=base.bank,
        account_holder=base.account_holder,
        account_no=base.account_no,
        account_type=base.account_type,
        ifsc=base.ifsc,
        micr=base.micr,
        branch_address=base.branch_address,
        customer_id=base.customer_id,
        pan=base.pan,
        mobile=base.mobile,
        email=base.email,
        address=base.address,
    )
    # Period spans all files
    starts = [s.metadata.period_from for s in stmts if s.metadata.period_from]
    ends = [s.metadata.period_to for s in stmts if s.metadata.period_to]
    combined.period_from = min(starts) if starts else None
    combined.period_to = max(ends) if ends else None
    # Earliest opening, latest closing
    combined.opening_balance = stmts[0].metadata.opening_balance
    combined.closing_balance = stmts[-1].metadata.closing_balance
    # Sum totals
    combined.total_debit = sum((s.metadata.total_debit or 0) for s in stmts)
    combined.total_credit = sum((s.metadata.total_credit or 0) for s in stmts)

    all_txns = []
    for s in stmts:
        all_txns.extend(s.transactions)
    all_txns.sort(key=lambda t: t.date)

    all_charges = []
    for s in stmts:
        all_charges.extend(s.charges)

    return ParsedStatement(metadata=combined, transactions=all_txns, charges=all_charges)
