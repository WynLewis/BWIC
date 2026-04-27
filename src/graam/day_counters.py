from __future__ import annotations

import calendar
from abc import ABC, abstractmethod
from datetime import date


class DayCounter(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    def day_count(self, start: date, end: date) -> int:
        return (end - start).days

    def year_fraction(self, start: date, end: date) -> float:
        return self._year_fraction_impl(start, end, start, end)

    @abstractmethod
    def _year_fraction_impl(
        self, start: date, end: date, ref_start: date, ref_end: date
    ) -> float: ...


class Thirty360Us(DayCounter):
    """30/360 US Bond Basis day count convention."""

    @property
    def name(self) -> str:
        return "30/360 (Bond Basis)"

    def day_count(self, start: date, end: date) -> int:
        dd1, dd2 = start.day, end.day
        mm1, mm2 = start.month, end.month
        yy1, yy2 = start.year, end.year

        if dd2 == 31 and dd1 < 30:
            dd2 = 1
            mm2 += 1

        return 360 * (yy2 - yy1) + 30 * (mm2 - mm1 - 1) + max(0, 30 - dd1) + min(30, dd2)

    def _year_fraction_impl(self, start, end, ref_start, ref_end) -> float:
        return self.day_count(start, end) / 360.0


class ActualActualISDA(DayCounter):
    """Actual/Actual (ISDA) day count convention."""

    @property
    def name(self) -> str:
        return "Actual/Actual (ISDA)"

    def _year_fraction_impl(self, start, end, ref_start, ref_end) -> float:
        if start == end:
            return 0.0
        if start > end:
            return -self.year_fraction(end, start)

        y1, y2 = start.year, end.year
        dib1 = 366 if calendar.isleap(y1) else 365
        dib2 = 366 if calendar.isleap(y2) else 365

        total = float(y2 - y1 - 1)
        total += self.day_count(start, date(y1 + 1, 1, 1)) / dib1
        total += self.day_count(date(y2, 1, 1), end) / dib2
        return total


class Actual360(DayCounter):
    """Actual/360 day count convention."""

    @property
    def name(self) -> str:
        return "Actual/360"

    def _year_fraction_impl(self, start, end, ref_start, ref_end) -> float:
        return self.day_count(start, end) / 360.0


class Actual365(DayCounter):
    """Actual/365 (Fixed) day count convention."""

    @property
    def name(self) -> str:
        return "Actual/365 (Fixed)"

    def _year_fraction_impl(self, start, end, ref_start, ref_end) -> float:
        return self.day_count(start, end) / 365.0


class ActualActualISMA(DayCounter):
    """Actual/Actual (ISMA) day count convention."""

    @property
    def name(self) -> str:
        return "Actual/Actual (ISMA)"

    def _year_fraction_impl(self, start, end, ref_start, ref_end) -> float:
        if start == end:
            return 0.0

        ref_days = self.day_count(ref_start, ref_end)
        if ref_days == 0:
            return 0.0

        # Number of whole periods in the reference
        from dateutil.relativedelta import relativedelta  # type: ignore
        delta = relativedelta(ref_end, ref_start)
        months = delta.months + 12 * delta.years
        freq = 12 / months if months > 0 else 1

        actual_days = self.day_count(start, end)
        return actual_days / (ref_days * freq)


_REGISTRY: dict[str, DayCounter] = {}


def _reg(dc: DayCounter) -> DayCounter:
    _REGISTRY[dc.name] = dc
    return dc


_THIRTY_360_US = _reg(Thirty360Us())
_ACTUAL_ACTUAL_ISDA = _reg(ActualActualISDA())
_ACTUAL_360 = _reg(Actual360())
_ACTUAL_365 = _reg(Actual365())


def get_day_counter(name: str) -> DayCounter:
    """Return a DayCounter by name string (case-insensitive, flexible matching)."""
    n = name.strip().lower().replace(" ", "").replace("/", "").replace("-", "").replace("_", "")
    mapping = {
        "30360": Thirty360Us(),
        "thirty360": Thirty360Us(),
        "bondbasis": Thirty360Us(),
        "actual360": Actual360(),
        "act360": Actual360(),
        "actual365": Actual365(),
        "act365": Actual365(),
        "actualactual": ActualActualISDA(),
        "actualactualisda": ActualActualISDA(),
        "actactisda": ActualActualISDA(),
        "isda": ActualActualISDA(),
        "actualactualisma": ActualActualISMA(),
        "actactisma": ActualActualISMA(),
        "isma": ActualActualISMA(),
    }
    result = mapping.get(n)
    if result is None:
        raise ValueError(f"Unknown day count convention: {name!r}")
    return result
