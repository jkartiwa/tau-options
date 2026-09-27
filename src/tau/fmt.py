"""Number formatting shared by the CLI tables and the TUI tables.

Both surfaces print the same figures in the same columns, so the rules live
in one place. The detail pane formats for prose rather than columns, so it
keeps its own helpers.
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
    """Buying power with its source marked per row: broker figures plain,
    formula estimates with a trailing tilde. The shared `BPR` header cannot
    say which margin model a row came from, so the row has to."""
    if value is None:
        return "—"
    return fmt(value, ",.0f") + ("" if source == "broker" else "~")
