"""Universe loading. The default universe ships as package data
(`data/universe.txt`, one symbol per line, `#` comments); TAU_UNIVERSE or
--universe points at a custom file."""

from importlib.resources import files
from pathlib import Path


def load_universe(path: str | None = None) -> list[str]:
    if path:
        text = Path(path).read_text()
    else:
        text = files("tau").joinpath("data/universe.txt").read_text()
    symbols: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        sym = line.split("#", 1)[0].strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols
