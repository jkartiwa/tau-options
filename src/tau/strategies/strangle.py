"""Short strangle — two naked short wings, neutral.

Undefined risk on both sides. No references between the legs; each wing
searches its own small delta ladder.
"""

from tau.payoff import OptionType, Side
from tau.strategies.defaults import MAX_SPREAD_COST, MIN_POP
from tau.strategy import Bias, Delta, LegSpec, Require, Strategy

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
