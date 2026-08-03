"""Short put, one leg — the simplest premium sale.

Named for how it is usually traded, but the buying-power figure the engine
returns is the naked-margin estimate, which is what a margin account is
actually charged. An account genuinely securing the position with cash ties up
strike x 100 instead, so read the return on capital accordingly.
"""

from tau.payoff import OptionType, Side
from tau.strategy import Bias, Delta, LegSpec, Require, Strategy
from tau.strategies.defaults import MAX_SPREAD_COST

P = OptionType.PUT
SHORT = Side.SHORT

CASH_SECURED_PUT = Strategy(
    name="cash-secured-put",
    bias=Bias.BULLISH,
    legs=[
        LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.16, 0.20, 0.30])),
    ],
    require=[Require("spread_cost", "<=", MAX_SPREAD_COST)],
)
