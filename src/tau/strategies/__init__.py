"""The strategy definitions tau searches, one per module.

They ship in-package so that what tau searches for is visible. Keep each
module to literal construction — a comprehension over literals for a delta
ladder is fine, a call into market data is not — so they read as definitions
and `asdict()` stays meaningful for the scan log.

Importing this module validates every shipped strategy, so a malformed
definition fails at import rather than mid-scan.
"""

from tau.strategies.broken_wing_butterfly import BROKEN_WING_BUTTERFLY
from tau.strategies.cash_secured_put import CASH_SECURED_PUT
from tau.strategies.defaults import MAX_SPREAD_COST, MIN_POP
from tau.strategies.iron_condor import IRON_CONDOR
from tau.strategies.jade_lizard import JADE_LIZARD
from tau.strategies.strangle import STRANGLE
from tau.strategies.vertical_put import VERTICAL_PUT
from tau.strategy import Strategy

ALL: tuple[Strategy, ...] = (
    STRANGLE,
    VERTICAL_PUT,
    IRON_CONDOR,
    JADE_LIZARD,
    BROKEN_WING_BUTTERFLY,
    CASH_SECURED_PUT,
)

STRATEGIES: dict[str, Strategy] = {s.name: s for s in ALL}

if len(STRATEGIES) != len(ALL):
    raise ValueError("duplicate strategy name among the shipped definitions")

__all__ = ["ALL", "MAX_SPREAD_COST", "MIN_POP", "STRATEGIES"]
