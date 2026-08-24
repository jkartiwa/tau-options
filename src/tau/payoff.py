"""Payoff arithmetic for an arbitrary option structure.

Composition is the right model for legs and the wrong model for math. An iron
condor is "two verticals" in its leg list and in nothing else: margin does not
compose (two verticals charged separately is 2x width, a condor is charged
1x because only one side can lose), probabilities do not compose (it is the
same underlying variable in both), and a jade lizard's defining property —
no upside risk — is a pricing outcome rather than a shape, so a structure
built from the right legs may not have it on any given day.

So the universal representation is the payoff function, not the taxonomy.
Given a signed leg list and quoted prices, every metric worth ranking on is
derivable here, once, generically.

Everything in this module is pure: no I/O, no SDK types, no chain types. The
payoff is piecewise-linear with kinks exactly at the strikes, which is what
lets breakevens, extrema and profitable regions be solved analytically rather
than sampled.

Buying power stays an *estimate* from the standard naked-margin formula, not
a broker quote — the read-only grant cannot dry-run an order. It is the
shakiest piece of the generalization and must be checked against tastytrade's
own figure the first time each new structure family is traded.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from math import erf, inf, log, sqrt

CONTRACT_MULTIPLIER = 100
DAYS_PER_YEAR = 365.0

# Naked equity option margin, the standard broker formula. The requirement is
# the greatest of these, per side, per contract.
OTM_PERCENT = 0.20
STRIKE_PERCENT = 0.10
MIN_PER_CONTRACT = 50.0


class OptionType(StrEnum):
    """Values match OCC symbols and the strings chain.Leg.right already
    carries, so `leg.right == OptionType.CALL` is true without a conversion."""

    CALL = "C"
    PUT = "P"


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 long, -1 short — the one thing the payoff engine needs from a
        side. It lives on the enum so there is no parallel lookup table to
        drift out of sync."""
        return 1 if self is Side.LONG else -1


@dataclass(frozen=True)
class PayoffLeg:
    """One priced leg of a structure, reduced to what the math needs."""

    type: OptionType
    side: Side
    strike: float
    mid: float
    qty: int = 1

    @property
    def signed_qty(self) -> int:
        return self.side.sign * self.qty


def net_premium(legs: tuple[PayoffLeg, ...]) -> float:
    """Per share, positive = credit taken in, negative = debit paid."""
    return sum(-leg.side.sign * leg.qty * leg.mid for leg in legs)


def _intrinsic(leg: PayoffLeg, spot: float) -> float:
    if leg.type is OptionType.CALL:
        return max(0.0, spot - leg.strike)
    return max(0.0, leg.strike - spot)


def payoff_at(legs: tuple[PayoffLeg, ...], spot: float) -> float:
    """Profit or loss in dollars for one structure held to expiry, if the
    underlying settles at `spot`."""
    intrinsic = sum(leg.signed_qty * _intrinsic(leg, spot) for leg in legs)
    return CONTRACT_MULTIPLIER * (net_premium(legs) + intrinsic)


def strikes(legs: tuple[PayoffLeg, ...]) -> list[float]:
    return sorted({leg.strike for leg in legs})


def slope_right(legs: tuple[PayoffLeg, ...]) -> float:
    """dP/dS above the highest strike, in dollars of payoff per dollar of
    underlying. Negative means open upside risk."""
    calls = [leg for leg in legs if leg.type is OptionType.CALL]
    return CONTRACT_MULTIPLIER * sum(leg.signed_qty for leg in calls)


def slope_left(legs: tuple[PayoffLeg, ...]) -> float:
    """dP/dS below the lowest strike. Positive means the structure loses as
    the underlying falls — open downside, bounded at S=0 in reality but
    charged as open by every broker."""
    puts = [leg for leg in legs if leg.type is OptionType.PUT]
    return CONTRACT_MULTIPLIER * sum(-leg.signed_qty for leg in puts)


def _kinks(legs: tuple[PayoffLeg, ...]) -> list[float]:
    """Every point where the payoff can change slope, plus S=0. The underlying
    cannot go below zero, so the left end is a real evaluation point rather
    than a limit."""
    return [0.0] + strikes(legs)


def breakevens(legs: tuple[PayoffLeg, ...]) -> list[float]:
    """Every underlying price where the structure breaks even at expiry.
    Handles one, two or four identically — the payoff is piecewise-linear, so
    each sign change between consecutive kinks has exactly one root."""
    points = _kinks(legs)
    if len(points) < 2:
        return []
    found: list[float] = []
    for a, b in zip(points, points[1:]):
        pa, pb = payoff_at(legs, a), payoff_at(legs, b)
        if pa == 0.0 and a > 0.0:
            found.append(a)
        if (pa < 0.0 < pb) or (pb < 0.0 < pa):
            found.append(a + (b - a) * (-pa) / (pb - pa))
    last = points[-1]
    p_last = payoff_at(legs, last)
    if p_last == 0.0:
        found.append(last)
    slope = slope_right(legs)
    if slope != 0.0:
        root = last - p_last / slope
        if root > last:
            found.append(root)
    return sorted({round(value, 10) for value in found})


def max_profit(legs: tuple[PayoffLeg, ...]) -> float:
    """Best case in dollars; `inf` when the upside is open. The downside is
    bounded at S=0 and that point is already evaluated."""
    if slope_right(legs) > 0:
        return inf
    return max(payoff_at(legs, point) for point in _kinks(legs))


def max_loss(legs: tuple[PayoffLeg, ...]) -> float:
    """Worst case in dollars, negative by convention; `-inf` when the upside
    is open."""
    if slope_right(legs) < 0:
        return -inf
    return min(payoff_at(legs, point) for point in _kinks(legs))


def worst_loss_up(legs: tuple[PayoffLeg, ...], spot: float) -> float:
    """Worst loss at or above spot, as a positive magnitude; 0.0 when there is
    no loss up there and `inf` when the tail is open.

    This is what makes a jade lizard's defining property expressible as a
    plain scalar comparison (`worst_loss_up <= 0`) with no expression
    language in the constraint vocabulary.
    """
    if slope_right(legs) < 0:
        return inf
    points = [spot] + [k for k in strikes(legs) if k > spot]
    return max(0.0, -min(payoff_at(legs, point) for point in points))


def worst_loss_down(legs: tuple[PayoffLeg, ...], spot: float) -> float:
    """Worst loss at or below spot, as a positive magnitude.

    Unlike `worst_loss_up` this is never `inf`: the underlying cannot go below
    zero, so a naked short put's worst case is a real, finite, large number.
    The asymmetry is deliberate — reporting `inf` here would hide the one
    figure worth looking at. Margin treats the tail as open regardless, which
    `bpr` handles separately.
    """
    points = [0.0] + [k for k in strikes(legs) if k < spot] + [spot]
    return max(0.0, -min(payoff_at(legs, point) for point in points))


def naked_side_requirement(
    spot: float, strike: float, premium: float, option_type: OptionType
) -> float:
    """Margin for one naked short option, per contract, in dollars.

    Which way is out of the money depends on the side: a short put is OTM
    below spot, a short call above it. An in-the-money short has no OTM
    distance to hand back, so the 20 %-of-underlying term is charged in full
    — taking `abs(spot - strike)` there would discount the leg that is
    actually at risk.
    """
    if option_type is OptionType.PUT:
        otm = max(0.0, spot - strike)
    else:
        otm = max(0.0, strike - spot)
    a = (OTM_PERCENT * spot - otm + premium) * CONTRACT_MULTIPLIER
    b = (STRIKE_PERCENT * strike + premium) * CONTRACT_MULTIPLIER
    return max(a, b, MIN_PER_CONTRACT)


def _side_premium(legs: tuple[PayoffLeg, ...], option_type: OptionType) -> float:
    """Net premium collected on one side of the structure, in dollars."""
    side = tuple(leg for leg in legs if leg.type is option_type)
    return net_premium(side) * CONTRACT_MULTIPLIER


def _open_side_requirement(
    legs: tuple[PayoffLeg, ...], option_type: OptionType, spot: float, net_short: float
) -> float:
    """Naked margin on a side whose tail is open.

    The short leg with the largest requirement sets the rate, times the net
    short quantity. That is exact for one net-short contract, which is every
    structure shipped today; a ratio spread with two net-short contracts gets
    a conservative estimate rather than an exact figure.
    """
    shorts = [
        leg for leg in legs if leg.type is option_type and leg.side is Side.SHORT
    ]
    if not shorts or net_short <= 0:
        return 0.0
    per = max(
        naked_side_requirement(spot, leg.strike, leg.mid, option_type)
        for leg in shorts
    )
    return per * net_short


def bpr(legs: tuple[PayoffLeg, ...], spot: float) -> float | None:
    """Estimated buying power reduction in dollars.

    Defined risk on both tails is charged as the max loss. Otherwise the open
    side is charged naked margin, the closed side its own worst loss, and only
    the larger of the two is charged — one side cannot lose while the other
    does — plus the premium collected on the side that went uncharged.

    Reduces correctly: a short strangle to the larger naked side plus the
    other side's credit, an iron condor to width minus credit, a jade lizard
    to the naked put.
    """
    if not legs or spot <= 0:
        return None
    open_up = slope_right(legs) < 0
    open_down = slope_left(legs) > 0
    if not open_up and not open_down:
        return max(0.0, -max_loss(legs))

    if open_up:
        req_up = _open_side_requirement(
            legs, OptionType.CALL, spot, -slope_right(legs) / CONTRACT_MULTIPLIER
        )
    else:
        req_up = worst_loss_up(legs, spot)
    if open_down:
        req_down = _open_side_requirement(
            legs, OptionType.PUT, spot, slope_left(legs) / CONTRACT_MULTIPLIER
        )
    else:
        req_down = worst_loss_down(legs, spot)

    if req_up >= req_down:
        return req_up + max(0.0, _side_premium(legs, OptionType.PUT))
    return req_down + max(0.0, _side_premium(legs, OptionType.CALL))


def profitable_intervals(legs: tuple[PayoffLeg, ...]) -> list[tuple[float, float]]:
    """The underlying ranges where the structure finishes profitable, bounded
    by the breakevens and by 0 / infinity at the ends."""
    bounds = [0.0] + breakevens(legs) + [inf]
    ks = strikes(legs)
    out: list[tuple[float, float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if b == inf:
            probe = (a + max(1.0, a * 0.5)) if a > 0 else (ks[len(ks) // 2] if ks else 1.0)
        else:
            probe = (a + b) / 2
        if payoff_at(legs, probe) > 0:
            out.append((a, b))
    return out


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _lognormal_cdf(bound: float, spot: float, sigma: float, drift: float) -> float:
    if bound <= 0:
        return 0.0
    if bound == inf:
        return 1.0
    return _norm_cdf((log(bound / spot) - drift) / sigma)


def _boundary_cdf(
    bound: float,
    spot: float,
    iv: float,
    dte: int,
    option_type: OptionType,
    iv_at: Callable[[float, OptionType], float | None] | None,
) -> float:
    """Lognormal CDF at one interval boundary, priced under the vol local to
    that boundary when `iv_at` can supply one and under `iv` when it cannot."""
    if bound <= 0:
        return 0.0
    if bound == inf:
        return 1.0
    local = iv_at(bound, option_type) if iv_at is not None else None
    vol = local if local is not None and local > 0 else iv
    sigma = vol * sqrt(dte / DAYS_PER_YEAR)
    # Driftless in log terms means a -sigma^2/2 median shift.
    return _lognormal_cdf(bound, spot, sigma, -0.5 * sigma * sigma)


def pop_over_intervals(
    intervals: list[tuple[float, float]],
    spot: float,
    iv: float,
    dte: int,
    iv_at: Callable[[float, OptionType], float | None] | None = None,
) -> float | None:
    """Probability the underlying finishes inside any profitable interval,
    under a driftless lognormal.

    Deliberately not the 1 - delta shortcut: delta measures finishing beyond
    the *strikes*, while the trade is profitable out to the breakevens, which
    the credit pushes further out. The shortcut understates every proposal's
    odds.

    With no `iv_at`, one vol prices the whole distribution and the smile is
    thrown away. Pass `iv_at(price, option_type) -> vol | None` and each
    boundary is instead priced under the vol local to it: the put side for a
    lower boundary, the call side for an upper one. On an equity where puts
    are bid over calls that lowers the estimate, which is the point — a single
    ATM vol systematically overstates POP on skewed names.

    Be clear about what that is. Reading a lower boundary off one lognormal
    and the upper boundary off another is a practitioner approximation, not a
    distribution: the two CDFs subtracted here do not belong to the same
    random variable, and nothing constrains the result to be monotone in the
    skew (a call side quoted far under ATM can push the number back *up*). It
    is strictly better than ATM-for-everything and it is what a desk would
    do; it is not a correct POP. The rigorous version recovers the
    risk-neutral density from the whole smile (Breeden-Litzenberger across the
    chain) and is not implemented here.

    A boundary whose own vol is missing falls back to `iv` rather than
    invalidating the proposal — a partially skewed estimate beats none.
    """
    if spot <= 0 or iv <= 0 or dte <= 0:
        return None
    total = 0.0
    for lower, upper in intervals:
        total += _boundary_cdf(
            upper, spot, iv, dte, OptionType.CALL, iv_at
        ) - _boundary_cdf(lower, spot, iv, dte, OptionType.PUT, iv_at)
    return min(1.0, max(0.0, total))


def pop_between(
    spot: float, lower: float, upper: float, iv: float, dte: int
) -> float | None:
    """Single-interval form, kept for the two-breakeven case. Single-vol
    only — the skew-aware path is `pop_over_intervals(..., iv_at=...)`."""
    if spot <= 0 or lower <= 0 or upper <= lower or iv <= 0 or dte <= 0:
        return None
    return pop_over_intervals([(lower, upper)], spot, iv, dte)
