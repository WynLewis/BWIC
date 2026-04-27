from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .enums import (
    BusinessDayConvention,
    CashflowType,
    Compounding,
    CouponType,
    Frequency,
    InterestRateType,
    PrepaymentType,
    TrancheType,
)


# ---------------------------------------------------------------------------
# Cashflow / stream models
# ---------------------------------------------------------------------------

@dataclass
class CashflowEntry:
    """Single period cashflow, matching CashflowEntryDto / ICashflow in C#."""

    cashflow_date: date
    interest: float
    principal: float
    balance: float
    prev_balance: float = 0.0
    index_value: float = 0.0  # index rate for this period (used for DM calc)

    @property
    def cashflow(self) -> float:
        return self.interest + self.principal


@dataclass
class CashflowStream:
    """
    A stream of cashflows used for pricing.

    Mirrors ICashflowStream; holds all the metadata the analytics engine needs
    alongside the cashflow list.
    """

    cashflows: list[CashflowEntry]
    settle_date: date
    balance: float
    day_counter_name: str = "Actual360"
    compounding: Compounding = Compounding.SIMPLE
    frequency: Frequency = Frequency.MONTHLY
    is_io: bool = False
    start_accrual_period: Optional[date] = None
    pay_delay: int = 0

    def future_cashflows(self) -> list[CashflowEntry]:
        return [cf for cf in self.cashflows if cf.cashflow_date >= self.settle_date and cf.cashflow > 0]


# ---------------------------------------------------------------------------
# Asset (loan / pool)
# ---------------------------------------------------------------------------

@dataclass
class Asset:
    """
    Loan pool asset.

    Mirrors GraamFlows.Domain.Asset and IAsset.
    All numeric rates are in percent (e.g. 6.5 means 6.5% p.a.).
    """

    asset_name: str = ""
    asset_id: str = ""
    interest_rate_type: InterestRateType = InterestRateType.FRM
    original_date: Optional[date] = None        # origination date
    original_balance: float = 0.0
    original_interest_rate: float = 0.0         # % p.a.
    current_interest_rate: float = 0.0          # % p.a. (0 = use original)
    original_amortization_term: int = 0         # months
    current_balance: float = 0.0
    balance_at_issuance: float = 0.0
    original_ltv: float = 0.0
    group_num: str = "1"
    loan_status: str = ""
    service_fee: float = 0.0                    # % p.a.
    debt_service: float = 0.0                   # override scheduled payment
    is_io: bool = False
    io_term: int = 0                            # months of interest-only
    initial_adjustment_period: int = 0          # ARM: initial fixed period
    adjustment_period: int = 0                  # ARM: reset frequency
    index_margin: float = 0.0                   # ARM: spread over index
    adjustment_cap: float = 0.0                 # ARM: per-reset cap
    life_adjustment_cap: float = 0.0            # ARM: lifetime ceiling
    life_adjustment_floor: float = 0.0          # ARM: lifetime floor
    forbearance_amt: float = 0.0
    wala: int = 0                               # weighted avg loan age (months)


# ---------------------------------------------------------------------------
# Tranche
# ---------------------------------------------------------------------------

@dataclass
class Tranche:
    """
    Bond tranche definition.

    Mirrors GraamFlows.Domain.Tranche and ITranche.
    All rates in percent (e.g. 5.0 means 5% coupon).
    """

    deal_name: str = ""
    tranche_name: str = ""
    cusip: str = ""
    coupon_type: CouponType = CouponType.FIXED
    cashflow_type: CashflowType = CashflowType.PI
    tranche_type: TrancheType = TrancheType.OFFERED
    original_balance: float = 0.0
    factor: float = 1.0
    fixed_coupon: float = 0.0                   # % p.a. for fixed tranches
    floater_index: str = ""                     # e.g. "SOFR", "Libor3M"
    floater_spread: float = 0.0                 # % p.a. spread over index
    cap: float = 0.0
    floor: float = 0.0
    first_pay_date: Optional[date] = None
    legal_maturity_date: Optional[date] = None
    stated_maturity_date: Optional[date] = None
    pay_frequency: int = 12                     # payments per year
    pay_delay: int = 0                          # days
    pay_day: int = 25
    day_count: str = "Actual360"
    business_day_convention: str = "ModifiedFollowing"
    holiday_calendar: str = ""
    interest_priority: int = 0
    description: str = ""

    @property
    def current_balance(self) -> float:
        return self.original_balance * self.factor

    @property
    def frequency(self) -> Frequency:
        mapping = {1: Frequency.ANNUAL, 2: Frequency.SEMIANNUAL, 4: Frequency.QUARTERLY, 12: Frequency.MONTHLY}
        return mapping.get(self.pay_frequency, Frequency.MONTHLY)

    def __str__(self) -> str:
        return (
            f"{self.deal_name}/{self.tranche_name} "
            f"Oface:{self.original_balance:,.0f} "
            f"Cface:{self.current_balance:,.0f}"
        )


# ---------------------------------------------------------------------------
# Deal
# ---------------------------------------------------------------------------

@dataclass
class Deal:
    """
    Structured deal (CLO / ABS) definition.

    Minimal representation matching what the waterfall engine needs.
    """

    deal_name: str = ""
    closing_date: Optional[date] = None
    tranches: list[Tranche] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    execution_order: list[str] = field(default_factory=list)
    waterfall_type: str = "ComposableStructure"


# ---------------------------------------------------------------------------
# Collateral cashflow output
# ---------------------------------------------------------------------------

@dataclass
class PeriodCashflows:
    """
    Aggregated collateral cashflows for a single period.

    Mirrors GraamFlows.Objects.DataObjects.PeriodCashflows.
    All amounts in currency units.
    """

    cashflow_date: date
    group_num: str = "1"
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
    accum_forbearance: float = 0.0
    forbearance_liquidated: float = 0.0
    forbearance_recovery: float = 0.0
    wam: float = 0.0
    wala: float = 0.0


# ---------------------------------------------------------------------------
# Tranche cashflow output
# ---------------------------------------------------------------------------

@dataclass
class TrancheCashflow:
    """Single-period cashflow for one tranche."""

    cashflow_date: date
    tranche_name: str
    begin_balance: float = 0.0
    balance: float = 0.0
    interest: float = 0.0
    principal: float = 0.0
    writedown: float = 0.0
    accrued_interest: float = 0.0


# ---------------------------------------------------------------------------
# Pricing inputs / outputs (matching PricingRequest / PricingResponse)
# ---------------------------------------------------------------------------

@dataclass
class PricingParams:
    """Inputs to the bond analytics engine."""

    input_type: str = "price"          # "price" | "yield" | "spread" | "dm"
    input_value: float = 0.0
    settle_date: Optional[date] = None
    balance: float = 0.0
    day_count: str = "Actual360"
    compounding: str = "Simple"
    start_accrual_period: Optional[date] = None
    pay_delay: int = 0


@dataclass
class PricingResult:
    """Output of the bond analytics engine."""

    price: Optional[float] = None
    dirty_price: Optional[float] = None
    yld: Optional[float] = None        # yield (renamed to avoid shadowing builtin)
    spread: Optional[float] = None     # Z-spread in bps
    dm: Optional[float] = None         # discount margin in bps
    modified_duration: Optional[float] = None
    wal: Optional[float] = None
    accrued_interest: Optional[float] = None


# ---------------------------------------------------------------------------
# Assumptions
# ---------------------------------------------------------------------------

@dataclass
class Assumptions:
    """
    Prepayment / credit assumptions for collateral projection.

    Scalars broadcast to vectors; vectors override per-period.
    Rates are in percent (e.g. cpr=6.0 means 6% CPR).
    """

    cpr: float = 0.0
    cdr: float = 0.0
    severity: float = 0.0
    delinquency: float = 0.0
    prepayment_type: PrepaymentType = PrepaymentType.CPR
    cpr_vector: Optional[list[float]] = None
    cdr_vector: Optional[list[float]] = None
    severity_vector: Optional[list[float]] = None
