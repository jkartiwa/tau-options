"""Builders shared across the test modules.

The ladder is five strikes a side around a spot of 100, with deltas that fall
away symmetrically and mids that make each structure hand-checkable.
"""

from datetime import date

from tau.chain import Cycle, Leg
from tau.payoff import OptionType
from tau.screen import Candidate
from tau.strategy import Bias, Strategy

C, P = OptionType.CALL, OptionType.PUT

PUT_DELTAS = {80: -0.08, 85: -0.12, 90: -0.20, 95: -0.32, 100: -0.50}
CALL_DELTAS = {100: 0.50, 105: 0.30, 110: 0.20, 115: 0.12, 120: 0.08}
PUT_MIDS = {80: 0.50, 85: 0.80, 90: 1.20, 95: 2.00, 100: 3.50}
CALL_MIDS = {100: 3.50, 105: 2.00, 110: 1.20, 115: 0.80, 120: 0.50}
SPREAD = 0.02  # tight enough that the shipped spread_cost constraints pass


def cand(symbol="TEST", **kw) -> Candidate:
    fields = {
        "ivr": 50.0,
        "ivp": 50.0,
        "iv30": 30.0,
        "hv30": 25.0,
        "liquidity": 4,
        "beta": 1.0,
        "earnings_date": None,
    }
    return Candidate(symbol=symbol, **(fields | kw))


def leg(strike, option_type, delta, mid, spread=SPREAD, iv=0.30) -> Leg:
    return Leg(
        occ=f"{option_type}{strike:g}",
        streamer=f"s{option_type}{strike:g}",
        strike=float(strike),
        type=option_type,
        bid=mid - spread / 2,
        ask=mid + spread / 2,
        delta=delta,
        iv=iv,
    )


def ladder() -> tuple[Leg, ...]:
    legs = [leg(k, P, d, PUT_MIDS[k]) for k, d in PUT_DELTAS.items()]
    legs += [leg(k, C, d, CALL_MIDS[k]) for k, d in CALL_DELTAS.items()]
    return tuple(legs)


def cycle(legs=None, underlying=100.0, dte=45, symbol="TEST") -> Cycle:
    return Cycle(
        symbol=symbol,
        expiration=date(2026, 9, 18),
        dte=dte,
        underlying=underlying,
        legs=legs if legs is not None else ladder(),
    )


def strat(*legs, bias=Bias.NEUTRAL, **kwargs) -> Strategy:
    """A test strategy: the legs positionally, neutral unless told otherwise."""
    return Strategy(name="s", bias=bias, legs=list(legs), **kwargs)
