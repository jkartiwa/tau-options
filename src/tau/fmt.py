"""Number formatting shared by the CLI tables and the TUI tables.

Both surfaces print the same figures in the same columns, so the formatting
rules are one thing, not two. `bpr` is the one that must never drift: the
whole source-labelling contract is a trailing tilde on a formula estimate, and
a second copy of that rule is a second place for it to be wrong.

The detail pane formats for prose rather than columns (its own dash and its
own `%` suffix), so it keeps its own helpers.
"""


def fmt(value, spec: str = ".1f") -> str:
    if value is None:
        return "—"
    if value in (float("inf"), float("-inf")):
        return "∞" if value > 0 else "-∞"
    return format(value, spec)


def pct(value, spec: str = ".0f") -> str:
    """A rate stored as a fraction, shown as a percentage. The column headers
    already carry the % sign, so this doesn't repeat it."""
    return "—" if value is None else fmt(value * 100, spec)


def bpr(value, source: str) -> str:
    """Buying power with the source readable per row: broker figures plain,
    formula estimates carrying the tilde the column header used to. The header
    is `BPR` for everyone, so the row itself has to say which margin model the
    number came from — a bare number next to that header would silently be
    either, and this column is exactly the one place a guess is not
    acceptable."""
    if value is None:
        return "—"
    return fmt(value, ",.0f") + ("" if source == "broker" else "~")
