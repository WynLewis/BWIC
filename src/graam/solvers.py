from __future__ import annotations

import math
from typing import Callable

EPSILON = 1e-15
_GROWTH_FACTOR = 1.6


class ConvergenceError(Exception):
    pass


class RootBracketingError(Exception):
    pass


class Brent:
    """
    Brent root-finding solver.

    Direct translation of GraamFlows.Util.Solvers1D.Brent / Solver1D<T>.
    Mirrors the bracketing + inverse-quadratic-interpolation algorithm from
    Numerical Recipes in C (Press, Teukolsky, Vetterling, Flannery), 2nd ed.
    """

    def __init__(self, max_evaluations: int = 1000) -> None:
        self.max_evaluations = max_evaluations
        self._lower_bound: float | None = None
        self._upper_bound: float | None = None

    def set_lower_bound(self, lb: float) -> None:
        self._lower_bound = lb

    def set_upper_bound(self, ub: float) -> None:
        self._upper_bound = ub

    def _enforce_bounds(self, x: float) -> float:
        if self._lower_bound is not None and x < self._lower_bound:
            return self._lower_bound
        if self._upper_bound is not None and x > self._upper_bound:
            return self._upper_bound
        return x

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def solve(
        self,
        f: Callable[[float], float],
        accuracy: float,
        guess: float,
        x_min: float,
        x_max: float,
    ) -> float:
        """Solve f(x) == 0 given a bracket [x_min, x_max]."""
        accuracy = max(accuracy, EPSILON)

        if not (x_min < x_max):
            raise ValueError(f"Invalid bracket: x_min={x_min} >= x_max={x_max}")

        fx_min = f(x_min)
        if fx_min == 0.0:
            return x_min

        fx_max = f(x_max)
        if fx_max == 0.0:
            return x_max

        if fx_min * fx_max >= 0.0:
            raise RootBracketingError(
                f"Root not bracketed: f[{x_min},{x_max}] -> [{fx_min},{fx_max}]"
            )
        if not (x_min < guess < x_max):
            guess = (x_min + x_max) / 2.0

        return self._solve_impl(f, accuracy, x_min, x_max, fx_min, fx_max, guess)

    def solve_with_step(
        self,
        f: Callable[[float], float],
        accuracy: float,
        guess: float,
        step: float,
    ) -> float:
        """Solve f(x) == 0 using an initial guess + step to bracket the root."""
        accuracy = max(accuracy, EPSILON)

        root = guess
        fx_max = f(root)

        if fx_max == 0.0:
            return root

        if fx_max > 0.0:
            x_min = self._enforce_bounds(root - step)
            fx_min = f(x_min)
            x_max = root
        else:
            x_min = root
            fx_min = fx_max
            x_max = self._enforce_bounds(root + step)
            fx_max = f(x_max)

        eval_count = 2
        flipflop = -1

        while eval_count <= self.max_evaluations:
            if fx_min * fx_max <= 0.0:
                if fx_min == 0.0:
                    return x_min
                if fx_max == 0.0:
                    return x_max
                root = (x_max + x_min) / 2.0
                return self._solve_impl(f, accuracy, x_min, x_max, fx_min, fx_max, root)

            if abs(fx_min) < abs(fx_max):
                x_min = self._enforce_bounds(x_min + _GROWTH_FACTOR * (x_min - x_max))
                fx_min = f(x_min)
            elif abs(fx_min) > abs(fx_max):
                x_max = self._enforce_bounds(x_max + _GROWTH_FACTOR * (x_max - x_min))
                fx_max = f(x_max)
            elif flipflop == -1:
                x_min = self._enforce_bounds(x_min + _GROWTH_FACTOR * (x_min - x_max))
                fx_min = f(x_min)
                eval_count += 1
                flipflop = 1
            else:
                x_max = self._enforce_bounds(x_max + _GROWTH_FACTOR * (x_max - x_min))
                fx_max = f(x_max)
                flipflop = -1

            eval_count += 1

        raise RootBracketingError(
            f"Unable to bracket root in {self.max_evaluations} evaluations"
        )

    # ------------------------------------------------------------------
    # Internal Brent iteration (Numerical Recipes algorithm)
    # ------------------------------------------------------------------

    def _solve_impl(
        self,
        f: Callable[[float], float],
        accuracy: float,
        x_min: float,
        x_max: float,
        fx_min: float,
        fx_max: float,
        root: float,
    ) -> float:
        d = 0.0
        e = 0.0
        froot = fx_max
        eval_count = 2

        while eval_count <= self.max_evaluations:
            if (froot > 0.0 and fx_max > 0.0) or (froot < 0.0 and fx_max < 0.0):
                x_max = x_min
                fx_max = fx_min
                e = d = root - x_min

            if abs(fx_max) < abs(froot):
                x_min = root
                root = x_max
                x_max = x_min
                fx_min = froot
                froot = fx_max
                fx_max = fx_min

            x_acc1 = 2.0 * EPSILON * abs(root) + 0.5 * accuracy
            x_mid = (x_max - root) / 2.0

            if abs(x_mid) <= x_acc1 or froot == 0.0:
                return root

            if abs(e) >= x_acc1 and abs(fx_min) > abs(froot):
                s = froot / fx_min
                if x_min == x_max:
                    p = 2.0 * x_mid * s
                    q = 1.0 - s
                else:
                    q_val = fx_min / fx_max
                    r = froot / fx_max
                    p = s * (2.0 * x_mid * q_val * (q_val - r) - (root - x_min) * (r - 1.0))
                    q = (q_val - 1.0) * (r - 1.0) * (s - 1.0)

                if p > 0.0:
                    q = -q
                p = abs(p)
                min1 = 3.0 * x_mid * q - abs(x_acc1 * q)
                min2 = abs(e * q)

                if 2.0 * p < min(min1, min2):
                    e = d
                    d = p / q
                else:
                    d = x_mid
                    e = d
            else:
                d = x_mid
                e = d

            x_min = root
            fx_min = froot

            if abs(d) > x_acc1:
                root += d
            else:
                root += abs(x_acc1) * math.copysign(1.0, x_mid)

            froot = f(root)
            eval_count += 1

        raise ConvergenceError(
            f"Brent solver: max evaluations ({self.max_evaluations}) exceeded"
        )
