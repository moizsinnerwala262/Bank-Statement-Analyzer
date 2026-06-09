"""Streamlit Bank Statement Analyzer."""
import streamlit as st
import pandas as pd
import tempfile
import os
import traceback
from pathlib import Path

import parsers
from parsers import UnsupportedBankError, PasswordRequiredError, supported_banks
from analyzers.abb import compute_abb
from analyzers.emi import detect_emis
from analyzers.party import analyze_parties
from analyzers.returns import detect_cheque_returns
from analyzers.monthly import compute_monthly_summary
from output.excel_builder import build_report


st.set_page_config(
    page_title="Bank Statement Analyzer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Sidebar ---
with st.sidebar:
    st.title("📊 BSA Tool")
    st.markdown("**Bank Statement Analyzer**")
    st.markdown("---")
    st.markdown("### Supported Banks")
    for b in supported_banks():
        st.markdown(f"- ✅ {b}")
    st.markdown("### Coming Soon")
    for b in ["SBI", "Kotak Mahindra", "Yes Bank", "IndusInd Bank", "Bank of Baroda", "PNB"]:
        st.markdown(f"- ⏳ {b}")
    st.markdown("---")
    st.caption("⚠️ Your PDFs are processed in-memory only. Files are not stored.")
    st.caption("💡 For banks that provide monthly statements (like ICICI), upload all months together — they'll be combined automatically.")

# --- Main ---
st.title("Bank Statement Analyzer")
st.markdown("Upload one or more bank statement PDFs to get **Daily Balance Matrix**, **EMI Extract**, **Party-wise Analysis**, and complete Excel report.")

col1, col2 = st.columns([3, 2])
with col1:
    uploaded_files = st.file_uploader(
        "Upload Bank Statement PDF(s)",
        type=["pdf"],
        accept_multiple_files=True,
        help="Upload one PDF, or multiple monthly statements of the same account",
    )
with col2:
    password = st.text_input("PDF Password (if any)", type="password", help="Leave blank if not protected")
    st.caption("Same password applied to all files")

analyze_btn = st.button("🔍 Analyze Statement", type="primary", use_container_width=True,
                        disabled=not uploaded_files)

if analyze_btn and uploaded_files:
    tmp_paths = []
    parsed_stmts = []
    parse_errors = []
    try:
        for upl in uploaded_files:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(upl.read())
                tmp_paths.append((upl.name, tmp.name))

        with st.spinner(f"🔄 Parsing {len(tmp_paths)} PDF(s)..."):
            for upl_name, p in tmp_paths:
                try:
                    s = parsers.parse(p, password=password if password else None)
                    parsed_stmts.append((upl_name, s))
                except Exception as e:
                    parse_errors.append((upl_name, str(e)))

        if parse_errors:
            for n, err in parse_errors:
                st.error(f"❌ {n}: {err}")
        if not parsed_stmts:
            st.stop()

        # Show per-file summary if multiple
        if len(parsed_stmts) > 1:
            st.markdown("### 📂 Files Parsed")
            file_df = pd.DataFrame([{
                "File": n,
                "Bank": s.metadata.bank,
                "Account": s.metadata.account_no or "—",
                "Period": f"{s.metadata.period_from} → {s.metadata.period_to}" if s.metadata.period_from else "—",
                "Transactions": len(s.transactions),
            } for n, s in parsed_stmts])
            st.dataframe(file_df, use_container_width=True, hide_index=True)

            # Combine
            try:
                with st.spinner("🔗 Combining statements..."):
                    stmt = parsers.combine_statements([s for _, s in parsed_stmts])
                st.success(f"✅ Combined into **{len(stmt.transactions)} transactions** spanning {stmt.metadata.period_from} → {stmt.metadata.period_to}")
            except parsers.AccountMismatchError as e:
                st.error(f"❌ Cannot combine: {e}")
                st.stop()
        else:
            stmt = parsed_stmts[0][1]

        if not stmt.transactions:
            st.error("❌ No transactions could be parsed. The PDF(s) may be image-based or in an unsupported format.")
            st.stop()

        st.success(f"✅ Total: **{len(stmt.transactions)} transactions** from {stmt.metadata.bank}")

        # Quick stats row
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Bank", stmt.metadata.bank)
        c2.metric("Transactions", f"{len(stmt.transactions):,}")
        if stmt.metadata.period_from and stmt.metadata.period_to:
            days = (stmt.metadata.period_to - stmt.metadata.period_from).days + 1
            c3.metric("Statement Days", f"{days:,}")
        c4.metric("Opening Balance", f"₹{stmt.metadata.opening_balance:,.2f}" if stmt.metadata.opening_balance else "—")

        with st.spinner("📈 Computing ABB..."):
            abb = compute_abb(stmt)
        with st.spinner("🔍 Detecting EMIs..."):
            emis = detect_emis(stmt)
        with st.spinner("👥 Analyzing parties..."):
            credit_parties, debit_parties = analyze_parties(stmt)
        with st.spinner("📅 Computing monthly summary..."):
            monthly_stats = compute_monthly_summary(stmt)
        with st.spinner("🚨 Detecting cheque returns..."):
            returns_data = detect_cheque_returns(stmt)

        # Display key results
        st.markdown("### 📈 ABB Metrics")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Average Bank Balance", f"₹{abb.overall_abb:,.2f}")
        m2.metric("Min EOD Balance", f"₹{abb.overall_min:,.2f}")
        m3.metric("Max EOD Balance", f"₹{abb.overall_max:,.2f}")
        m4.metric("Days < ₹1,000", f"{abb.days_below_1000}")

        st.markdown("### 💳 EMI Obligations Detected")
        if emis:
            df = pd.DataFrame([{
                "Lender": e.lender,
                "Mode": e.mode,
                "Day": e.typical_day,
                "Months": e.months_seen,
                "Avg EMI (₹)": f"{e.avg_emi:,.2f}",
                "Min (₹)": f"{e.min_emi:,.2f}",
                "Max (₹)": f"{e.max_emi:,.2f}",
                "Var %": f"{e.amt_variance_pct:.1f}%",
                "Total Paid (₹)": f"{e.total_paid:,.2f}",
                "Flag": e.flag,
            } for e in emis])
            st.dataframe(df, use_container_width=True, hide_index=True)
            total_emi = sum(e.avg_emi for e in emis)
            st.info(f"💰 **Total Monthly EMI obligation: ₹{total_emi:,.2f}** (sum of avg EMIs across {len(emis)} loans)")
        else:
            st.info("No recurring EMI obligations detected (ACH-DR / ECS / NACH against known lenders).")

        st.markdown("### 👥 Top Counterparties")
        pcol1, pcol2 = st.columns(2)
        with pcol1:
            st.markdown("**Top Receivers (Credits)**")
            if credit_parties:
                cr_df = pd.DataFrame([{
                    "Party": p.party,
                    "Mode": p.mode,
                    "Txns": p.txn_count,
                    "Total ₹": f"{p.total_amount:,.0f}",
                } for p in credit_parties[:10]])
                st.dataframe(cr_df, use_container_width=True, hide_index=True)
            else:
                st.info("No credit parties detected")
        with pcol2:
            st.markdown("**Top Payers (Debits)**")
            if debit_parties:
                dr_df = pd.DataFrame([{
                    "Party": p.party,
                    "Mode": p.mode,
                    "Txns": p.txn_count,
                    "Total ₹": f"{p.total_amount:,.0f}",
                } for p in debit_parties[:10]])
                st.dataframe(dr_df, use_container_width=True, hide_index=True)
            else:
                st.info("No debit parties detected")

        # Trend chart
        st.markdown("### 📉 Daily Balance Trend")
        chart_df = pd.DataFrame({
            "Date": list(abb.daily_eod.keys()),
            "Balance": list(abb.daily_eod.values()),
        })
        st.line_chart(chart_df.set_index("Date"))

        # Excel download
        with st.spinner("📊 Building Excel report..."):
            excel_bytes = build_report(stmt, abb, emis,
                                       parties=(credit_parties, debit_parties),
                                       monthly_stats=monthly_stats,
                                       returns_data=returns_data)

        st.markdown("### 📥 Download Full Report")
        filename = f"BSA_Report_{stmt.metadata.account_no or 'account'}.xlsx"
        st.download_button(
            label="⬇️ Download Excel Report",
            data=excel_bytes,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True,
        )

    except PasswordRequiredError:
        st.error("🔒 This PDF is password-protected. Please enter the password above and try again.")
    except UnsupportedBankError as e:
        st.error(f"❌ {e}")
        st.info("This bank's format is not yet supported. Email a sample PDF (with sensitive data masked) and we'll add support.")
    except Exception as e:
        st.error(f"❌ Error: {e}")
        with st.expander("Show technical details"):
            st.code(traceback.format_exc())
    finally:
        for _name, p in tmp_paths:
            try:
                os.unlink(p)
            except Exception:
                pass

st.markdown("---")
st.caption("Built for SME / personal loan credit analysis. Files are processed in-memory and not stored.")
