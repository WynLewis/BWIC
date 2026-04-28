"""
graam — Python port of the Graam structured-credit execution engine.

Provides three engines:
  - Collateral engine  (amortizer.generate_cashflows)
  - Waterfall engine   (waterfall.run_waterfall)
  - Pricing engine     (analytics.BondAnalytics / compute_analytics)

Quick start::

    from graam.analytics import compute_analytics
    from graam.models import PricingParams
    from datetime import date

    params = PricingParams(
        input_type="price",
        input_value=99.5,
        settle_date=date(2024, 1, 25),
        balance=10_000_000,
        day_count="Actual360",
    )
    result = compute_analytics(cashflows=[...], params=params)
    print(result.dm, result.wal)
"""

from .analytics import BondAnalytics, compute_analytics, npv
from .amortizer import generate_cashflows
from .bwic_pricing import (
    BASE,
    MARKET,
    STANDARD_SCENARIOS,
    BwicLine as BwicPricingLine,
    BwicLineMulti,
    BwicMultiResult,
    BwicResult,
    IntexScenario,
    dm_price_table,
    price_bwic,
    price_bwic_line,
    price_bwic_line_multi,
    price_bwic_multi,
    to_dataframe,
)
from .bwic_workflow import (
    Bid,
    BidError,
    Bwic,
    BwicLine,
    LineAward,
    LineState,
)
from .intex_loader import (
    load_intex_dataframe,
    load_intex_excel,
    stream_from_records,
)
from .day_counters import Actual360, Actual365, ActualActualISDA, Thirty360Us, get_day_counter
from .enums import (
    Compounding,
    CouponType,
    Frequency,
    InterestRateType,
    PrepaymentType,
    TrancheType,
)
from .interest_rate import InterestRate, TermStructure
from .models import (
    Assumptions,
    Asset,
    CashflowEntry,
    CashflowStream,
    Deal,
    PeriodCashflows,
    PricingParams,
    PricingResult,
    Tranche,
    TrancheCashflow,
)
from .solvers import Brent, ConvergenceError, RootBracketingError

__version__ = "0.1.0"

__all__ = [
    # analytics
    "BondAnalytics",
    "compute_analytics",
    "npv",
    # amortizer
    "generate_cashflows",
    # day counters
    "Actual360",
    "Actual365",
    "ActualActualISDA",
    "Thirty360Us",
    "get_day_counter",
    # enums
    "Compounding",
    "CouponType",
    "Frequency",
    "InterestRateType",
    "PrepaymentType",
    "TrancheType",
    # interest rate
    "InterestRate",
    "TermStructure",
    # models
    "Assumptions",
    "Asset",
    "CashflowEntry",
    "CashflowStream",
    "Deal",
    "PeriodCashflows",
    "PricingParams",
    "PricingResult",
    "Tranche",
    "TrancheCashflow",
    # solvers
    "Brent",
    "ConvergenceError",
    "RootBracketingError",
]
