"""Broken wing put butterfly — an unbalanced fly, bullish-to-neutral.

Two short bodies with an unequal wing on either side. The widths *are* the
trade and which pair pays depends on today's skew, so both are searched
rather than pinned.

It is why every structure's return is `max_profit / bpr` rather than
`credit / bpr`: here max profit sits at the body strike, well above the credit
taken in, and the structure can legitimately price as a debit.
"""

from tau.payoff import OptionType, Side
from tau.strategies.defaults import MAX_SPREAD_COST, MIN_POP
from tau.strategy import Bias, Delta, LegSpec, Ref, Require, Strategy

P = OptionType.PUT
LONG, SHORT = Side.LONG, Side.SHORT

BROKEN_WING_BUTTERFLY = Strategy(
    name="broken-wing-butterfly",
    bias=Bias.BULLISH,
    legs=[
        LegSpec("body", type=P, side=SHORT, strike=Delta([0.25, 0.30]), qty=2),
        # The far wing is always wider than the near one, on every combination.
        # Overlapping ladders would let the search build a balanced fly, whose
        # tiny buying-power figure tops the ranking on a low chance of profit.
        LegSpec("near_wing", type=P, side=LONG, strike=Ref("body", offset=[5, 10])),
        LegSpec("far_wing", type=P, side=LONG, strike=Ref("body", offset=[-15, -20, -25])),
    ],
    require=[
        Require("spread_cost", "<=", MAX_SPREAD_COST),
        Require("pop", ">=", MIN_POP),
    ],
)
