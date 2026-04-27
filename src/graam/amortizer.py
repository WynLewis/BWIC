"""
Collateral cashflow engine.

Direct Python translation of GraamFlows.Core.AssetCashflowEngine.Amortizer (C#).

Projects period-by-period pool cashflows from asset-level data and
prepayment/default/severity assumptions.  The algorithm is a faithful port of the
C# struct-of-arrays inner loop — only Python idioms replace the low-level
allocation tricks (no numpy needed, though numpy arrays would be faster at scale).

Key conventions:
- All rates are in percent per annum in the public API (e.g. cpr=6.0).
- Internally they are converted to monthly decimals (smm, mdr, sev).
- Periods are 0-based month offsets from the start of the projection window.
- balance=0 before original date; hasCashflow=False after full amortization.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from .models import Asset, Assumptions, PeriodCashflows


# ---------------------------------------------------------------------------
# Assumption vector helpers
# ---------------------------------------------------------------------------

def _cpr_to_smm(cpr: float) -> float:
    """Convert annual CPR (%) to monthly SMM (decimal)."""
    return 1.0 - (1.0 - cpr / 100.0) ** (1.0 / 12.0)


def _cdr_to_mdr(cdr: float) -> float:
    """Convert annual CDR (%) to monthly MDR (decimal)."""
    return 1.0 - (1.0 - cdr / 100.0) ** (1.0 / 12.0)


def _build_vector(scalar: float, vec: Optional[list[float]], n: int, is_rate: bool = True) -> list[float]:
    """Broadcast scalar to length-n vector, or use provided vector (truncated/padded)."""
    if vec:
        if len(vec) >= n:
            return vec[:n]
        return vec + [vec[-1]] * (n - len(vec))
    return [scalar] * n


def _build_smm_vector(assumptions: Assumptions, n: int) -> list[float]:
    if assumptions.prepayment_type.value.upper() == "ABS":
        return [0.0] * n  # ABS handled separately via abs_time
    cpr_vec = _build_vector(assumptions.cpr, assumptions.cpr_vector, n)
    return [_cpr_to_smm(c) for c in cpr_vec]


def _build_mdr_vector(assumptions: Assumptions, n: int) -> list[float]:
    cdr_vec = _build_vector(assumptions.cdr, assumptions.cdr_vector, n)
    return [_cdr_to_mdr(c) for c in cdr_vec]


def _build_sev_vector(assumptions: Assumptions, n: int) -> list[float]:
    sev_vec = _build_vector(assumptions.severity, assumptions.severity_vector, n)
    return [s / 100.0 for s in sev_vec]


def _build_abs_vector(assumptions: Assumptions, n: int) -> Optional[list[float]]:
    if assumptions.prepayment_type.value.upper() != "ABS":
        return None
    return _build_vector(assumptions.cpr, assumptions.cpr_vector, n, is_rate=True)


# ---------------------------------------------------------------------------
# Per-asset amortizer
# ---------------------------------------------------------------------------

def _amortizing_payment(balance: float, monthly_rate: float, remaining_term: int) -> float:
    """Standard mortgage amortization payment."""
    if remaining_term <= 0:
        return balance
    if monthly_rate <= 0:
        return balance / remaining_term
    r = monthly_rate
    t = remaining_term
    return balance * (r * (1.0 + r) ** t) / ((1.0 + r) ** t - 1.0)


@dataclass
class _PeriodResult:
    """Mutable accumulator for one projection period."""
    begin_balance: float = 0.0
    balance: float = 0.0
    scheduled_principal: float = 0.0
    unscheduled_principal: float = 0.0
    interest: float = 0.0
    net_interest: float = 0.0
    service_fee: float = 0.0
    defaulted_principal: float = 0.0
    recovery_principal: float = 0.0
    delinq_balance: float = 0.0
    un_adv_principal: float = 0.0
    un_adv_interest: float = 0.0
    adv_principal: float = 0.0
    adv_interest: float = 0.0
    forbearance_recovery: float = 0.0
    forbearance_liquidated: float = 0.0
    accum_forbearance: float = 0.0
    wam_num: float = 0.0   # numerator for WAM (balance * remaining term)
    wala_num: float = 0.0  # numerator for WALA (balance * age)


def _project_asset(
    asset: Asset,
    start_time: int,
    end_time: int,
    smm_vec: list[float],
    mdr_vec: list[float],
    sev_vec: list[float],
    del_vec: list[float],
    del_adv_int_vec: list[float],
    del_adv_prin_vec: list[float],
    abs_vec: Optional[list[float]],
    results: list[_PeriodResult],
) -> None:
    """
    Project a single asset's cashflows and accumulate into results[].

    This is a direct translation of the inner loop in Amortizer.GenerateCashflows().
    Variable names match the C# source for traceability.
    """
    term = asset.original_amortization_term
    orig_balance = asset.original_balance
    service_fee = asset.service_fee / 1200.0

    # Use current rate if set, else fall back to original rate
    ann_rate_pct = asset.current_interest_rate if asset.current_interest_rate > 0 else asset.original_interest_rate
    rate = ann_rate_pct / 1200.0

    io_term = asset.io_term or 0
    forbearance_amt = asset.forbearance_amt or 0.0
    balance = asset.current_balance
    cashflow_balance = balance
    cashflow_prev_balance = balance
    survival_factor = 1.0
    has_cashflow = True

    # Age (months elapsed since origination)
    if asset.original_date:
        orig_time = (asset.original_date.year - 2000) * 12 + asset.original_date.month
        age = max(0, start_time - orig_time - 1)
    else:
        age = asset.wala

    # Subtract forbearance from current cashflow balance
    if forbearance_amt > 0:
        cashflow_balance -= forbearance_amt
        balance = cashflow_balance

    # Initial scheduled payment
    if io_term > 0 and age <= io_term:
        scheduled_payment = round(balance * rate * 100.0) / 100.0
    elif asset.debt_service > 0:
        scheduled_payment = asset.debt_service
    else:
        scheduled_payment = round(_amortizing_payment(orig_balance, rate, term) * 100.0) / 100.0

    for period in range(end_time - start_time + 1):
        if period >= len(results):
            break
        if balance < 1.0 or not has_cashflow:
            break

        smm = smm_vec[period]
        mdr = mdr_vec[period]
        sev = sev_vec[period]
        del_ = del_vec[period]
        del_adv_int = del_adv_int_vec[period]
        del_adv_prin = del_adv_prin_vec[period]

        if age > term:
            has_cashflow = False
        else:
            age += 1
            interest_paid = rate * cashflow_balance

            if age <= io_term:
                principal = 0.0
                cashflow_prev_balance = cashflow_balance
                if age == io_term:
                    scheduled_payment = round(_amortizing_payment(cashflow_balance, rate, term - age) * 100.0) / 100.0
            else:
                actual_payment = (cashflow_balance + interest_paid) if age >= term else scheduled_payment
                principal = min(actual_payment - interest_paid, cashflow_balance)
                cashflow_prev_balance = cashflow_balance
                cashflow_balance -= principal
                if cashflow_balance <= 0:
                    cashflow_balance = 0.0
                    has_cashflow = False

        begin_balance = balance
        sched_bal = balance

        dq_factor = (sched_bal / (cashflow_prev_balance * survival_factor)
                     if cashflow_prev_balance * survival_factor > 0 else 1.0)
        if not math.isfinite(dq_factor):
            dq_factor = 1.0

        def_prin = mdr * sched_bal
        interest = survival_factor * interest_paid * dq_factor
        sched_prin = survival_factor * principal * dq_factor
        sched_prin_mdr = sched_prin * (1.0 - mdr)

        unadv_interest = interest * del_ * (1.0 - del_adv_int) - begin_balance * service_fee * del_ * (1.0 - del_adv_int)
        unadv_principal = sched_prin_mdr * del_ * (1.0 - del_adv_prin)

        sched_prin_mdr -= unadv_principal
        interest -= unadv_interest + begin_balance * service_fee * del_ * (1.0 - del_adv_int)

        defaulted_principal = def_prin
        recovery_principal = defaulted_principal * (1.0 - sev)

        # Prepayment
        if abs_vec is not None:
            abs_rate = abs_vec[period] / 100.0  # ABS rate as % of original balance
            max_prepay = max(sched_bal - sched_prin + unadv_principal - def_prin, 0.0)
            unscheduled_principal = min(abs_rate * orig_balance, max_prepay)
        else:
            unscheduled_principal = max(sched_bal - sched_prin + unadv_principal, 0.0) * smm

        balance = sched_bal - sched_prin_mdr - def_prin - unscheduled_principal
        dq_bal = balance * del_

        # Near-maturity cleanup
        cleanup = 0.0
        if balance < 4.0 and balance > 0.0:
            cleanup = balance
            balance = 0.0

        scheduled_principal_out = sched_prin_mdr + cleanup
        effective_service_fee = (begin_balance + forbearance_amt) * service_fee
        effective_service_fee -= effective_service_fee * del_ * (1.0 - del_adv_int)
        net_interest = interest - effective_service_fee

        # Forbearance
        forbearance_recovery = 0.0
        forbearance_liquidated = 0.0
        if forbearance_amt > 0:
            begin_forb = forbearance_amt
            forb_recov = forbearance_amt * smm
            forb_writedown = forbearance_amt * mdr
            forbearance_amt -= forb_recov + forb_writedown
            forb_recov *= 1.0  # forbRecovPpay defaults to 1
            forb_recov += forb_writedown * (1.0 - sev)

            if not has_cashflow and forbearance_amt > 0:
                forbearance_amt = 0.0

            forbearance_recovery = forb_recov
            forbearance_liquidated = begin_forb - forbearance_amt

        delinq_balance = 0.0
        if not has_cashflow and balance > 0 and unadv_principal > 0:
            defaulted_principal += balance
            balance = 0.0
        else:
            delinq_balance = dq_bal

        survival_factor *= 1.0 - (mdr + smm)

        # Accumulate into results
        r = results[period]
        prev_begin = r.begin_balance
        if prev_begin + begin_balance > 0:
            r.wala_num = (prev_begin * (r.wala_num / prev_begin if prev_begin > 0 else 0) + begin_balance * age) / (prev_begin + begin_balance) * (prev_begin + begin_balance)
            r.wam_num = (prev_begin * (r.wam_num / prev_begin if prev_begin > 0 else 0) + begin_balance * (term - age)) / (prev_begin + begin_balance) * (prev_begin + begin_balance)

        r.begin_balance += begin_balance
        r.balance += balance
        r.scheduled_principal += scheduled_principal_out
        r.unscheduled_principal += unscheduled_principal
        r.interest += interest
        r.net_interest += net_interest
        r.service_fee += effective_service_fee
        r.defaulted_principal += defaulted_principal
        r.recovery_principal += recovery_principal
        r.delinq_balance += delinq_balance
        r.un_adv_principal += unadv_principal
        r.un_adv_interest += unadv_interest
        r.adv_principal += (sched_prin_mdr + unadv_principal) * del_ * del_adv_prin
        r.adv_interest += (interest + unadv_interest) * del_ * del_adv_int - effective_service_fee * del_ * del_adv_int
        r.forbearance_recovery += forbearance_recovery
        r.forbearance_liquidated += forbearance_liquidated
        r.accum_forbearance += forbearance_amt


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_cashflows(
    assets: list[Asset],
    assumptions: Assumptions,
    projection_date: date,
    max_periods: int = 720,
    group_num: str = "1",
) -> list[PeriodCashflows]:
    """
    Project collateral cashflows for a pool of assets.

    Direct translation of Amortizer.GenerateCashflows().

    Args:
        assets: List of Asset objects in the pool.
        assumptions: CPR/CDR/severity assumptions.
        projection_date: Start of projection (first cashflow date = projection_date + 1 month).
        max_periods: Maximum number of monthly periods to project (default 720 = 60 years).
        group_num: Group identifier (for multi-group waterfalls).

    Returns:
        List of PeriodCashflows, one per month, from projection_date + 1m to last cashflow.
    """
    import datetime

    n = min(max_periods, 720)

    # Build assumption vectors (all decimal, monthly)
    smm_vec = _build_smm_vector(assumptions, n)
    mdr_vec = _build_mdr_vector(assumptions, n)
    sev_vec = _build_sev_vector(assumptions, n)
    del_vec = [0.0] * n           # delinquency factor (not exposed in simple API)
    del_adv_int_vec = [0.0] * n
    del_adv_prin_vec = [0.0] * n
    abs_vec = _build_abs_vector(assumptions, n)

    results = [_PeriodResult() for _ in range(n)]

    # start_time: absolute month index (year * 12 + month)
    start_time = projection_date.year * 12 + projection_date.month

    for asset in assets:
        _project_asset(
            asset=asset,
            start_time=start_time,
            end_time=start_time + n - 1,
            smm_vec=smm_vec,
            mdr_vec=mdr_vec,
            sev_vec=sev_vec,
            del_vec=del_vec,
            del_adv_int_vec=del_adv_int_vec,
            del_adv_prin_vec=del_adv_prin_vec,
            abs_vec=abs_vec,
            results=results,
        )

    # Convert to PeriodCashflows, dropping empty trailing periods
    output = []
    for i, r in enumerate(results):
        if r.begin_balance < 1.0 and i > 0:
            break

        year = (start_time + i) // 12
        month = (start_time + i) % 12 + 1
        # Cashflow date = 25th of the following month
        cf_date = datetime.date(year, month, 25)

        pc = PeriodCashflows(
            cashflow_date=cf_date,
            group_num=group_num,
            begin_balance=r.begin_balance,
            balance=r.balance,
            scheduled_principal=r.scheduled_principal,
            unscheduled_principal=r.unscheduled_principal,
            interest=r.interest,
            net_interest=r.net_interest,
            service_fee=r.service_fee,
            defaulted_principal=r.defaulted_principal,
            recovery_principal=r.recovery_principal,
            delinq_balance=r.delinq_balance,
            accum_forbearance=r.accum_forbearance,
            forbearance_liquidated=r.forbearance_liquidated,
            forbearance_recovery=r.forbearance_recovery,
        )
        output.append(pc)

    return output
