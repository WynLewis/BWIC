"""
Intex cashflow Excel loader.

Assumes the following Intex Wave / CDI export layout (row 1 = headers, row 2+ = data):

    Period | Date | Beg Balance | Principal | Interest | End Balance | Coupon | Index Rate

Exact column names vary between Intex versions; this loader accepts all common aliases
via case-insensitive fuzzy matching.  Unknown columns are ignored gracefully.

Assumed Intex Excel structure
─────────────────────────────
Row 1: Deal / tranche metadata (skipped — set skip_rows=N to advance past it)
Row 2: Column headers
Row 3+: Monthly cashflow data, one row per period

Expected columns (any alias in the list will match):
  date            → "Date", "Pay Date", "Payment Date", "Period Date"
  beg_balance     → "Beg Balance", "Begin Balance", "Beginning Balance", "Opening Balance"
  interest        → "Interest", "Interest Payment", "Coupon Payment"
  principal       → "Principal", "Total Principal", "Tot Principal", "Principal Payment"
                    (if absent, loader sums sched_principal + prepaid_principal)
  sched_principal → "Sched Principal", "Scheduled Principal", "Sched Prin"  (optional)
  prepaid_prin    → "Prepaid Principal", "Unscheduled Principal", "Prepay", "Prepaid Prin" (optional)
  end_balance     → "End Balance", "Ending Balance", "Closing Balance", "Ending Bal"
  index_rate      → "Index", "Index Rate", "SOFR", "1m SOFR", "Term SOFR",
                    "SOFR Forward", "LIBOR", "1mL", "Base Rate"  (% p.a.)
                    *** REQUIRED for DM calculation — this is the per-period
                    SOFR/LIBOR forward rate that Intex used to project the
                    cashflows.  Each row carries that period's forward, and the
                    DM solver discounts at (forward + spread) per period. ***
  coupon_rate     → "Coupon", "Coupon Rate", "All-In Rate", "Rate"   (% p.a.)
                    Used as a fallback if no index_rate column is present
                    (loader will treat the all-in coupon as the forward proxy).

All balance / cashflow columns are in face-value dollar units.
All rate columns are in percent per annum (e.g. 5.30 = 5.30% SOFR).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from .enums import Compounding, Frequency
from .models import CashflowEntry, CashflowStream


# ---------------------------------------------------------------------------
# Column alias registry
# ---------------------------------------------------------------------------

_ALIASES: dict[str, list[str]] = {
    "date": [
        "date", "pay date", "payment date", "period date", "cashflow date", "cf date",
    ],
    "beg_balance": [
        "beg balance", "begin balance", "beginning balance", "opening balance",
        "beg bal", "begin bal", "start balance", "start bal",
    ],
    "interest": [
        "interest", "interest payment", "coupon payment", "int payment", "int",
        "net interest",  # some exports only show net
    ],
    "principal": [
        "principal", "total principal", "tot principal", "principal payment",
        "prin", "total prin", "tot prin",
    ],
    "sched_principal": [
        "sched principal", "scheduled principal", "sched prin", "scheduled prin",
        "amortization", "amort",
    ],
    "prepaid_principal": [
        "prepaid principal", "unscheduled principal", "prepay principal",
        "prepaid prin", "prepay", "unscheduled prin",
    ],
    "end_balance": [
        "end balance", "ending balance", "closing balance", "ending bal",
        "end bal", "close balance",
    ],
    "index_rate": [
        "index", "index rate", "sofr", "libor", "base rate", "floating index",
        "reference rate", "ref rate", "index value",
        # Intex SOFR/LIBOR forward variants — the per-period forward rate is what
        # the DM calc needs.  These are typical Intex Wave / CDI column headers:
        "sofr forward", "sofr fwd", "fwd sofr", "forward sofr",
        "libor forward", "libor fwd", "fwd libor", "forward libor",
        "1m sofr", "1ml", "1m libor", "1mlibor", "1msofr", "1m sofr forward",
        "3m sofr", "3ml", "3m libor", "3mlibor", "3msofr", "3m sofr forward",
        "term sofr", "term sofr 1m", "term sofr 3m",
        "index forward", "index fwd",
    ],
    "coupon_rate": [
        "coupon", "coupon rate", "all-in rate", "all in rate", "rate", "gross coupon",
        "note rate",
    ],
}


def _normalise(s: str) -> str:
    return s.strip().lower().replace("-", " ").replace("_", " ")


def _find_column(df: pd.DataFrame, field: str) -> Optional[str]:
    """Return the actual DataFrame column name matching field aliases, or None."""
    aliases = [_normalise(a) for a in _ALIASES.get(field, [])]
    for col in df.columns:
        if _normalise(str(col)) in aliases:
            return col
    return None


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------

def load_intex_excel(
    path: str | Path,
    sheet_name: int | str = 0,
    header_row: int = 0,          # 0-based row index of the header row in Excel
    settle_date: Optional[date] = None,
    balance_override: Optional[float] = None,
    day_count: str = "Actual360",
    compounding: Compounding = Compounding.SIMPLE,
    frequency: Frequency = Frequency.MONTHLY,
    start_accrual_period: Optional[date] = None,
    pay_delay: int = 0,
) -> CashflowStream:
    """
    Load an Intex cashflow Excel export and return a CashflowStream ready for pricing.

    Args:
        path: Path to the .xlsx / .xls file.
        sheet_name: Sheet index or name (default 0 = first sheet).
        header_row: 0-based index of the row containing column headers.
                    Set to 1 if Intex wrote deal metadata in row 0.
        settle_date: Settlement date for pricing.  If None, defaults to the
                     first cashflow date in the file.
        balance_override: Override the notional used for NPV scaling.  If None,
                          the first row's beginning balance is used.
        day_count: Day count convention string (default "Actual360" — CLO standard).
        compounding: Interest compounding (default Simple — CLO DM convention).
        frequency: Payment frequency (default Monthly).
        start_accrual_period: Start of current coupon period (for accrued interest calc).
        pay_delay: Payment delay in days (default 0).

    Returns:
        CashflowStream ready to pass to BondAnalytics.

    Raises:
        ValueError: If required columns (date, interest, principal/end_balance) are missing.
    """
    df = pd.read_excel(path, sheet_name=sheet_name, header=header_row)

    # Drop completely empty rows
    df = df.dropna(how="all").reset_index(drop=True)

    # Locate columns
    col_date = _find_column(df, "date")
    col_beg = _find_column(df, "beg_balance")
    col_interest = _find_column(df, "interest")
    col_principal = _find_column(df, "principal")
    col_sched = _find_column(df, "sched_principal")
    col_prepaid = _find_column(df, "prepaid_principal")
    col_end = _find_column(df, "end_balance")
    col_index = _find_column(df, "index_rate")
    col_coupon = _find_column(df, "coupon_rate")

    # Validate required columns
    missing = []
    if col_date is None:
        missing.append("date")
    if col_interest is None:
        missing.append("interest")
    if col_principal is None and col_end is None and col_sched is None:
        missing.append("principal (or end_balance)")
    if missing:
        raise ValueError(
            f"Required Intex columns not found: {missing}\n"
            f"Available columns: {list(df.columns)}"
        )

    entries: list[CashflowEntry] = []

    for _, row in df.iterrows():
        # --- Date ---
        raw_date = row[col_date]
        if pd.isna(raw_date):
            continue
        if isinstance(raw_date, (date, pd.Timestamp)):
            cf_date = pd.Timestamp(raw_date).date()
        else:
            try:
                cf_date = pd.to_datetime(str(raw_date)).date()
            except Exception:
                continue  # skip non-date rows (subtotal rows, etc.)

        # --- Balances ---
        beg_bal = float(row[col_beg]) if col_beg and not pd.isna(row[col_beg]) else 0.0

        if col_end and not pd.isna(row[col_end]):
            end_bal = float(row[col_end])
        else:
            end_bal = None

        # --- Principal ---
        if col_principal and not pd.isna(row[col_principal]):
            principal = float(row[col_principal])
        elif col_sched is not None or col_prepaid is not None:
            sched = float(row[col_sched]) if col_sched and not pd.isna(row[col_sched]) else 0.0
            prepaid = float(row[col_prepaid]) if col_prepaid and not pd.isna(row[col_prepaid]) else 0.0
            principal = sched + prepaid
        elif end_bal is not None:
            principal = max(beg_bal - end_bal, 0.0)
        else:
            principal = 0.0

        # Derive end_balance from principal if missing
        if end_bal is None:
            end_bal = max(beg_bal - principal, 0.0)

        # --- Interest ---
        interest = float(row[col_interest]) if not pd.isna(row[col_interest]) else 0.0

        # --- Index rate (SOFR/LIBOR for DM calc) ---
        index_value = 0.0
        if col_index and not pd.isna(row[col_index]):
            index_value = float(row[col_index])
        elif col_coupon and not pd.isna(row[col_coupon]):
            # Fallback: use all-in coupon as proxy (DM will absorb the spread)
            index_value = float(row[col_coupon])

        entries.append(
            CashflowEntry(
                cashflow_date=cf_date,
                interest=interest,
                principal=principal,
                balance=end_bal,
                prev_balance=beg_bal,
                index_value=index_value,
            )
        )

    if not entries:
        raise ValueError("No valid cashflow rows found in the Intex file.")

    # Sort by date — Intex exports are usually already sorted, but just in case
    entries.sort(key=lambda e: e.cashflow_date)

    # Balance for NPV scaling — use override, or first row's beginning balance
    if balance_override is not None:
        notional = balance_override
    elif entries[0].prev_balance > 0:
        notional = entries[0].prev_balance
    else:
        notional = entries[0].balance + entries[0].principal

    # Settle date defaults to first cashflow date
    if settle_date is None:
        settle_date = entries[0].cashflow_date

    return CashflowStream(
        cashflows=entries,
        settle_date=settle_date,
        balance=notional,
        day_counter_name=day_count,
        compounding=compounding,
        frequency=frequency,
        start_accrual_period=start_accrual_period,
        pay_delay=pay_delay,
    )


def load_intex_dataframe(
    df: pd.DataFrame,
    settle_date: Optional[date] = None,
    balance_override: Optional[float] = None,
    day_count: str = "Actual360",
    compounding: Compounding = Compounding.SIMPLE,
    frequency: Frequency = Frequency.MONTHLY,
    start_accrual_period: Optional[date] = None,
    pay_delay: int = 0,
) -> CashflowStream:
    """
    Same as load_intex_excel but accepts an already-loaded pandas DataFrame.

    Useful when reading from a marimo file-upload widget or a multi-sheet workbook
    where you've already isolated the cashflow tab.
    """
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        df.to_excel(tmp_path, index=False)
        return load_intex_excel(
            tmp_path,
            settle_date=settle_date,
            balance_override=balance_override,
            day_count=day_count,
            compounding=compounding,
            frequency=frequency,
            start_accrual_period=start_accrual_period,
            pay_delay=pay_delay,
        )
    finally:
        os.unlink(tmp_path)


def stream_from_records(
    records: list[dict],
    settle_date: date,
    balance_override: Optional[float] = None,
    day_count: str = "Actual360",
    compounding: Compounding = Compounding.SIMPLE,
    frequency: Frequency = Frequency.MONTHLY,
) -> CashflowStream:
    """
    Build a CashflowStream directly from a list of dicts (e.g. pasted data).

    Each dict should have keys (flexible, case-insensitive):
        date, beg_balance, interest, principal, end_balance, index_rate

    This is the easiest path for manual data entry in the marimo app.
    """
    entries = []
    for rec in records:
        # normalise keys
        r = {_normalise(k): v for k, v in rec.items()}

        def _get(*names):
            for n in names:
                if n in r and r[n] is not None:
                    return float(r[n])
            return 0.0

        def _get_date(*names):
            for n in names:
                if n in r and r[n] is not None:
                    v = r[n]
                    if isinstance(v, (date, pd.Timestamp)):
                        return pd.Timestamp(v).date()
                    return pd.to_datetime(str(v)).date()
            return None

        cf_date = _get_date("date", "pay date", "payment date", "cashflow date")
        if cf_date is None:
            continue

        beg_bal = _get("beg balance", "begin balance", "beginning balance", "beg bal")
        interest = _get("interest", "interest payment", "coupon payment")
        principal = _get("principal", "total principal", "prin")
        end_bal = _get("end balance", "ending balance", "end bal")
        index_value = _get("index", "index rate", "sofr", "libor", "base rate", "coupon rate")

        if end_bal == 0 and beg_bal > 0:
            end_bal = max(beg_bal - principal, 0.0)

        entries.append(
            CashflowEntry(
                cashflow_date=cf_date,
                interest=interest,
                principal=principal,
                balance=end_bal,
                prev_balance=beg_bal,
                index_value=index_value,
            )
        )

    entries.sort(key=lambda e: e.cashflow_date)

    notional = balance_override or (entries[0].prev_balance if entries else 0.0) or (entries[0].balance + entries[0].principal if entries else 1.0)

    return CashflowStream(
        cashflows=entries,
        settle_date=settle_date,
        balance=notional,
        day_counter_name=day_count,
        compounding=compounding,
        frequency=frequency,
    )
