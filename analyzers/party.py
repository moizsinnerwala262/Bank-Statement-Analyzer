"""Party-wise analysis: extract counterparty from narration, aggregate by month."""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, List, Tuple, Dict
from parsers.base import ParsedStatement, Transaction


@dataclass
class PartyAggregate:
    party: str                  # Display name
    norm_key: str               # Normalized key for grouping
    mode: str                   # Most common mode used
    direction: str              # 'CREDIT' or 'DEBIT'
    txn_count: int = 0
    total_amount: float = 0.0
    months_active: int = 0
    largest_txn: float = 0.0
    first_date: Optional[date] = None
    last_date: Optional[date] = None
    by_month: Dict[Tuple[int, int], float] = field(default_factory=dict)
    txn_type: str = "Other"     # Business / Related / EMI / Charges / Tax / Cash / Salary / Self / Other


def _norm_key(name: str) -> str:
    """Strip spaces/punctuation for grouping; keep display name separate."""
    return re.sub(r"[\s\.\-_,/&()]", "", name).upper()


# Pattern handlers in priority order. Each returns (party, mode) or None.
_PATTERN_HANDLERS = []


def _handler(fn):
    _PATTERN_HANDLERS.append(fn)
    return fn


@_handler
def _h_cash_deposit(n):
    if re.search(r"CASH\s*DEPOSIT", n, re.I):
        return ("Cash Deposit (Self)", "CASH")
    return None


@_handler
def _h_interest_paid(n):
    if re.search(r"Int\.Pd|INT\.PD|INTEREST\s*PAID|INT\sCOLL", n, re.I):
        return ("Interest (Bank)", "INTEREST")
    return None


@_handler
def _h_charges(n):
    if re.search(r"ECS\s*Txn\s*Chrgs|Charges|Service\s*Charge|GST", n, re.I) and not re.search(r"^UPI|^NEFT|^IMPS|^RTGS", n, re.I):
        return ("Bank Charges", "CHARGES")
    return None


@_handler
def _h_icici_imps_charges(n):
    # ICICI: "IMPS Chg May25+GST"
    if re.match(r"IMPS\s*Chg|N\s*chg", n, re.I):
        return ("Bank Charges (IMPS/NEFT)", "CHARGES")
    return None


@_handler
def _h_icici_cash_paid(n):
    if re.match(r"CASH\s*PAID:?\s*Self", n, re.I):
        return ("Cash Withdrawal (Self)", "CASH")
    return None


@_handler
def _h_icici_mmt_imps(n):
    # ICICI: MMT/IMPS/<ref>/<remarks>/<NAME>/<IFSC>
    m = re.match(r"MMT/IMPS/\d+/.+?/([^/]+)/[A-Z]{4,}\d", n, re.I)
    if m:
        name = m.group(1).strip()
        if name and len(name) > 1:
            return (name, "IMPS")
    return None


@_handler
def _h_icici_inft(n):
    # ICICI internal transfer: INF/INFT/<ref>/<remarks_or_NAME>/<NAME>
    m = re.match(r"INF/INFT/\d+/(.+)$", n, re.I)
    if m:
        parts = [p.strip() for p in m.group(1).split("/") if p.strip()]
        if parts:
            return (parts[-1], "INFT")
    return None


@_handler
def _h_icici_inf_neft(n):
    # ICICI: INF/NEFT/<ref>/<IFSC>/<remarks>/<NAME>
    m = re.match(r"INF/NEFT/[A-Z0-9]+/[A-Z]+\d+/(.+)$", n, re.I)
    if m:
        parts = [p.strip() for p in m.group(1).split("/") if p.strip()]
        if parts:
            return (parts[-1], "NEFT")
    return None


@_handler
def _h_icici_cms(n):
    # ICICI bulk payment: "CMS/ /AMBUJA CEMENTS LIMITED" or "CMS/<ref>/<NAME>"
    # ICICI retail: "CMS/001919751491/ADINGEN__ADITYA BIRLA CAPITAL LTD"
    m = re.match(r"CMS/\s*/?(.*)$", n, re.I)
    if m:
        rest = m.group(1).strip()
        if not rest:
            return None
        # If form CMS/<ref>/<NAME>, take last part
        if "/" in rest:
            parts = [p.strip() for p in rest.split("/") if p.strip()]
            if parts and not re.match(r"^\d+_", parts[-1]):
                name = re.sub(r"^[A-Z]+__", "", parts[-1])  # strip "ADINGEN__" prefix
                return (name.strip().title(), "CMS")
        rest = re.sub(r"^[A-Z]+__", "", rest)
        return (rest.strip().title(), "CMS")
    return None


@_handler
def _h_icici_ach_slash(n):
    # ICICI direct debit: ACH/<MERCHANT>/<ref>/<details>
    m = re.match(r"ACH/([^/]+)/", n, re.I)
    if m:
        name = m.group(1).strip()
        # ACH/CTRAZORPAY is L&T Finance routed via Razorpay
        if "CTRAZORPAY" in name.upper() or "RAZORPAY" in name.upper():
            # Check if narration mentions LTFINANCE
            if "LTFINANCE" in n.upper() or "L&T" in n.upper():
                return ("L&T Finance (via Razorpay)", "ACH")
            return ("Razorpay Direct Debit", "ACH")
        return (name, "ACH")
    return None


@_handler
def _h_icici_bill(n):
    # ICICI online bill payment: BIL/ONL/<ref>/<MERCHANT>/<remarks>
    m = re.match(r"BIL/ONL/\d+/([^/]+)/", n, re.I)
    if m:
        return (m.group(1).strip(), "BILL")
    # Also BIL/<other>/<ref>
    m = re.match(r"BIL/[A-Z]+/\d+/([^/]+)", n, re.I)
    if m:
        return (m.group(1).strip(), "BILL")
    return None


@_handler
def _h_icici_gib(n):
    # ICICI tax payment: GIB/<ref>/DTAX/<ref>
    if re.match(r"GIB/", n, re.I):
        if "DTAX" in n.upper():
            return ("Direct Tax / GST", "TAX")
        if "IDTX" in n.upper():
            return ("Indirect Tax", "TAX")
        return ("Statutory Payment", "TAX")
    return None


@_handler
def _h_icici_clg(n):
    # ICICI cheque clearing: "<chq_no> CLG/<MERCHANT>/<bank>"
    m = re.match(r"\d+\s+CLG/([^/]+)/", n, re.I)
    if m:
        return (m.group(1).strip(), "CHEQUE")
    return None


@_handler
def _h_icici_rtgs(n):
    # SBI bulk corporate settlement: contains PCVO/SLP/SPL marker tokens
    if re.search(r"\bPCVO\d+|\bSLP\d+|\bSPL\d+", n, re.I):
        return ("Bulk Corporate Settlement (SBI CMS)", "RTGS")
    # ICICI incoming RTGS: "RTGS-<BANK>R\d+-<NAME>-<acct>-<IFSC>"
    m = re.match(r"RTGS-?[A-Z]{4}R?\d+-(.+?)(?:-\d{12,}|-[A-Z]{4}R?\d{6,})", n, re.I)
    if m:
        return (re.sub(r"\s+", " ", m.group(1).strip()), "RTGS")
    # ICICI outgoing RTGS via netbanking: "RTGS/ICICR\d+/<IFSC>/<NAME>"
    m = re.match(r"RTGS/[A-Z0-9]+/[A-Z]+\d+/(.+)$", n, re.I)
    if m:
        return (m.group(1).strip(), "RTGS")
    return None


@_handler
def _h_icici_neft_incoming(n):
    # ICICI incoming NEFT: "NEFT-<BANK>N\d+-<NAME>-..." OR "NEFTBARBN5...-NAME-..."
    m = re.match(r"NEFT-?[A-Z]{4}[NU]?\d+-(.+?)(?:-{2,}|-\d{12,})", n, re.I)
    if m:
        return (re.sub(r"\s+", " ", m.group(1).strip()), "NEFT")
    return None


@_handler
def _h_bob_loan_recovery(n):
    # BOB internal loan EMI: "Loan Recovery For18670600002810"
    m = re.match(r"Loan Recovery For\s*(\d+)", n, re.I)
    if m:
        return (f"BOB Loan A/C ...{m.group(1)[-4:]}", "ACH")
    return None


@_handler
def _h_bob_charges(n):
    # BOB charge patterns: DCARDFEE, SMS Charges, VETTING CHARGE, TDS U/S 194N, Charges for PORD
    if re.search(r"^DCARDFEE|^SMS Charges|^SMS-USG-CRG|^VETTING CHARGE|^TDS U/S 194N|^Charges for PORD", n, re.I):
        return ("Bank Charges (BOB)", "CHARGES")
    return None


@_handler
def _h_bob_cash_withdrawal(n):
    # BOB cash via cheque at branch: "TO CASH 130" or "CASH TO SELF 28"
    if re.match(r"TO CASH(\s|$)|CASH TO SELF", n, re.I):
        return ("Cash Withdrawal (Branch)", "CASH")
    return None


@_handler
def _h_bob_cheque_deposit(n):
    # BOB cheque deposit by clearing: "BY INST 3546 : MICR CLG (CTS)"
    if re.match(r"BY INST\s+\d+", n, re.I):
        return ("Cheque Deposit (MICR CLG)", "CHEQUE")
    return None


@_handler
def _h_bob_ach_credit(n):
    # BOB incoming ACH credit: "ACHCR/M1 RI 240225 000473/5145587298/110764876447"
    if re.match(r"ACHCR/", n, re.I):
        return ("ACH Credit (Incoming)", "ACH-CR")
    return None


@_handler
def _h_bob_ebank(n):
    # BOB Internet Banking outgoing: "EBANK:<ref>/<MERCHANT>/<bill_id>/BILLD"
    m = re.match(r"EBANK:\d+/([^/]+)/", n, re.I)
    if m:
        merchant = m.group(1).strip()
        upper_full = n.upper()
        upper_m = merchant.upper()
        if "FASTAG" in upper_m:
            return ("ICICI FASTag", "BILL")
        if "CYBER TREASURY" in upper_full or "DTI" in upper_full or "BHR2025" in upper_full:
            return ("Tax Payment (Govt)", "TAX")
        if "SUNDARAMFIN" in upper_m or "SUNDARAM" in upper_m:
            return ("Sundaram Finance (Bill)", "BILL")
        if "CCAVENUES" in upper_full or "CC AVENUES" in upper_full:
            return ("Online Portal (CCAvenues)", "BILL")
        if "BARB" in upper_m:
            return ("BOB Internal Transfer", "TRANSFER")
        return (merchant, "BILL")
    # BOB Internet Banking - Web Internet Banking (often self): "EBANK:WIB/<ref>/<remark>"
    if re.match(r"EBANK:WIB/", n, re.I):
        return ("Internet Banking (Self)", "TRANSFER")
    return None


@_handler
def _h_bob_neft(n):
    # BOB outgoing NEFT: "NEFT-BARBR25118470336-NEELKANTH AUTO ADVISOR-STATE"
    # Format: NEFT-BARB<letter><utr>-<NAME>-<dest_bank>
    m = re.match(r"NEFT-BARB[A-Z]\d+-(.+?)-[A-Z][A-Z\s]{2,}$", n, re.I)
    if m:
        return (m.group(1).strip(), "NEFT")
    # Fallback for outgoing without clear bank suffix
    m = re.match(r"NEFT-BARB[A-Z]\d+-(.+)$", n, re.I)
    if m:
        return (m.group(1).strip(), "NEFT")
    # BOB incoming NEFT: "NEFT-<OTHERBANK><letter><utr>-<NAME>"
    # E.g., NEFT-HDFCH00207436577-DEVGIRI ENTERPRISE
    #       NEFT-SBIN425007950002-INDIAN OIL CORPORATION LTD
    # Lazy capture stops at first dash so we don't swallow "-ATTN/..." suffixes
    m = re.match(r"NEFT-([A-Z]{4})[A-Z]?\d{8,}-(.+?)(?:-|$)", n, re.I)
    if m and m.group(1).upper() != "BARB":
        return (m.group(2).strip(), "NEFT")
    # BOB CMS NEFT: "NEFT-CMS0352583031309-PRISM JOHNSON LIMITED RMC IN"
    m = re.match(r"NEFT-CMS\d+-(.+)$", n, re.I)
    if m:
        return (m.group(1).strip(), "NEFT")
    # BOB numeric UTR DC format: "NEFT-38641729191DC-OMEGA LOGISTICS AND"
    m = re.match(r"NEFT-\d+(?:DC)?-(.+)$", n, re.I)
    if m:
        return (m.group(1).strip(), "NEFT")
    return None


@_handler
def _h_bob_rtgs(n):
    # BOB outgoing RTGS: "RTGS-BARBR52025042800963850-ASTHA AUTOMOBILES-HDFC"
    m = re.match(r"RTGS-BARB[A-Z]\d+-(.+?)-[A-Z][A-Z]{2,}$", n, re.I)
    if m:
        return (m.group(1).strip(), "RTGS")
    m = re.match(r"RTGS-BARB[A-Z]\d+-(.+)$", n, re.I)
    if m:
        return (m.group(1).strip(), "RTGS")
    return None


@_handler
def _h_imps_masked(n):
    # IMPS to masked linked account: IMPS/P2A/<ref>/XXXXXXXXXX<last4>/<remark>
    m = re.match(r"IMPS/P2A/\d+/X+(\d{4})/", n, re.I)
    if m:
        return (f"Linked A/C ...{m.group(1)}", "IMPS")
    return None


@_handler
def _h_iciretail_reject(n):
    # ICICI retail bounced cheque: "REJECT:301822:FUNDS INSUFFICIENT"
    if re.match(r"REJECT:\d+", n, re.I) or re.search(r"FUNDS\s*INSUFFICIENT", n, re.I):
        if re.match(r"RTN\s*CHG", n, re.I):
            return ("Cheque Return Charge", "CHARGES")
        return ("Cheque Return (Inward Bounce)", "RETURN")
    if re.match(r"RTN\s*CHG", n, re.I):
        return ("Cheque Return Charge", "CHARGES")
    return None


@_handler
def _h_iciretail_clg(n):
    # ICICI retail cheque clearing (inward deposit): "CLG/S A MARKETING/001451/HDF/31.03.2026"
    m = re.match(r"CLG/([^/]+)/", n, re.I)
    if m:
        return (m.group(1).strip().title(), "CHEQUE")
    return None


@_handler
def _h_iciretail_neft_credit(n):
    # ICICI retail NEFT: "NEFTKVBLH00258436986-FANCY ZOOMER GALLERY-NA NANA"
    #                    "NEFT-SDCBN26092463268-DIPIKA LIGHTS-ATTN/MANSI COR"
    m = re.match(r"NEFT-?[A-Z]{4}[A-Z0-9]+-(.+?)(?:-|$)", n, re.I)
    if m:
        name = m.group(1).strip()
        # Avoid catching BOB outgoing (NEFT-BARB...) which is handled earlier
        if name and not name.upper().startswith("BARB"):
            return (name.title(), "NEFT")
    return None


@_handler
def _h_iciretail_upi(n):
    # ICICI retail UPI: "UPI/645780583737/UPI/pradeepmishra82/The Kalupur C"
    m = re.match(r"UPI/\d+/UPI/([^/]+)/", n, re.I)
    if m:
        return (m.group(1).strip(), "UPI")
    return None


@_handler
def _h_iciretail_mmt(n):
    # ICICI retail Mobile Money Transfer (IMPS): "MMT/IMPS/612211863487/KKBKTransfer/SAMARTH EN/Kota"
    m = re.match(r"MMT/IMPS/\d+/[^/]*/([^/]+)/", n, re.I)
    if m:
        name = m.group(1).strip()
        if name and name.lower() not in ("payout", "transfer"):
            return (name.title(), "IMPS")
        return ("IMPS Transfer", "IMPS")
    return None


@_handler
def _h_iciretail_cash(n):
    # ICICI retail cash deposit: "BY CASH - SURAT UDHANA"
    if re.match(r"BY CASH", n, re.I):
        return ("Cash Deposit", "CASH")
    return None


@_handler
def _h_iciretail_trf(n):
    # ICICI retail transfer: "TRF/DEEP ELECTRIC AND REWINDING/ICI"
    m = re.match(r"TRF/(.+?)/[A-Z]{2,4}$", n, re.I)
    if m:
        return (m.group(1).strip().title(), "TRANSFER")
    m = re.match(r"TRF/(.+)$", n, re.I)
    if m:
        return (m.group(1).strip().title(), "TRANSFER")
    return None


@_handler
def _h_iciretail_cms(n):
    # ICICI retail CMS bulk payment: "CMS/001919751491/ADINGEN__ADITYA BIRLA CAPITAL LTD"
    m = re.match(r"CMS/\d+/(.+)$", n, re.I)
    if m:
        name = m.group(1).strip()
        name = re.sub(r"^[A-Z]+__", "", name)  # strip "ADINGEN__" prefix
        return (name.title(), "CMS")
    return None


@_handler
def _h_upi(n):
    m = re.match(r"UPI/P2[AM]/\d+/([^/]+?)/", n, re.I)
    if m:
        name = m.group(1).strip()
        if name and len(name) > 1:
            return (name, "UPI")
    # Alt: UPI/CRADJ/... (UPI credit adjustment) - skip party extraction
    m = re.match(r"UPI/CRADJ/", n, re.I)
    if m:
        return ("UPI Adjustment", "UPI")
    return None


@_handler
def _h_neft(n):
    # NEFT/<ref>/<NAME>/<bank>/...
    m = re.match(r"NEFT/[A-Z0-9]+/([^/]+?)/", n, re.I)
    if m:
        return (m.group(1).strip(), "NEFT")
    # NEFT/MB/<ref>/<NAME>/<bank>/
    m = re.match(r"NEFT/MB/[A-Z0-9]+/([^/]+?)/", n, re.I)
    if m:
        return (m.group(1).strip(), "NEFT")
    return None


@_handler
def _h_imps(n):
    m = re.match(r"IMPS/P2[AM]/\d+/([^/]+?)/", n, re.I)
    if m:
        return (m.group(1).strip(), "IMPS")
    return None


@_handler
def _h_rtgs_hdfc(n):
    # HDFC: RTGSDR-IBKL0000224-KAMLESHKUMAR DHIRUBHAI... (debit) or RTGSCR-... (credit)
    m = re.match(r"RTGS[CD]R-[A-Z]+\d+-(.+?)(?:-VRINDAVAN|-VINDAVAN|-SILVASSA|-NETBANK|-MUM|\s+\d{5,}|-HDFCR|$)", n, re.I)
    if m:
        name = m.group(1).strip()
        name = re.sub(r"\s+", "", name)  # HDFC name-wrap artifacts
        if name and len(name) > 1:
            return (name, "RTGS")
    # Generic RTGS
    m = re.match(r"RTGS[-/]([A-Z0-9]+)[/-](.+?)(?:/|$)", n, re.I)
    if m:
        return (m.group(2).strip(), "RTGS")
    return None


@_handler
def _h_tpt_hdfc(n):
    # Variant 1: <acct>-TPT-TRANSFER-<NAME>  (NAME may wrap across lines -> internal spaces)
    m = re.search(r"TPT-TRANSFER-(.+)$", n, re.I)
    if m:
        name = re.sub(r"\s+", "", m.group(1).strip())
        if name and len(name) > 1:
            return (name, "TPT")
    # Variant 2: <acct>-TPT-HDFC<hex>-<NAME>  (HDFC's debit-card-based transfer)
    m = re.search(r"TPT-HDFC[A-Z0-9]+-(.+)$", n, re.I)
    if m:
        name = re.sub(r"\s+", "", m.group(1).strip())
        if name and len(name) > 1:
            return (name, "TPT")
    # Variant 3: -TPT-NB<text>-<NAME>
    m = re.search(r"-TPT-NB[A-Z0-9]+-(.+)$", n, re.I)
    if m:
        name = re.sub(r"\s+", "", m.group(1).strip())
        if name and len(name) > 1:
            return (name, "TPT")
    return None


@_handler
def _h_ft_cr(n):
    # HDFC: FT-CR-<account>-<NAME>
    m = re.match(r"FT-CR-\d+-(.+)$", n, re.I)
    if m:
        name = re.sub(r"\s+", "", m.group(1).strip())
        if name and len(name) > 1:
            return (name, "FT-CR")
    return None


@_handler
def _h_hdfc_upi(n):
    # HDFC UPI: UPI-<id>-<vpa>@<handle>-<ref>-<remarks>
    # Each end-customer has unique VPA - roll up all small payments
    m = re.match(r"UPI-[\w\d]+-([\w\d]+)@(\S+?)-", n, re.I)
    if m:
        return ("UPI Customer Payments", "UPI")
    return None


@_handler
def _h_settlement(n):
    if re.match(r"^UPISETTLEMENT", n, re.I):
        return ("UPI Settlement (Bank)", "SETTLE")
    if re.search(r"CARDSSETTL", n, re.I):
        return ("Card Settlement (POS/Online)", "SETTLE")
    return None


@_handler
def _h_razorpay(n):
    if re.search(r"RAZPOM|RAZORPAY", n, re.I):
        return ("Razorpay Settlement", "PG")
    return None


@_handler
def _h_mob_tpft(n):
    # Axis internal: MOB/TPFT/<NAME>/<acct>
    m = re.match(r"MOB/TPFT/([^/]+?)/", n, re.I)
    if m:
        return (m.group(1).strip(), "INTERNAL")
    return None


@_handler
def _h_term_deposit(n):
    # MOB-TD/<ref>/<NAME> or MBBTD/<ref>/<dt>/<NAME>
    if re.match(r"(MOB-TD|MBBTD)/", n, re.I):
        return ("Term Deposit (Self)", "TD")
    return None


@_handler
def _h_ach(n):
    # ACH-DR-<NAME>-UTIB or ACH-DR-TP ACH <NAME>-UTIB
    m = re.match(r"ACH-DR-(?:TP\s+ACH\s+)?(.+?)-UTIB", n, re.I)
    if m:
        name = m.group(1).strip()
        # Strip trailing variable digits (5+) from mandate IDs
        name = re.sub(r"\d{5,}$", "", name).strip()
        return (name, "ACH")
    # Plain ACH-DR-<NAME> (no UTIB suffix)
    m = re.match(r"ACH-DR-([^/]+?)(?:\d{5,}|-UT)", n, re.I)
    if m:
        return (m.group(1).strip(), "ACH")
    return None


@_handler
def _h_ecs(n):
    # ECS/<umrn>/<NAME> (possibly with suffix like _SMS OT)
    m = re.match(r"ECS/[A-Z0-9]+/(.+)", n, re.I)
    if m:
        name = m.group(1).strip()
        # Drop trailing tokens like "_SMS OT"
        name = re.split(r"[_]", name)[0].strip()
        if name:
            return (name, "ECS")
    return None


@_handler
def _h_nbsm(n):
    # NBSM/<ref>/<NAME>(...
    m = re.match(r"NBSM/\d+/([^(]+?)(?:\(|$)", n, re.I)
    if m:
        return (m.group(1).strip(), "NBSM")
    return None


@_handler
def _h_emandate(n):
    if re.match(r"EMANDATE|E-MANDATE|E\.MANDATE", n, re.I):
        m = re.search(r"MANDATE[/-](.+?)(?:/|-|$)", n, re.I)
        if m:
            return (m.group(1).strip(), "EMANDATE")
        return ("E-Mandate (unparsed)", "EMANDATE")
    return None


@_handler
def _h_bcb(n):
    # BCB/<branch>/<ref> - Branch Cash Bouncing or similar
    if n.startswith("BCB/"):
        return ("Bounce / Return", "BOUNCE")
    return None


@_handler
def _h_jno_jir_brc(n):
    # These appear in loan-account-style narrations
    if re.match(r"^(JIR|JNO|BRC|BPY)/", n):
        return ("Internal Adjustment", "INTERNAL")
    return None


def _norm_for_extraction(n: str) -> str:
    """Remove PDF line-wrap artifact spaces inside refs/IFSC codes (mainly ICICI)."""
    if not n:
        return n
    # Space immediately after slash or dash (PDF line breaks at column boundaries)
    n = re.sub(r"/\s+", "/", n)
    n = re.sub(r"-\s+(?=[A-Z0-9])", "-", n)
    # Join consecutive digit groups separated by single space
    n = re.sub(r"(\d)\s+(\d)", r"\1\2", n)
    # Join broken IFSC patterns like "SB IN0009548" -> "SBIN0009548"
    n = re.sub(r"\b([A-Z]{2,3})\s+([A-Z]{1,3}\d{4,7})\b", r"\1\2", n)
    return n


def extract_party(narration: str) -> Tuple[str, str]:
    """Returns (display_name, mode). Falls back to ('Unclassified', 'OTHER')."""
    if not narration:
        return ("Unclassified", "OTHER")
    narration = _norm_for_extraction(narration)
    for handler in _PATTERN_HANDLERS:
        try:
            res = handler(narration)
            if res:
                name, mode = res
                # Clean up name: collapse whitespace, strip
                name = re.sub(r"\s+", " ", name).strip()
                if not name:
                    return ("Unclassified", "OTHER")
                return (name, mode)
        except Exception:
            continue
    return ("Unclassified", "OTHER")


def analyze_parties(stmt: ParsedStatement,
                    min_txns: int = 2,
                    min_amount: float = 10000.0
                    ) -> Tuple[List[PartyAggregate], List[PartyAggregate]]:
    """Returns (credit_parties, debit_parties), each sorted by total_amount DESC.

    Parties with < min_txns AND < min_amount are folded into a single
    'Other (N parties)' aggregate row at the bottom.
    """
    cr_groups: Dict[str, PartyAggregate] = {}
    dr_groups: Dict[str, PartyAggregate] = {}

    for t in stmt.transactions:
        narration = t.particulars or ""
        party_name, mode = extract_party(narration)
        key = _norm_key(party_name) or "UNCLASSIFIED"

        if t.credit and t.credit > 0:
            target = cr_groups
            direction = "CREDIT"
            amt = t.credit
        elif t.debit and t.debit > 0:
            target = dr_groups
            direction = "DEBIT"
            amt = t.debit
        else:
            continue

        if key not in target:
            target[key] = PartyAggregate(
                party=party_name,
                norm_key=key,
                mode=mode,
                direction=direction,
            )
        agg = target[key]
        agg.txn_count += 1
        agg.total_amount += amt
        agg.largest_txn = max(agg.largest_txn, amt)
        if agg.first_date is None or t.date < agg.first_date:
            agg.first_date = t.date
        if agg.last_date is None or t.date > agg.last_date:
            agg.last_date = t.date
        ym = (t.date.year, t.date.month)
        agg.by_month[ym] = agg.by_month.get(ym, 0) + amt

    for groups in (cr_groups, dr_groups):
        for agg in groups.values():
            agg.months_active = len(agg.by_month)
            agg.txn_type = _classify_party_type(agg)

    def split_and_consolidate(groups: Dict[str, PartyAggregate]) -> List[PartyAggregate]:
        kept = []
        small = []
        for agg in groups.values():
            if agg.txn_count >= min_txns or agg.total_amount >= min_amount:
                kept.append(agg)
            else:
                small.append(agg)
        kept.sort(key=lambda a: -a.total_amount)
        if small:
            other = PartyAggregate(
                party=f"Other ({len(small)} parties)",
                norm_key="__OTHER__",
                mode="MIXED",
                direction=small[0].direction,
            )
            for s in small:
                other.txn_count += s.txn_count
                other.total_amount += s.total_amount
                other.largest_txn = max(other.largest_txn, s.largest_txn)
                if other.first_date is None or (s.first_date and s.first_date < other.first_date):
                    other.first_date = s.first_date
                if other.last_date is None or (s.last_date and s.last_date > other.last_date):
                    other.last_date = s.last_date
                for ym, v in s.by_month.items():
                    other.by_month[ym] = other.by_month.get(ym, 0) + v
            other.months_active = len(other.by_month)
            other.txn_type = "Other"
            kept.append(other)
        return kept

    return split_and_consolidate(cr_groups), split_and_consolidate(dr_groups)


# ----------------------- Transaction type classifier -----------------------
# Used to label each PartyAggregate row as Business / Related / EMI / Charges /
# Tax / Cash / Salary / Self / Other - for credit-underwriting cash flow analysis.

_BUSINESS_KEYWORDS = re.compile(
    r"\b(LIMITED|LTD|PVT|PRIVATE LIMITED|"
    r"PRIVATELIMITED|"  # spaces sometimes stripped from name display
    r"COMPANY|"
    r"CORPORATION|CORP|INC|INCORPORATED|"
    r"ENTERPRISES?|ENTERPRISE|"
    r"INDUSTRIES|INDUSTRY|"
    r"SERVICES|SOLUTIONS|"
    r"AUTOMOBILES?|MOTORS?|PETROLEUM|CEMENT|STEEL|"
    r"OIL CORPORATION|REFINERY|CHEMICALS?|"
    r"TRADERS?|TRADING|MERCHANTS?|"
    r"ASSOCIATES|GROUP|HOLDINGS?|VENTURES?|"
    r"CONSULTANCY|CONSULTANTS?|INFRA|MARKET|"
    r"LOGISTICS|TRANSPORT|FREIGHT|CARRIERS?|"
    r"PROJECTS?|PROPERTIES|REALTY)\b",
    re.IGNORECASE,
)

_RELATED_PARTY_KEYWORDS = re.compile(
    r"\b(OMEGA LOGISTICS|GIRJA|DEVGIRI|MERCURIAL|"
    r"LINKED A/?C)\b",
    re.IGNORECASE,
)


def _classify_party_type(agg: PartyAggregate) -> str:
    """Classify a party for credit-underwriting reporting."""
    name = agg.party.upper()
    mode = (agg.mode or "").upper()

    # Highest priority - explicit categorisations from the mode
    if mode == "BOB-LOAN" or "LOAN A/C" in name:
        return "EMI / Loan"
    if mode == "ACH" or mode == "ACH-DR":
        return "EMI / Loan"
    if mode == "CHARGES" or "BANK CHARGES" in name:
        return "Bank Charges"
    if mode == "TAX" or "TAX" in name or "DTAX" in name or "GST" in name:
        return "Tax / Statutory"
    if mode == "CASH" or "CASH WITHDRAWAL" in name or "CASH DEPOSIT" in name:
        return "Cash"
    if mode == "CHEQUE" and ("CHEQUE DEPOSIT" in name or "MICR" in name):
        return "Cheque Deposit"
    if "CHEQUE RETURN" in name or "BOUNCE" in name:
        return "Cheque Return"
    if "FASTAG" in name:
        return "Vehicle Expense (FASTag)"

    # Related-party detection (self + sister concerns + linked accounts)
    if _RELATED_PARTY_KEYWORDS.search(name):
        return "Related Party / Self"

    # Salary detection - small amounts, person names with frequent NEFT/IMPS
    # (heuristic: average txn 5k-50k AND debit AND >=3 txns)
    if (agg.direction == "DEBIT"
            and 3 <= agg.txn_count <= 100
            and 5000 <= (agg.total_amount / max(agg.txn_count, 1)) <= 60000
            and mode in ("NEFT", "IMPS", "TRANSFER")
            and not _BUSINESS_KEYWORDS.search(name)
            and len(name.split()) <= 4):
        return "Salary / Wages"

    # Business detection: name contains corporate keyword
    if _BUSINESS_KEYWORDS.search(name):
        return "Business"

    # High-value RTGS counterparties default to business
    if mode == "RTGS" and agg.total_amount >= 500000:
        return "Business"

    if name in ("UNCLASSIFIED",):
        return "Other / Unclassified"

    return "Other"
