"""Shared constants for the strategy definitions.

Separate from `__init__` so a definition module can import it without the
package importing itself.
"""

# A four-legger crosses four markets. Ranked on return alone, condors and
# butterflies would dominate the list on fills that never happen — the same
# failure already caught live, where a two-legged AAPL strangle cost ~99% of
# its credit to cross. Every strategy ships a spread_cost constraint for that
# reason; this is the shared default.
MAX_SPREAD_COST = 0.25

# annualized_roc alone cannot see how it was bought: every structure scans
# several deltas per leg, so a wider/higher-delta variant always carries more
# credit and a higher annualized_roc at a lower probability of profit. This is
# a premium-selling scanner, not a lottery-ticket one, so every strategy ships
# a pop floor too. 0.50 already anchored the broken wing butterfly's own
# constraint (a structure more likely to lose than win is not the trade); this
# makes that floor the shared, CLI-overridable default (`--min-pop`, see
# `strategy.with_min_pop` and `cli.py`).
MIN_POP = 0.50
