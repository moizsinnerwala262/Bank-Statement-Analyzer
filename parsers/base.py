"""Base parser interface - all bank parsers inherit from this."""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, List, Dict, Any


@dataclass
class Transaction:
    date: date
    particulars: str
    debit: Optional[float] = None
    credit: Optional[float] = None
    balance: Optional[float] = None
    cheque_no: Optional[str] = None
    branch: Optional[str] = None


@dataclass
class StatementMetadata:
    """Account info extracted from PDF header."""
    bank: str = ""
    account_holder: str = ""
    account_no: str = ""
    account_type: str = ""
    ifsc: str = ""
    micr: str = ""
    branch_address: str = ""
    customer_id: str = ""
    pan: str = ""
    mobile: str = ""
    email: str = ""
    address: str = ""
    period_from: Optional[date] = None
    period_to: Optional[date] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    total_debit: Optional[float] = None
    total_credit: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedStatement:
    metadata: StatementMetadata
    transactions: List[Transaction]
    charges: List[Dict[str, Any]] = field(default_factory=list)


class BaseParser:
    """Subclass per bank. Override `match` and `parse`."""
    bank_name: str = "Unknown"

    @classmethod
    def match(cls, first_page_text: str) -> bool:
        """Return True if this parser handles the given PDF (called on raw text of page 1)."""
        return False

    @classmethod
    def parse(cls, pdf_path: str, password: Optional[str] = None) -> ParsedStatement:
        raise NotImplementedError
