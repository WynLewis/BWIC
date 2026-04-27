"""
Bond analytics engine.

Direct Python translation of GraamFlows.Util.Finance.Cashflow (C#).

Provides:
- NPV / price from yield or spread
- Yield / spread / DM from price
- WAL, modified duration, accrued interest
- Standalone helper functions usable without constructing BondAnalytics

All prices are expressed as a percentage of par (e.g. 99.5 means $99.50 per $100 face).
Yields and rates are in percent (e.g. 5.0 means 5% p.a.).
Spreads are in basis points (e.g. 150 means 150 bps).
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Optional

from .day_counters import ActualActualISDA, DayCounter, Thirty360Us, get_day_counter
from .enums import Compounding, Frequency
from .interest_rate import InterestRate, TermStructure
from .models import CashflowEntry, CashflowStream, PricingParams, PricingResult
from .solvers import Brent, ConvergenceError, RootBracketingError


# ---------------------------------------------------------------------------
# Core NPV engine
# ---------------------------------------------------------------------------

def npv(
    stream: CashflowStream,
    rate_func: Callable[[CashflowEntry], InterestRate],
    settle_date: date,
    spread: float = 0.0,
) -> float:
    """
    Compute the clean NPV (as % of par) of a cashflow stream.

    Mirrors Cashflow.NPV() — iterates cashflows, chains discount factors, divides by balance.

    Args:
        stream: CashflowStream containing cashflows and metadata.
        rate_func: Given a CashflowEntry, return the InterestRate for that period.
        settle_date: Settlement date; cashflows before this date are ignored.
        spread: Additional spread (in decimal, not bps) added to each period's rate.

    Returns:
        Clean price as % of par.
    """
    if not stream.cashflows:
        return 0.0

    total_npv = 0.0
    discount = 1.0
    last_date = settle_date

    for cf in stream.cashflows:
        cf_date = cf.cashflow_date
        if cf_date < settle_date:
            continue

        ncf = cf.cashflow / stream.balance

        if cf_date != settle_date:
            rate = rate_func(cf)
            b = rate.discount_factor_dates(last_date, cf_date, spread)
            discount *= b

        last_date = cf_date
        total_npv += ncf * discount

    return total_npv * 100.0


# ---------------------------------------------------------------------------
# DM term structure builder
# ---------------------------------------------------------------------------

def _build_dm_term_structure(stream: CashflowStream) -> TermStructure:
    """
    Build a DM term structure from the index values embedded in the cashflow stream.

    For floating-rate CLO tranches the index value (SOFR/LIBOR) is stored per period.
    The DM calc discounts at (index + dm_spread) using simple compounding.
    """
    dc = get_day_counter(stream.day_counter_name)
    rates = [
        InterestRate(
            rate=cf.index_value / 100.0,
            day_counter=dc,
            compounding=Compounding.SIMPLE,
            frequency=stream.frequency,
            term=dc.year_fraction(stream.settle_date, cf.cashflow_date),
        )
        for cf in stream.cashflows
        if cf.cashflow_date >= stream.settle_date
    ]
    return TermStructure(stream.settle_date, rates)


# ---------------------------------------------------------------------------
# BondAnalytics class
# ---------------------------------------------------------------------------

class BondAnalytics:
    """
    Bond analytics calculator.

    Wraps a CashflowStream and optional TermStructure to compute standard
    fixed-income analytics: price, yield, Z-spread, DM, WAL, duration, accrued.

    Usage::

        stream = CashflowStream(cashflows=[...], settle_date=..., balance=..., ...)
        analytics = BondAnalytics(stream)

        price = analytics.price_from_yield(5.5)
        yld   = analytics.yield_from_price(98.5)
        dm    = analytics.dm_from_price(99.0)
    """

    def __init__(
        self,
        stream: CashflowStream,
        term_structure: Optional[TermStructure] = None,
    ) -> None:
        self.stream = stream
        self.ts = term_structure
        self._dc: DayCounter = get_day_counter(stream.day_counter_name)

    # ------------------------------------------------------------------
    # Price from yield / spread
    # ------------------------------------------------------------------

    def price_from_yield(self, yield_pct: float) -> float:
        """
        Compute clean price from a yield (% p.a.).

        Mirrors Cashflow.PriceFromYield().
        """
        rate = InterestRate(
            rate=yield_pct / 100.0,
            day_counter=self._dc,
            compounding=self.stream.compounding,
            frequency=self.stream.frequency,
        )
        return npv(self.stream, lambda _: rate, self.stream.settle_date)

    def price_from_dm(self, dm_bps: float) -> float:
        """
        Compute clean price from a discount margin (bps).

        The DM term structure is built from index values in the cashflow stream
        (SOFR/LIBOR per period). Spread is added on top in simple compounding.

        Mirrors Cashflow.PriceFromSpread(CurveType.DiscountMargin, spread).
        """
        dm_ts = _build_dm_term_structure(self.stream)
        spread_dec = dm_bps / 10_000.0

        def rate_func(cf: CashflowEntry) -> InterestRate:
            return dm_ts.get_rate_for_date(self._dc, cf.cashflow_date)

        return npv(self.stream, rate_func, self.stream.settle_date, spread_dec)

    def price_from_spread(self, spread_bps: float, term_structure: Optional[TermStructure] = None) -> float:
        """
        Compute clean price from a Z-spread (bps) over a provided term structure.

        Mirrors Cashflow.PriceFromSpread(ITermStructure, spread).
        """
        ts = term_structure or self.ts
        if ts is None:
            raise ValueError("A TermStructure is required for Z-spread pricing")

        spread_dec = spread_bps / 10_000.0

        def rate_func(cf: CashflowEntry) -> InterestRate:
            return ts.get_rate_for_date(self._dc, cf.cashflow_date)

        return npv(self.stream, rate_func, self.stream.settle_date, spread_dec)

    # ------------------------------------------------------------------
    # Yield / spread from price
    # ------------------------------------------------------------------

    def yield_from_price(self, price: float) -> float:
        """
        Solve for yield (% p.a.) given a clean price.

        Mirrors Cashflow.YieldFromPrice().
        Returns -100 if the solver fails to converge.
        """
        self._assert_future_cashflows("yield_from_price")

        def objective(y: float) -> float:
            rate = InterestRate(
                rate=y,
                day_counter=self._dc,
                compounding=self.stream.compounding,
                frequency=self.stream.frequency,
            )
            return npv(self.stream, lambda _: rate, self.stream.settle_date) - price

        try:
            solver = Brent(max_evaluations=1000)
            y = solver.solve(objective, 1e-7, 0.10, -1.0, 10.0)
            return y * 100.0
        except (ConvergenceError, RootBracketingError, ValueError):
            return -100.0

    def dm_from_price(self, price: float) -> float:
        """
        Solve for discount margin (bps) given a clean price.

        Mirrors Cashflow.SpreadFromPrice(CurveType.DiscountMargin, price).
        Returns -10_000 if the solver fails to converge.
        """
        self._assert_future_cashflows("dm_from_price")
        dm_ts = _build_dm_term_structure(self.stream)

        def rate_func(cf: CashflowEntry) -> InterestRate:
            return dm_ts.get_rate_for_date(self._dc, cf.cashflow_date)

        return self._solve_spread(price, rate_func)

    def spread_from_price(
        self, price: float, term_structure: Optional[TermStructure] = None
    ) -> float:
        """
        Solve for Z-spread (bps) given a clean price and a term structure.

        Mirrors Cashflow.SpreadFromPrice(ITermStructure, price).
        Returns -10_000 if the solver fails to converge.
        """
        self._assert_future_cashflows("spread_from_price")
        ts = term_structure or self.ts
        if ts is None:
            raise ValueError("A TermStructure is required for Z-spread solving")

        def rate_func(cf: CashflowEntry) -> InterestRate:
            return ts.get_rate_for_date(self._dc, cf.cashflow_date)

        return self._solve_spread(price, rate_func)

    def _solve_spread(
        self,
        price: float,
        rate_func: Callable[[CashflowEntry], InterestRate],
    ) -> float:
        """Internal: solve for spread given a rate function. Returns spread in bps."""
        def objective(s: float) -> float:
            return npv(self.stream, rate_func, self.stream.settle_date, s) - price

        try:
            solver = Brent(max_evaluations=1000)
            spread = solver.solve(objective, 1e-7, 0.10, -1.0, 10.0)
            return spread * 10_000.0
        except (ConvergenceError, RootBracketingError, ValueError):
            return -10_000.0

    # ------------------------------------------------------------------
    # WAL
    # ------------------------------------------------------------------

    def wal(self) -> float:
        """
        Weighted-average life in years (Actual/Actual ISDA day count).

        Mirrors Cashflow.WeightedAverageLife().
        """
        dc = ActualActualISDA()
        eligible = [
            cf for cf in self.stream.cashflows
            if cf.cashflow_date >= self.stream.settle_date and cf.principal >= 0
        ]

        def _principal(cf: CashflowEntry) -> float:
            if self.stream.is_io:
                return cf.prev_balance - cf.balance
            return cf.principal

        items = [
            (dc.year_fraction(self.stream.settle_date, cf.cashflow_date), _principal(cf))
            for cf in eligible
        ]

        total = sum(p for _, p in items)
        if total < 0.01:
            return 0.0
        return sum(t * p for t, p in items) / total

    def balance_wal(self) -> float:
        """
        Balance-weighted WAL using 30/360 day count.

        Mirrors Cashflow.BalanceWeightedAverageLife().
        """
        dc = Thirty360Us()
        eligible = [
            cf for cf in self.stream.cashflows
            if cf.cashflow_date >= self.stream.settle_date and cf.principal >= 0
        ]
        items = [
            (
                dc.year_fraction(self.stream.settle_date, cf.cashflow_date),
                cf.prev_balance - cf.balance,
            )
            for cf in eligible
        ]
        total = sum(p for _, p in items)
        if total < 0.01:
            return 0.0
        return sum(t * p for t, p in items) / total

    # ------------------------------------------------------------------
    # Modified duration
    # ------------------------------------------------------------------

    def modified_duration(
        self,
        price: float,
        spread: float,
        shock_bps: float = 1.0,
        term_structure: Optional[TermStructure] = None,
    ) -> float:
        """
        Modified duration computed by shocking the spread ± shock/2 bps.

        Mirrors Cashflow.ModifiedDuration().

        Args:
            price: Clean price used as the denominator.
            spread: Initial spread in bps (Z-spread or DM).
            shock_bps: Total shock size in bps (default 1 bp for DV01-style).
            term_structure: If None, uses DM curve from index values.
        """
        if spread <= -10_000:
            return 0.0

        if term_structure is not None:
            px_up = self.price_from_spread(spread + shock_bps / 2.0, term_structure)
            px_dn = self.price_from_spread(spread - shock_bps / 2.0, term_structure)
        else:
            px_up = self.price_from_dm(spread + shock_bps / 2.0)
            px_dn = self.price_from_dm(spread - shock_bps / 2.0)

        return (px_dn - px_up) / price / (shock_bps / 10_000.0)

    # ------------------------------------------------------------------
    # Accrued interest
    # ------------------------------------------------------------------

    def accrued_interest(self) -> float:
        """
        Accrued interest as % of par from the start of the current coupon period.

        Mirrors Cashflow.AccruedAmount().
        Returns 0 if no start accrual period is set.
        """
        first_cf = next(
            (cf for cf in self.stream.cashflows if cf.cashflow_date >= self.stream.settle_date),
            None,
        )
        if first_cf is None:
            return 0.0
        if self.stream.start_accrual_period is None:
            return 0.0

        delay = -self.stream.pay_delay
        start_acc = _add_days(self.stream.start_accrual_period, delay)
        cf_date = _add_days(first_cf.cashflow_date, delay)

        if start_acc >= self.stream.settle_date:
            return 0.0

        acc_days = self._dc.day_count(start_acc, self.stream.settle_date)
        acc_period = self._dc.day_count(start_acc, cf_date)
        interest = first_cf.interest

        acc_amt = acc_days / acc_period * interest
        return 100.0 * acc_amt / self.stream.balance

    # ------------------------------------------------------------------
    # Principal window
    # ------------------------------------------------------------------

    def first_prin_date(self) -> Optional[date]:
        """First date on which principal > 0."""
        try:
            return next(cf.cashflow_date for cf in self.stream.cashflows if cf.principal > 0)
        except StopIteration:
            return None

    def last_prin_date(self) -> Optional[date]:
        """Last date on which principal > 0."""
        result = None
        for cf in self.stream.cashflows:
            if cf.principal > 0:
                result = cf.cashflow_date
        return result

    def prin_window_years(self) -> tuple[float, float]:
        """(first_prin_year, last_prin_year) from settle date."""
        first = self.first_prin_date()
        if first is None:
            return (0.0, 0.0)
        last = self.last_prin_date()
        dc = self._dc
        t0 = max(0.0, dc.year_fraction(self.stream.settle_date, first))
        t1 = max(0.0, dc.year_fraction(self.stream.settle_date, last))
        return (t0, t1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _assert_future_cashflows(self, method: str) -> None:
        if not self.stream.future_cashflows():
            last = max((cf.cashflow_date for cf in self.stream.cashflows), default=None)
            raise ValueError(
                f"{method}: No cashflows after settle date {self.stream.settle_date}. "
                f"Last cashflow date: {last}."
            )


def _add_days(d: date, days: int) -> date:
    from datetime import timedelta
    return d + timedelta(days=days)


# ---------------------------------------------------------------------------
# Standalone convenience: compute all analytics from raw inputs
# ---------------------------------------------------------------------------

def compute_analytics(
    cashflows: list[dict],
    params: PricingParams,
    rates: Optional[list[list[float]]] = None,
) -> PricingResult:
    """
    High-level entry point matching the /api/pricing REST endpoint.

    Args:
        cashflows: List of dicts with keys date, interest, principal, balance, index_value.
        params: PricingParams specifying input type and settlement info.
        rates: Optional [[term_years, rate_pct], ...] for Z-spread / duration calc.

    Returns:
        PricingResult with price, yield, spread, dm, duration, wal, accrued.
    """
    from .models import CashflowEntry, CashflowStream
    from .enums import Compounding, Frequency

    dc_name = params.day_count or "Actual360"
    comp_map = {
        "simple": Compounding.SIMPLE,
        "compounded": Compounding.COMPOUNDED,
        "continuous": Compounding.CONTINUOUS,
        "semiannual": Compounding.COMPOUNDED,  # common alias
    }
    comp = comp_map.get((params.compounding or "simple").lower(), Compounding.SIMPLE)

    entries = [
        CashflowEntry(
            cashflow_date=cf["date"] if isinstance(cf["date"], date) else date.fromisoformat(str(cf["date"])),
            interest=float(cf.get("interest", 0)),
            principal=float(cf.get("principal", 0)),
            balance=float(cf.get("balance", 0)),
            prev_balance=float(cf.get("prev_balance", cf.get("balance", 0))) + float(cf.get("principal", 0)),
            index_value=float(cf.get("index_value", 0) or 0),
        )
        for cf in cashflows
    ]

    stream = CashflowStream(
        cashflows=entries,
        settle_date=params.settle_date,
        balance=params.balance,
        day_counter_name=dc_name,
        compounding=comp,
        frequency=Frequency.MONTHLY,
        start_accrual_period=params.start_accrual_period,
        pay_delay=params.pay_delay,
    )

    ts: Optional[TermStructure] = None
    if rates:
        ts = TermStructure.from_pairs(params.settle_date, rates)

    ba = BondAnalytics(stream, ts)
    result = PricingResult()

    input_type = (params.input_type or "price").lower()

    if input_type == "price":
        price = params.input_value
        result.price = price
        result.yld = ba.yield_from_price(price)
        result.dm = ba.dm_from_price(price)
        if ts:
            result.spread = ba.spread_from_price(price, ts)
            result.modified_duration = ba.modified_duration(price, result.dm)

    elif input_type == "yield":
        yld = params.input_value
        result.yld = yld
        result.price = ba.price_from_yield(yld)
        result.dm = ba.dm_from_price(result.price)
        if ts:
            result.spread = ba.spread_from_price(result.price, ts)

    elif input_type in ("spread", "zspread"):
        if ts is None:
            raise ValueError("Z-spread input requires a term structure (rates)")
        result.spread = params.input_value
        result.price = ba.price_from_spread(params.input_value, ts)
        result.yld = ba.yield_from_price(result.price)
        result.dm = ba.dm_from_price(result.price)

    elif input_type == "dm":
        result.dm = params.input_value
        result.price = ba.price_from_dm(params.input_value)
        result.yld = ba.yield_from_price(result.price)
        if ts:
            result.spread = ba.spread_from_price(result.price, ts)

    result.wal = ba.wal()
    result.accrued_interest = ba.accrued_interest()
    if result.price is not None:
        result.dirty_price = result.price + (result.accrued_interest or 0.0)

    return result
