from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .day_counters import DayCounter, ActualActualISDA
from .enums import Compounding, Frequency


def _compound_factor(t: float, r: float, freq: Frequency, comp: Compounding) -> float:
    """Compute the compound factor for a given rate, time, and convention."""
    f = int(freq)
    if comp == Compounding.SIMPLE:
        return 1.0 + r * t
    if comp == Compounding.COMPOUNDED:
        if f <= 0:
            raise ValueError(f"Frequency {freq} is invalid for compounded convention")
        return (1.0 + r / f) ** (f * t)
    if comp == Compounding.CONTINUOUS:
        return math.exp(r * t)
    if comp == Compounding.SIMPLE_THEN_COMPOUNDED:
        if f <= 0:
            raise ValueError(f"Frequency {freq} is invalid for simple-then-compounded convention")
        if t <= 1.0 / f:
            return 1.0 + r * t
        return (1.0 + r / f) ** (f * t)
    raise ValueError(f"Unknown compounding: {comp}")


@dataclass
class InterestRate:
    """
    Single-rate interest rate object.

    Mirrors GraamFlows.Util.TermStructure.InterestRate with:
    - compound_factor / discount_factor calculations
    - implied_rate class method for bootstrapping
    """

    rate: float
    day_counter: DayCounter
    compounding: Compounding
    frequency: Frequency
    term: float = float("nan")  # year-fraction term on the curve; NaN when rate is standalone

    def compound_factor(self, t: float) -> float:
        return _compound_factor(t, self.rate, self.frequency, self.compounding)

    def discount_factor(self, t: float, spread: float = 0.0) -> float:
        return 1.0 / _compound_factor(t, self.rate + spread, self.frequency, self.compounding)

    def discount_factor_dates(self, d1: date, d2: date, spread: float = 0.0) -> float:
        t = self.day_counter.year_fraction(d1, d2)
        return self.discount_factor(t, spread)

    def equivalent_rate(
        self,
        comp: Compounding,
        freq: Frequency,
        t: float,
    ) -> "InterestRate":
        cf = self.compound_factor(t)
        return InterestRate.implied_rate(cf, self.day_counter, comp, freq, t)

    @staticmethod
    def implied_rate(
        compound: float,
        day_counter: DayCounter,
        comp: Compounding,
        freq: Frequency,
        t: float,
    ) -> "InterestRate":
        """Back out a rate from a compound factor."""
        f = float(int(freq))
        if abs(compound - 1.0) < 1e-15:
            r = 0.0
        elif comp == Compounding.SIMPLE:
            r = (compound - 1.0) / t
        elif comp == Compounding.COMPOUNDED:
            r = (compound ** (1.0 / (f * t)) - 1.0) * f
        elif comp == Compounding.CONTINUOUS:
            r = math.log(compound) / t
        elif comp == Compounding.SIMPLE_THEN_COMPOUNDED:
            if t <= 1.0 / f:
                r = (compound - 1.0) / t
            else:
                r = (compound ** (1.0 / (f * t)) - 1.0) * f
        else:
            raise ValueError(f"Unknown compounding: {comp}")
        return InterestRate(r, day_counter, comp, freq, t)


class TermStructure:
    """
    Piecewise term structure built from a list of InterestRate objects sorted by term.

    Mirrors GraamFlows.Util.TermStructure.TermStructure with binary-search interpolation.
    Rates between knot points are interpolated from the nearest knot (flat extrapolation
    beyond the longest term).
    """

    def __init__(self, settle_date: date, rates: list[InterestRate]) -> None:
        self.settle_date = settle_date
        self.curve: list[InterestRate] = sorted(rates, key=lambda r: r.term)

    def get_rate(self, t: float) -> InterestRate:
        """Return the InterestRate for a given year-fraction term (binary search, nearest-neighbor)."""
        if not self.curve:
            raise ValueError("TermStructure has no rates")
        terms = [r.term for r in self.curve]
        idx = bisect.bisect_left(terms, t)
        if idx == 0:
            return self.curve[0]
        if idx >= len(self.curve):
            return self.curve[-1]
        # return the closer of the two surrounding knots
        before = self.curve[idx - 1]
        after = self.curve[idx]
        if abs(after.term - t) < abs(t - before.term):
            return after
        return before

    def get_rate_for_date(self, dc: DayCounter, rate_date: date) -> InterestRate:
        t = dc.year_fraction(self.settle_date, rate_date)
        return self.get_rate(t)

    @classmethod
    def from_pairs(
        cls,
        settle_date: date,
        pairs: list[list[float]],  # [[term_years, rate_pct], ...]
        day_counter: DayCounter | None = None,
        compounding: Compounding = Compounding.SIMPLE,
        frequency: Frequency = Frequency.SEMIANNUAL,
    ) -> "TermStructure":
        """Build a TermStructure from [[term, rate_pct], ...] pairs (as used by the pricing API)."""
        if day_counter is None:
            day_counter = ActualActualISDA()
        rates = [
            InterestRate(r / 100.0, day_counter, compounding, frequency, t)
            for t, r in pairs
        ]
        return cls(settle_date, rates)

    @classmethod
    def flat(
        cls,
        settle_date: date,
        rate: float,
        day_counter: DayCounter | None = None,
        compounding: Compounding = Compounding.COMPOUNDED,
        frequency: Frequency = Frequency.SEMIANNUAL,
    ) -> "TermStructure":
        """Build a flat (constant) term structure — useful for yield-based pricing."""
        if day_counter is None:
            day_counter = ActualActualISDA()
        ir = InterestRate(rate, day_counter, compounding, frequency, 0.0)
        return cls(settle_date, [ir])
