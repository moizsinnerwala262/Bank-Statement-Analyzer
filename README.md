# Bank Statement Analyzer (BSA Tool)

A Python + Streamlit web application that parses Indian bank statement PDFs and produces a 5-sheet Excel analysis report with:

- **Daily Balance Matrix** (day-of-month × month grid of EOD balances)
- **Statement** (PDF rendered as clean Excel)
- **EMI Extract** (auto-detected loan obligations with lender, mandate, monthly amount grid)
- **Summary** (ABB metrics + EMI obligation total)
- **Balance Chart** (daily EOD trend line)

## Currently Supported Banks

- ✅ Axis Bank
- ✅ HDFC Bank (text-based PDF; Savings, Current, BizElite)

## Coming Soon

- ICICI Bank, SBI, Kotak Mahindra, Yes Bank, IndusInd, Bank of Baroda, PNB, AU Small Finance, IDFC First, Federal Bank, RBL.

To add a new bank, see [Adding a New Bank Parser](#adding-a-new-bank-parser) below.

---

## Local Setup (Test on Your Machine)

**Prerequisites:** Python 3.10+

```bash
# Clone or download this repo, then:
cd bsa_tool

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate    # Mac/Linux
# OR
venv\Scripts\activate       # Windows

# Install dependencies
pip install -r requirements.txt

# Run the app
streamlit run app.py
```

Browser opens at `http://localhost:8501`.

---

## Deploy to Streamlit Cloud (FREE Public URL)

1. **Create a free GitHub account** if you don't have one.
2. **Create a new GitHub repository** (e.g. `bsa-tool`) and push this entire folder to it.
3. **Sign up at https://share.streamlit.io** with your GitHub.
4. Click **"New app"** → select your repo → set "Main file path" to `app.py`.
5. Click **Deploy**. Your app gets a public URL like `https://your-app.streamlit.app`.

Streamlit Cloud provides:
- Free hosting (1 GB RAM, sleeps after inactivity)
- HTTPS automatically
- Auto-redeploy on git push

**Limitations:** Free tier sleeps after 7 days of no traffic; 1 GB RAM only. Sufficient for first 100-500 users. Beyond that, move to a paid VPS (₹400-800/month on Hetzner / DigitalOcean).

---

## Project Structure

```
bsa_tool/
├── app.py                      # Streamlit main app
├── requirements.txt
├── README.md
├── parsers/
│   ├── __init__.py             # Bank auto-detection + dispatch
│   ├── base.py                 # Transaction, StatementMetadata, BaseParser
│   └── axis.py                 # Axis Bank parser
├── analyzers/
│   ├── __init__.py
│   ├── abb.py                  # ABB / daily EOD matrix calculation
│   └── emi.py                  # EMI / recurring obligation detector
├── output/
│   ├── __init__.py
│   └── excel_builder.py        # 5-sheet Excel report generator
└── samples/
    └── README.md
```

---

## Adding a New Bank Parser

1. Create `parsers/<bankname>.py` (e.g. `parsers/hdfc.py`)
2. Subclass `BaseParser`:

```python
from .base import BaseParser, Transaction, StatementMetadata, ParsedStatement
import pdfplumber

class HDFCBankParser(BaseParser):
    bank_name = "HDFC Bank"

    @classmethod
    def match(cls, first_page_text: str) -> bool:
        return "HDFC BANK" in first_page_text.upper()

    @classmethod
    def parse(cls, pdf_path, password=None):
        # ... extract transactions using pdfplumber
        # ... return ParsedStatement(metadata=..., transactions=[...])
        pass
```

3. Register in `parsers/__init__.py`:

```python
from .hdfc import HDFCBankParser

REGISTERED_PARSERS = [
    AxisBankParser,
    HDFCBankParser,    # <-- add here
]
```

That's it. The Streamlit app and Excel builder automatically work with the new bank because they only depend on the `Transaction` and `StatementMetadata` interfaces.

---

## Architecture Notes

**Why this design:**

- **Decoupled parsers** — each bank PDF has a different layout. Parsers convert PDF → normalized `Transaction` list. Everything downstream (ABB, EMI, Excel) is bank-agnostic.
- **In-memory processing** — files never written to disk except a temporary OS-managed location during a single request. Auto-deleted after processing. No database.
- **Formula-driven Excel** — ABB metrics use Excel `AVERAGE()`, `MIN()`, etc. formulas referencing the daily matrix. Users can edit the matrix and watch metrics auto-recalculate.

**Known limitations:**

- Only text-based PDFs work. Scanned/image PDFs need OCR (next iteration — Tesseract or Anthropic Claude vision API).
- Password-protected PDFs work if user provides the password.
- EMI detector relies on a lender keyword list (`analyzers/emi.py`). Unknown lenders won't be flagged — expand the list as needed.

---

## Roadmap (Next Modules)

1. **Income / inflow detection** — salary credits, business receipts, identify avg monthly inflow → enables FOIR calculation
2. **Bounce / reversal detection** — ECS return, NACH reversal, cheque return narrations
3. **Inter-account transfer detection** — strip self-transfers from genuine inflow
4. **Cash deposit / withdrawal pattern**
5. **High-value transaction flagging**
6. **OCR support** for scanned PDFs
7. **PDF report output** (in addition to Excel)

---

## License

For your own use. Not for redistribution.
