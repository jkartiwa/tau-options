"""Put credit spread — defined risk, bullish-to-neutral.

The long wing is placed by reference rather than by its own delta: the width
is the trade, and a delta-picked wing would drift to a different width on
every name.
"""

from tau.payoff import OptionType, Side
from tau.strategy import Bias, Delta, LegSpec, Ref, Require, Strategy
from tau.strategies.defaults import MAX_SPREAD_COST

P = OptionType.PUT
LONG, SHORT = Side.LONG, Side.SHORT

VERTICAL_PUT = Strategy(
    name="vertical-put",
    bias=Bias.BULLISH,
    legs=[
        LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.20, 0.30])),
        LegSpec("long_put", type=P, side=LONG, strike=Ref("short_put", offset=[-5, -10])),
    ],
    require=[Require("spread_cost", "<=", MAX_SPREAD_COST)],
)
