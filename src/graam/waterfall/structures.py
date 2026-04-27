"""
Composable waterfall payment structures.

Python translation of the GraamFlows.Core.Waterfall.Structures.PayableStructures
hierarchy (SequentialStructure, ProrataStructure, ShiftingInterestStructure,
FixedStructure) plus a simplified ComposableStructure waterfall runner.

The C# engine is deeply integrated with Roslyn-compiled DSL rules and trigger
machinery that is language-specific.  This translation captures the *structural*
mechanics (how money flows between tranches) without the dynamic rule compiler.
For BWIC / pricing purposes, the simplified runner handles the common CLO
waterfall: sequential interest + sequential principal + reverse-sequential writedown.

All amounts are in currency units (not %).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Runtime tranche state (mirrors DynamicClass / DynamicTranche)
# ---------------------------------------------------------------------------

@dataclass
class TrancheLedger:
    """
    Mutable per-period ledger for one tranche, tracking balances and cashflows.
    """

    name: str
    original_balance: float
    factor: float = 1.0
    coupon_rate: float = 0.0        # decimal p.a. (e.g. 0.055 for 5.5%)
    index_rate: float = 0.0         # current period floating index (decimal)
    spread: float = 0.0             # decimal spread for floaters
    is_floater: bool = False
    pay_frequency: int = 12         # payments per year
    locked_out: bool = False
    cum_writedown: float = 0.0

    # Per-period cashflow accumulators (reset each period externally)
    period_interest: float = 0.0
    period_principal: float = 0.0
    period_writedown: float = 0.0

    @property
    def current_balance(self) -> float:
        return self.original_balance * self.factor

    @property
    def effective_rate(self) -> float:
        if self.is_floater:
            return self.index_rate + self.spread
        return self.coupon_rate

    def interest_due(self, days_in_period: float = 1.0 / 12.0) -> float:
        """Coupon interest due for one period."""
        return self.current_balance * self.effective_rate * days_in_period

    def pay_principal(self, amount: float) -> float:
        """Reduce balance by amount; return amount actually paid."""
        actual = min(amount, self.current_balance)
        if self.original_balance > 0:
            self.factor -= actual / self.original_balance
            if self.factor < 0:
                self.factor = 0.0
        self.period_principal += actual
        return actual

    def pay_interest(self, amount: float) -> float:
        """Record interest payment; return amount paid."""
        self.period_interest += amount
        return amount

    def apply_writedown(self, amount: float) -> float:
        """Apply writedown loss to balance; return amount absorbed."""
        actual = min(amount, self.current_balance)
        if self.original_balance > 0:
            self.factor -= actual / self.original_balance
            if self.factor < 0:
                self.factor = 0.0
        self.period_writedown += actual
        self.cum_writedown += actual
        return actual

    def reset_period(self) -> None:
        self.period_interest = 0.0
        self.period_principal = 0.0
        self.period_writedown = 0.0


# ---------------------------------------------------------------------------
# Abstract Payable interface
# ---------------------------------------------------------------------------

class Payable(ABC):
    """Abstract node in the payment distribution tree."""

    @property
    @abstractmethod
    def is_leaf(self) -> bool: ...

    @abstractmethod
    def current_balance(self) -> float: ...

    @abstractmethod
    def pay_principal(self, amount: float) -> float:
        """Distribute principal; return amount absorbed."""
        ...

    @abstractmethod
    def pay_interest(self, amount: float, rate_provider: Optional[Callable] = None) -> float:
        """Distribute interest; return amount absorbed."""
        ...

    @abstractmethod
    def pay_writedown(self, amount: float) -> float:
        """Distribute writedown loss; return amount absorbed."""
        ...

    @abstractmethod
    def interest_due(self, days: float = 1.0 / 12.0) -> float: ...

    def is_locked_out(self) -> bool:
        return False

    def begin_balance(self) -> float:
        return self.current_balance()

    def children(self) -> list["Payable"]:
        return []


# ---------------------------------------------------------------------------
# Leaf: single tranche
# ---------------------------------------------------------------------------

class SingleTranche(Payable):
    """Leaf node wrapping a TrancheLedger."""

    def __init__(self, ledger: TrancheLedger) -> None:
        self.ledger = ledger

    @property
    def is_leaf(self) -> bool:
        return True

    def current_balance(self) -> float:
        return self.ledger.current_balance

    def interest_due(self, days: float = 1.0 / 12.0) -> float:
        return self.ledger.interest_due(days)

    def pay_principal(self, amount: float) -> float:
        return self.ledger.pay_principal(amount)

    def pay_interest(self, amount: float, rate_provider=None) -> float:
        return self.ledger.pay_interest(amount)

    def pay_writedown(self, amount: float) -> float:
        return self.ledger.apply_writedown(amount)

    def is_locked_out(self) -> bool:
        return self.ledger.locked_out


# ---------------------------------------------------------------------------
# Sequential structure
# ---------------------------------------------------------------------------

class SequentialStructure(Payable):
    """
    Pay the first payable fully before moving to the next.

    Mirrors GraamFlows.Core.Waterfall.Structures.PayableStructures.SequentialStructure.
    """

    def __init__(self, payables: list[Payable]) -> None:
        self._payables = payables

    @property
    def is_leaf(self) -> bool:
        return False

    def children(self) -> list[Payable]:
        return list(self._payables)

    def current_balance(self) -> float:
        return sum(p.current_balance() for p in self._payables)

    def interest_due(self, days: float = 1.0 / 12.0) -> float:
        return sum(p.interest_due(days) for p in self._payables)

    def pay_interest(self, amount: float, rate_provider=None) -> float:
        paid = 0.0
        remaining = amount
        for p in self._payables:
            if remaining < 0.01:
                break
            if p.is_locked_out():
                continue
            due = p.interest_due()
            pay_amt = min(remaining, due)
            absorbed = p.pay_interest(pay_amt, rate_provider)
            paid += absorbed
            remaining -= absorbed
        return paid

    def pay_principal(self, amount: float) -> float:
        return self._distribute(amount, lambda p, a: p.pay_principal(a))

    def pay_writedown(self, amount: float) -> float:
        return self._distribute(amount, lambda p, a: p.pay_writedown(a))

    def _distribute(self, amount: float, pay_fn: Callable[[Payable, float], float]) -> float:
        paid = 0.0
        remaining = amount

        for p in self._payables:
            if remaining < 0.001:
                break
            if p.is_locked_out():
                continue
            cap = min(remaining, p.current_balance())
            absorbed = pay_fn(p, cap)
            paid += absorbed
            remaining -= absorbed

        # Second pass if any remains (lockout overrides)
        if remaining > 2.0:
            for p in self._payables:
                if remaining < 0.001:
                    break
                cap = min(remaining, p.current_balance())
                absorbed = pay_fn(p, cap)
                paid += absorbed
                remaining -= absorbed

        return paid


# ---------------------------------------------------------------------------
# Pro-rata structure
# ---------------------------------------------------------------------------

class ProrataStructure(Payable):
    """
    Pay payables proportionally to their begin balance.

    Mirrors GraamFlows.Core.Waterfall.Structures.PayableStructures.ProrataStructure.
    """

    def __init__(self, payables: list[Payable]) -> None:
        self._payables = payables

    @property
    def is_leaf(self) -> bool:
        return False

    def children(self) -> list[Payable]:
        return list(self._payables)

    def current_balance(self) -> float:
        return sum(p.current_balance() for p in self._payables)

    def interest_due(self, days: float = 1.0 / 12.0) -> float:
        return sum(p.interest_due(days) for p in self._payables)

    def pay_interest(self, amount: float, rate_provider=None) -> float:
        total_due = self.interest_due()
        if total_due < 0.01:
            return 0.0
        paid = 0.0
        for p in self._payables:
            if p.is_locked_out():
                continue
            due = p.interest_due()
            share = due / total_due
            absorbed = p.pay_interest(min(amount * share, due), rate_provider)
            paid += absorbed
        return paid

    def pay_principal(self, amount: float) -> float:
        return self._distribute_pro_rata(amount, lambda p, a: p.pay_principal(a))

    def pay_writedown(self, amount: float) -> float:
        return self._distribute_pro_rata(amount, lambda p, a: p.pay_writedown(a))

    def _distribute_pro_rata(self, amount: float, pay_fn: Callable[[Payable, float], float]) -> float:
        eligible = [p for p in self._payables if not p.is_locked_out()]
        total_bal = sum(p.current_balance() for p in eligible)
        if total_bal < 0.01:
            return 0.0

        paid = 0.0
        residual = 0.0
        for p in eligible:
            share = p.current_balance() / total_bal
            alloc = amount * share + residual
            cap = min(alloc, p.current_balance())
            absorbed = pay_fn(p, cap)
            paid += absorbed
            residual = alloc - absorbed  # overflow to next tranche

        return paid


# ---------------------------------------------------------------------------
# Shifting-interest structure
# ---------------------------------------------------------------------------

class ShiftingInterestStructure(Payable):
    """
    Split principal between seniors and subordinates by a configurable percentage.

    Mirrors ShiftingInterestStructure.  The senior percentage shifts over time
    (or is held constant).  Seniors get shift_pct * principal; subs get the rest.
    """

    def __init__(
        self,
        shift_pct: float,
        seniors: Payable,
        subordinates: Payable,
    ) -> None:
        self._shift_pct = shift_pct      # decimal (e.g. 0.70 for 70/30)
        self._seniors = seniors
        self._subs = subordinates

    @property
    def is_leaf(self) -> bool:
        return False

    def current_balance(self) -> float:
        return self._seniors.current_balance() + self._subs.current_balance()

    def interest_due(self, days: float = 1.0 / 12.0) -> float:
        return self._seniors.interest_due(days) + self._subs.interest_due(days)

    def pay_interest(self, amount: float, rate_provider=None) -> float:
        paid = self._seniors.pay_interest(amount, rate_provider)
        remaining = amount - paid
        if remaining > 0.01:
            paid += self._subs.pay_interest(remaining, rate_provider)
        return paid

    def pay_principal(self, amount: float) -> float:
        senior_amt = amount * self._shift_pct
        sub_amt = amount - senior_amt
        paid = self._seniors.pay_principal(senior_amt)
        paid += self._subs.pay_principal(sub_amt)
        return paid

    def pay_writedown(self, amount: float) -> float:
        sub_amt = min(amount, self._subs.current_balance())
        paid = self._subs.pay_writedown(sub_amt)
        remaining = amount - paid
        if remaining > 0.01:
            paid += self._seniors.pay_writedown(remaining)
        return paid


# ---------------------------------------------------------------------------
# Fixed-amount structure
# ---------------------------------------------------------------------------

class FixedStructure(Payable):
    """
    Pay a fixed dollar amount to the primary payable; remainder to overflow.

    Mirrors GraamFlows FixedStructure.
    """

    def __init__(self, fixed_amount: float, primary: Payable, overflow: Optional[Payable] = None) -> None:
        self._fixed = fixed_amount
        self._primary = primary
        self._overflow = overflow

    @property
    def is_leaf(self) -> bool:
        return False

    def current_balance(self) -> float:
        total = self._primary.current_balance()
        if self._overflow:
            total += self._overflow.current_balance()
        return total

    def interest_due(self, days: float = 1.0 / 12.0) -> float:
        total = self._primary.interest_due(days)
        if self._overflow:
            total += self._overflow.interest_due(days)
        return total

    def pay_interest(self, amount: float, rate_provider=None) -> float:
        return self._primary.pay_interest(amount, rate_provider)

    def pay_principal(self, amount: float) -> float:
        primary_amt = min(self._fixed, amount)
        paid = self._primary.pay_principal(primary_amt)
        remaining = amount - paid
        if remaining > 0.01 and self._overflow:
            paid += self._overflow.pay_principal(remaining)
        return paid

    def pay_writedown(self, amount: float) -> float:
        return self._primary.pay_writedown(amount)


# ---------------------------------------------------------------------------
# Simplified composable waterfall runner
# ---------------------------------------------------------------------------

@dataclass
class WaterfallPeriodResult:
    """Cashflows produced by the waterfall for a single period."""
    cashflow_date: date
    interest_paid: float = 0.0
    sched_principal_paid: float = 0.0
    unsched_principal_paid: float = 0.0
    recovery_paid: float = 0.0
    writedown_applied: float = 0.0
    excess_interest: float = 0.0
    tranche_cashflows: dict[str, dict] = field(default_factory=dict)


@dataclass
class WaterfallResult:
    """Complete waterfall output across all periods."""
    periods: list[WaterfallPeriodResult] = field(default_factory=list)

    def to_tranche_cashflows(self) -> dict[str, list[dict]]:
        """Reshape results into per-tranche cashflow lists."""
        out: dict[str, list[dict]] = {}
        for p in self.periods:
            for name, cf in p.tranche_cashflows.items():
                out.setdefault(name, []).append({"date": p.cashflow_date, **cf})
        return out


def run_waterfall(
    period_cashflows: list,          # list[PeriodCashflows]
    interest_structure: Payable,
    scheduled_structure: Payable,
    unscheduled_structure: Payable,
    writedown_structure: Payable,
    ledgers: list[TrancheLedger],
    execution_order: Optional[list[str]] = None,
    days_per_period: float = 1.0 / 12.0,
) -> WaterfallResult:
    """
    Run the composable waterfall for a list of collateral cashflow periods.

    Simplified translation of ComposableStructure.RunComposablePeriod().
    Supports the standard CLO execution order:
        INTEREST → PRINCIPAL_SCHEDULED → PRINCIPAL_UNSCHEDULED → PRINCIPAL_RECOVERY → WRITEDOWN → EXCESS_RELEASE

    Args:
        period_cashflows: Collateral cashflows (list of PeriodCashflows).
        interest_structure: Payable tree for interest distribution.
        scheduled_structure: Payable tree for scheduled principal.
        unscheduled_structure: Payable tree for prepay principal.
        writedown_structure: Payable tree for loss allocation (typically reverse sequential).
        ledgers: All TrancheLedger objects (for snapshot capture each period).
        execution_order: Override default step order (list of step name strings).
        days_per_period: Year fraction per period (default 1/12 for monthly).

    Returns:
        WaterfallResult with per-period breakdowns.
    """
    if execution_order is None:
        execution_order = [
            "INTEREST",
            "PRINCIPAL_SCHEDULED",
            "PRINCIPAL_UNSCHEDULED",
            "PRINCIPAL_RECOVERY",
            "WRITEDOWN",
            "EXCESS_RELEASE",
        ]

    result = WaterfallResult()

    for pc in period_cashflows:
        for ledger in ledgers:
            ledger.reset_period()

        avail_interest = pc.net_interest
        avail_sched = pc.scheduled_principal
        avail_unsched = pc.unscheduled_principal
        avail_recov = pc.recovery_principal
        avail_writedown = pc.defaulted_principal * (1.0 - 0.4)  # net of typical severity placeholder

        period_result = WaterfallPeriodResult(cashflow_date=pc.cashflow_date)

        for step in execution_order:
            step = step.upper()

            if step == "INTEREST":
                paid = interest_structure.pay_interest(avail_interest, None)
                period_result.interest_paid = paid
                avail_interest -= paid

            elif step == "PRINCIPAL_SCHEDULED":
                paid = scheduled_structure.pay_principal(avail_sched)
                period_result.sched_principal_paid = paid
                avail_sched -= paid

            elif step == "PRINCIPAL_UNSCHEDULED":
                paid = unscheduled_structure.pay_principal(avail_unsched)
                period_result.unsched_principal_paid = paid
                avail_unsched -= paid

            elif step == "PRINCIPAL_RECOVERY":
                paid = scheduled_structure.pay_principal(avail_recov)
                period_result.recovery_paid = paid
                avail_recov -= paid

            elif step == "WRITEDOWN":
                if avail_writedown > 0:
                    applied = writedown_structure.pay_writedown(avail_writedown)
                    period_result.writedown_applied = applied

            elif step in ("EXCESS", "EXCESS_RELEASE"):
                period_result.excess_interest = max(avail_interest, 0.0)
                avail_interest = 0.0

        # Snapshot tranche cashflows
        for ledger in ledgers:
            period_result.tranche_cashflows[ledger.name] = {
                "interest": ledger.period_interest,
                "principal": ledger.period_principal,
                "writedown": ledger.period_writedown,
                "balance": ledger.current_balance,
            }

        result.periods.append(period_result)

    return result
