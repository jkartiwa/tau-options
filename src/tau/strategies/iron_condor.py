"""Iron condor — defined risk both ways, neutral.

The structure the payoff engine exists for: an engine that summed two
verticals would charge twice the width in margin and report roughly half the
true return on capital. Only one side can lose, and the engine derives that
from the payoff rather than from the name.
"""

from tau.payoff import OptionType, Side
from tau.strategy import Bias, Delta, LegSpec, Ref, Require, Strategy
from tau.strategies.defaults import MAX_SPREAD_COST

C, P = OptionType.CALL, OptionType.PUT
LONG, SHORT = Side.LONG, Side.SHORT

IRON_CONDOR = Strategy(
    name="iron-condor",
    bias=Bias.NEUTRAL,
    legs=[
        LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.16, 0.20])),
        LegSpec("long_put", type=P, side=LONG, strike=Ref("short_put", offset=[-5, -10])),
        LegSpec("short_call", type=C, side=SHORT, strike=Delta([0.16, 0.20])),
        LegSpec("long_call", type=C, side=LONG, strike=Ref("short_call", offset=[5, 10])),
    ],
    require=[Require("spread_cost", "<=", MAX_SPREAD_COST)],
)
