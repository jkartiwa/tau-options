"""Shared constants for the strategy definitions.

Separate from `__init__` so a definition module can import it without the
package importing itself.
"""

# A four-legger crosses four markets. Ranked on return alone, condors and
# butterflies would dominate the list on fills that never happen, so every
# strategy caps the cost of crossing every leg as a share of the premium.
MAX_SPREAD_COST = 0.25

# A higher-delta variant always carries more credit and a higher
# annualized_roc at a lower probability of profit, so return alone would rank
# the riskiest variant first. Every strategy carries this pop floor;
# `--min-pop` overrides it (see `strategy.with_min_pop`).
MIN_POP = 0.68
