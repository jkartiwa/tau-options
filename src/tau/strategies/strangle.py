"""Short strangle — the structure tau shipped with, now expressed as data.

Two naked short wings, no references between them. This is the baseline every
other structure is compared against, so it searches a small delta ladder
rather than pinning 16 the way the hardcoded version did.
"""

from tau.payoff import OptionType, Side
from tau.strategy import Bias, Delta, LegSpec, Require, Strategy
from tau.strategies.defaults import MAX_SPREAD_COST, MIN_POP

C, P = OptionType.CALL, OptionType.PUT
SHORT = Side.SHORT

STRANGLE = Strategy(
    name="strangle",
    bias=Bias.NEUTRAL,
    legs=[
        LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.16, 0.20, 0.30])),
        LegSpec("short_call", type=C, side=SHORT, strike=Delta([0.16, 0.20, 0.30])),
    ],
    require=[
        Require("spread_cost", "<=", MAX_SPREAD_COST),
        Require("pop", ">=", MIN_POP),
    ],
)
