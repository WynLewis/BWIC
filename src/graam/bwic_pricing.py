"""
BWIC-oriented pricing: DM-to-Maturity, DM-to-Call, DM-to-Worst.

For each BWIC line you provide:
  - A CashflowStream (from intex_loader)
  - A bid price (% of par)
  - A settle date
  - A non-call date (for to-worst / to-call)
  - The Intex scenario assumptions used (CPR, CDR, Severity) — informational,
    stored on the result so bidders know what vector was used

The engine computes:
  - DM-to-Maturity: DM using the full cashflow stream as given by Intex.
  - DM-to-Call:     DM truncating the stream at the NC date and adding a
                    balloon principal equal to the remaining balance on that date.
  - DM-to-Worst:    min(DM_to_maturity, DM_to_call) — lower DM = worse for holder.

WAL equivalents are computed using the same truncation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .analytics import BondAnalytics
from .models import CashflowEntry, CashflowStream


# ---------------------------------------------------------------------------
# Scenario assumptions (informational — the Intex file encodes the actual CFs)
# ---------------------------------------------------------------------------

@dataclass
class IntexScenario:
    """
    Records the CPR / CDR / Severity assumptions that were used when
    generating the Intex cashflows.  Stored on BwicResult for transparency.
    These do NOT re-run the cashflows — they are purely metadata.
    """
    cpr: float = 0.0          # % p.a. constant prepayment rate
    cdr: float = 0.0          # % p.a. constant default rate
    severity: float = 0.0     # % loss given default (0–100)
    label: str = ""           # e.g. "Base", "Stress", "15 CPR / 2 CDR / 40 SEV"

    def __str__(self) -> str:
        if self.label:
            return self.label
        parts = []
        if self.cpr:
            parts.append(f"{self.cpr:.1f} CPR")
        if self.cdr:
            parts.append(f"{self.cdr:.1f} CDR")
        if self.severity:
            parts.append(f"{self.severity:.0f} SEV")
        return " / ".join(parts) if parts else "N/A"


# ---------------------------------------------------------------------------
# Standard BWIC scenarios — the two scenarios run for every line
# ---------------------------------------------------------------------------

MARKET = IntexScenario(cpr=20.0, cdr=2.0, severity=30.0, label="Market")
"""Market scenario: 20 CPR, 2 CDR, 30% severity. Faster prepays, lighter losses."""

BASE = IntexScenario(cpr=15.0, cdr=5.0, severity=50.0, label="Base")
"""Base scenario: 15 CPR, 5 CDR, 50% severity. Slower prepays, heavier losses."""

STANDARD_SCENARIOS: dict[str, IntexScenario] = {
    "Market": MARKET,
    "Base": BASE,
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class BwicResult:
    """Pricing output for a single BWIC line at a given bid price."""

    # ── Identification ────────────────────────────────────────────────────
    cusip: str = ""
    tranche_name: str = ""
    bid_price: float = 0.0
    settle_date: Optional[date] = None
    nc_date: Optional[date] = None           # non-call / first-call date
    scenario: Optional[IntexScenario] = None

    # ── DM results (bps) ─────────────────────────────────────────────────
    dm_to_maturity: Optional[float] = None
    dm_to_call: Optional[float] = None       # None when nc_date not provided
    dm_to_worst: Optional[float] = None      # min(dm_to_maturity, dm_to_call)

    # ── WAL (years) ───────────────────────────────────────────────────────
    wal_to_maturity: Optional[float] = None
    wal_to_call: Optional[float] = None

    # ── Accrued / dirty price ─────────────────────────────────────────────
    accrued_interest: Optional[float] = None
    dirty_price: Optional[float] = None

    # ── Diagnostic ────────────────────────────────────────────────────────
    call_date_used: Optional[date] = None    # actual date the stream was cut
    balloon_principal: Optional[float] = None  # balance added as balloon at call
    worst_case: Optional[str] = None         # "maturity" | "call"
    error: Optional[str] = None             # set if pricing failed

    def __str__(self) -> str:
        name = self.cusip or self.tranche_name or "???"
        if self.error:
            return f"{name} @ {self.bid_price:.3f} — ERROR: {self.error}"
        return (
            f"{name} @ {self.bid_price:.3f} | "
            f"DM-Mat: {self.dm_to_maturity:.1f} bps | "
            f"DM-Call: {self.dm_to_call:.1f} bps | "
            f"DM-Worst: {self.dm_to_worst:.1f} bps | "
            f"WAL: {self.wal_to_maturity:.2f}y"
        )


# ---------------------------------------------------------------------------
# Stream truncation at NC date
# ---------------------------------------------------------------------------

def _truncate_to_call(stream: CashflowStream, nc_date: date) -> CashflowStream:
    """
    Return a new CashflowStream truncated at nc_date.

    The last period on or before nc_date has a balloon principal added equal to
    the remaining balance at that point.  This models a clean par call — the
    deal is redeemed at par on the NC date and no further cashflows are received.

    If nc_date falls between payment dates, we use the last payment date
    on or before nc_date as the call date.
    """
    # Cashflows up to and including nc_date
    pre_call = [cf for cf in stream.cashflows if cf.cashflow_date <= nc_date]

    if not pre_call:
        # NC date is before all cashflows — caller will handle edge case
        return stream

    # Remaining balance at last payment date before/on nc_date
    last_cf = pre_call[-1]
    remaining_balance = last_cf.balance

    if remaining_balance <= 0.01:
        # Bond already fully paid before NC date — use as-is
        return CashflowStream(
            cashflows=pre_call,
            settle_date=stream.settle_date,
            balance=stream.balance,
            day_counter_name=stream.day_counter_name,
            compounding=stream.compounding,
            frequency=stream.frequency,
            is_io=stream.is_io,
            start_accrual_period=stream.start_accrual_period,
            pay_delay=stream.pay_delay,
        )

    # Add balloon: replace last cashflow with one that includes full balance paydown
    balloon_entry = CashflowEntry(
        cashflow_date=last_cf.cashflow_date,
        interest=last_cf.interest,
        principal=last_cf.principal + remaining_balance,   # scheduled prin + balloon
        balance=0.0,
        prev_balance=last_cf.prev_balance,
        index_value=last_cf.index_value,
    )
    truncated = pre_call[:-1] + [balloon_entry]

    return CashflowStream(
        cashflows=truncated,
        settle_date=stream.settle_date,
        balance=stream.balance,
        day_counter_name=stream.day_counter_name,
        compounding=stream.compounding,
        frequency=stream.frequency,
        is_io=stream.is_io,
        start_accrual_period=stream.start_accrual_period,
        pay_delay=stream.pay_delay,
    ), remaining_balance


# ---------------------------------------------------------------------------
# Core pricing function
# ---------------------------------------------------------------------------

def price_bwic_line(
    stream: CashflowStream,
    bid_price: float,
    nc_date: Optional[date] = None,
    cusip: str = "",
    tranche_name: str = "",
    scenario: Optional[IntexScenario] = None,
) -> BwicResult:
    """
    Compute DM-to-Maturity, DM-to-Call, and DM-to-Worst for one BWIC line.

    Args:
        stream:         CashflowStream loaded from Intex (full to maturity).
        bid_price:      Clean bid price as % of par (e.g. 99.75).
        nc_date:        Non-call / first-call date.  When provided, DM-to-Call
                        and DM-to-Worst are computed in addition to DM-to-Mat.
        cusip:          CUSIP for identification on the result.
        tranche_name:   Tranche name for display.
        scenario:       IntexScenario metadata (CPR/CDR/SEV used in Intex run).

    Returns:
        BwicResult with all analytics populated.
    """
    result = BwicResult(
        cusip=cusip,
        tranche_name=tranche_name,
        bid_price=bid_price,
        settle_date=stream.settle_date,
        nc_date=nc_date,
        scenario=scenario,
    )

    # ── DM-to-Maturity ────────────────────────────────────────────────────
    try:
        ba_mat = BondAnalytics(stream)
        result.dm_to_maturity = ba_mat.dm_from_price(bid_price)
        result.wal_to_maturity = ba_mat.wal()
        result.accrued_interest = ba_mat.accrued_interest()
        result.dirty_price = bid_price + (result.accrued_interest or 0.0)
    except Exception as exc:
        result.error = f"DM-to-Mat failed: {exc}"
        return result

    # ── DM-to-Call (only when nc_date is provided) ────────────────────────
    if nc_date is not None:
        try:
            call_result = _truncate_to_call(stream, nc_date)

            # _truncate_to_call returns (stream, balloon) tuple
            if isinstance(call_result, tuple):
                call_stream, balloon = call_result
            else:
                call_stream = call_result
                balloon = 0.0

            # Only price if the call stream has future cashflows after settle
            call_future = [
                cf for cf in call_stream.cashflows
                if cf.cashflow_date >= stream.settle_date and cf.cashflow > 0
            ]

            if call_future:
                ba_call = BondAnalytics(call_stream)
                result.dm_to_call = ba_call.dm_from_price(bid_price)
                result.wal_to_call = ba_call.wal()
                result.call_date_used = call_stream.cashflows[-1].cashflow_date
                result.balloon_principal = balloon

                # ── DM-to-Worst ───────────────────────────────────────────
                dm_mat = result.dm_to_maturity
                dm_call = result.dm_to_call

                # Both valid: worst = lower DM (lower spread = worse for holder)
                if dm_mat is not None and dm_call is not None and dm_mat > -9000 and dm_call > -9000:
                    if dm_call <= dm_mat:
                        result.dm_to_worst = dm_call
                        result.worst_case = "call"
                    else:
                        result.dm_to_worst = dm_mat
                        result.worst_case = "maturity"
                elif dm_call is not None and dm_call > -9000:
                    result.dm_to_worst = dm_call
                    result.worst_case = "call"
                else:
                    result.dm_to_worst = dm_mat
                    result.worst_case = "maturity"
            else:
                # NC date already past — only maturity applies
                result.dm_to_call = result.dm_to_maturity
                result.wal_to_call = result.wal_to_maturity
                result.dm_to_worst = result.dm_to_maturity
                result.worst_case = "maturity"

        except Exception as exc:
            # Don't fail the whole result — just leave call/worst as None
            result.error = f"DM-to-Call failed: {exc}"

    return result


# ---------------------------------------------------------------------------
# Batch pricer — run a whole BWIC list at once
# ---------------------------------------------------------------------------

@dataclass
class BwicLine:
    """Input definition for one BWIC line."""
    stream: CashflowStream
    bid_price: float
    nc_date: Optional[date] = None
    cusip: str = ""
    tranche_name: str = ""
    scenario: Optional[IntexScenario] = None


def price_bwic(lines: list[BwicLine]) -> list[BwicResult]:
    """
    Price a full BWIC list.  Returns one BwicResult per line in input order.
    """
    return [
        price_bwic_line(
            stream=line.stream,
            bid_price=line.bid_price,
            nc_date=line.nc_date,
            cusip=line.cusip,
            tranche_name=line.tranche_name,
            scenario=line.scenario,
        )
        for line in lines
    ]


# ---------------------------------------------------------------------------
# Sensitivity: reprice at a range of bid prices (for price/DM table)
# ---------------------------------------------------------------------------

def dm_price_table(
    stream: CashflowStream,
    prices: list[float],
    nc_date: Optional[date] = None,
    scenario: Optional[IntexScenario] = None,
) -> list[BwicResult]:
    """
    Return a list of BwicResults for each price in prices.

    Useful for generating a bid/DM reference table (e.g. 98 / 98.5 / 99 / par).
    """
    return [
        price_bwic_line(stream, p, nc_date=nc_date, scenario=scenario)
        for p in prices
    ]


# ---------------------------------------------------------------------------
# Multi-scenario pricing — run each tranche under Market AND Base
# ---------------------------------------------------------------------------

@dataclass
class BwicLineMulti:
    """
    BWIC line with one cashflow stream per scenario.

    Typical use::

        line = BwicLineMulti(
            cusip="12345ABC0",
            tranche_name="AA",
            bid_price=99.50,
            nc_date=date(2026, 1, 25),
            streams={
                "Market": market_stream,  # from Intex with 20/2/30
                "Base":   base_stream,    # from Intex with 15/5/50
            },
        )
    """
    cusip: str = ""
    tranche_name: str = ""
    bid_price: float = 0.0
    nc_date: Optional[date] = None
    streams: dict[str, CashflowStream] = field(default_factory=dict)
    scenarios: dict[str, IntexScenario] = field(default_factory=lambda: dict(STANDARD_SCENARIOS))


@dataclass
class BwicMultiResult:
    """
    Pricing output for one BWIC line across multiple scenarios.

    Attributes:
        cusip / tranche_name / bid_price / nc_date: identification.
        results: one BwicResult per scenario label (e.g. {"Market": ..., "Base": ...}).
    """
    cusip: str = ""
    tranche_name: str = ""
    bid_price: float = 0.0
    nc_date: Optional[date] = None
    results: dict[str, BwicResult] = field(default_factory=dict)

    # ── Convenience accessors ─────────────────────────────────────────────

    def dm_to_worst(self, scenario: str) -> Optional[float]:
        return self.results.get(scenario, BwicResult()).dm_to_worst

    def dm_to_maturity(self, scenario: str) -> Optional[float]:
        return self.results.get(scenario, BwicResult()).dm_to_maturity

    def dm_to_call(self, scenario: str) -> Optional[float]:
        return self.results.get(scenario, BwicResult()).dm_to_call

    def wal_to_worst(self, scenario: str) -> Optional[float]:
        r = self.results.get(scenario)
        if r is None:
            return None
        return r.wal_to_call if r.worst_case == "call" else r.wal_to_maturity

    def summary_row(self) -> dict:
        """
        Flatten to a single dict suitable for a pandas DataFrame row.

        Columns: cusip, tranche, bid, nc_date, then for each scenario
        DM-Mat / DM-Call / DM-Worst / WAL-Worst.
        """
        row: dict = {
            "cusip": self.cusip,
            "tranche": self.tranche_name,
            "bid": self.bid_price,
            "nc_date": self.nc_date,
        }
        for label, r in self.results.items():
            row[f"{label}_DM_Mat"] = r.dm_to_maturity
            row[f"{label}_DM_Call"] = r.dm_to_call
            row[f"{label}_DM_Worst"] = r.dm_to_worst
            row[f"{label}_WAL_Mat"] = r.wal_to_maturity
            row[f"{label}_WAL_Call"] = r.wal_to_call
            row[f"{label}_Worst_Case"] = r.worst_case
        return row


def price_bwic_line_multi(
    line: BwicLineMulti,
) -> BwicMultiResult:
    """
    Price one BWIC line across all its scenarios.

    Each (scenario_label, stream) pair is priced with the line's bid_price and nc_date.
    The IntexScenario metadata for each label is taken from line.scenarios (defaults to
    STANDARD_SCENARIOS = Market + Base).
    """
    out = BwicMultiResult(
        cusip=line.cusip,
        tranche_name=line.tranche_name,
        bid_price=line.bid_price,
        nc_date=line.nc_date,
    )

    for label, stream in line.streams.items():
        scenario = line.scenarios.get(label) or IntexScenario(label=label)
        out.results[label] = price_bwic_line(
            stream=stream,
            bid_price=line.bid_price,
            nc_date=line.nc_date,
            cusip=line.cusip,
            tranche_name=line.tranche_name,
            scenario=scenario,
        )
    return out


def price_bwic_multi(lines: list[BwicLineMulti]) -> list[BwicMultiResult]:
    """Run multi-scenario pricing across an entire BWIC list."""
    return [price_bwic_line_multi(line) for line in lines]


def to_dataframe(results: list[BwicMultiResult]):
    """
    Flatten a list of BwicMultiResults into a pandas DataFrame for the BWIC blotter.

    One row per line, with columns for each scenario × metric combination.
    """
    import pandas as pd
    return pd.DataFrame([r.summary_row() for r in results])
