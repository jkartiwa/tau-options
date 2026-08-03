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
