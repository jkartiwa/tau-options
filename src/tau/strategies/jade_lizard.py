"""Jade lizard — naked short put plus a call credit spread.

Its defining property is not the shape: it is that the total credit covers the
call spread's width, so there is no risk to the upside at all. That is a
pricing outcome, not a leg list. On a given day the requested deltas may
simply not produce it, and what got built is a lopsided condor — which is why
the constraint is on `worst_loss_up` rather than on the structure's name.
"""

from tau.payoff import OptionType, Side
from tau.strategy import Bias, Delta, LegSpec, Ref, Require, Strategy
from tau.strategies.defaults import MAX_SPREAD_COST

C, P = OptionType.CALL, OptionType.PUT
LONG, SHORT = Side.LONG, Side.SHORT

JADE_LIZARD = Strategy(
    name="jade-lizard",
    bias=Bias.BULLISH,
    legs=[
        LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.20, 0.30])),
        LegSpec("short_call", type=C, side=SHORT, strike=Delta([0.20, 0.25])),
        LegSpec("long_call", type=C, side=LONG, strike=Ref("short_call", offset=[5, 10, 15])),
    ],
    require=[
        # the property that actually makes it a lizard
        Require("worst_loss_up", "<=", 0),
        Require("spread_cost", "<=", MAX_SPREAD_COST),
    ],
)
