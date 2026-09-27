"""Capture the documentation screenshots from a live run.

Textual writes SVG, which stays sharp at any width and renders inline on
GitHub. Run against a real account so the shots show real chains:

    python scripts/screenshots.py

These views show market data only, with no positions, balances or account
identifiers, so the output is safe to commit.

Pricing goes through the app's buying-power path, so a capture run sends order
dry-runs to the account (nothing is placed). Without a trading-scoped grant the
BPR column falls back to the formula estimate, marked with `~`.
"""

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from tau.tui.app import TauApp

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img"
# The screen loads ~190 symbols of metrics; a chain is a websocket round trip
# and a shortlist is many of them at once.
LOAD_WAIT = 25
CHAIN_WAIT = 20
# `w` is a headline fetch plus a model call, both off the event loop.
WHY_WAIT = 45
RANK_WAIT = 90


async def main() -> None:
    load_dotenv(ROOT / ".env")
    OUT.mkdir(parents=True, exist_ok=True)
    app = TauApp()

    def shot(name: str) -> None:
        app.save_screenshot(str(OUT / name))
        print(name)

    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(LOAD_WAIT)
        shot("screen.svg")

        # Detail pane with a priced chain.
        await pilot.press("c")
        await pilot.pause(CHAIN_WAIT)
        shot("detail.svg")

        # Price context plus the catalyst read, on the same name.
        await pilot.press("w")
        await pilot.pause(WHY_WAIT)
        shot("catalyst.svg")

        # Ranked proposals across the whole shortlist.
        await pilot.press("p")
        await pilot.pause(RANK_WAIT)
        shot("rank.svg")

        # One name's whole search, rejections included.
        await pilot.press("v")
        await pilot.pause(1)
        shot("variants.svg")
        await pilot.press("escape")
        await pilot.pause(1)

        # The strategy picker, over the rank list.
        await pilot.press("S")
        await pilot.pause(1)
        shot("picker.svg")
        await pilot.press("escape")
        await pilot.pause(1)

        # Exclusions, back on the screen.
        await pilot.press("escape")
        await pilot.pause(1)
        await pilot.press("x")
        await pilot.pause(1)
        shot("excluded.svg")


if __name__ == "__main__":
    asyncio.run(main())
