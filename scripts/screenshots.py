"""Capture README screenshots from a live run.

Textual writes SVG, which stays sharp at any width and renders inline on
GitHub. Run against a real account so the shots show real chains:

    python scripts/screenshots.py

Only market data appears in these views — no positions, balances, or account
identifiers — so the output is safe to commit.
"""

import asyncio
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from tau.tui.app import TauApp  # noqa: E402  (needs the env loaded first)

OUT = Path(__file__).resolve().parents[1] / "docs" / "img"
# The screen loads ~170 symbols of metrics; a chain is a websocket round trip
# and a shortlist is many of them at once.
LOAD_WAIT = 25
CHAIN_WAIT = 20
# `w` is a headline fetch plus a model call, both off the event loop.
WHY_WAIT = 45
RANK_WAIT = 90


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = TauApp()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(LOAD_WAIT)
        app.save_screenshot(str(OUT / "screen.svg"))
        print("screen.svg")

        # Detail pane with a priced chain.
        await pilot.press("c")
        await pilot.pause(CHAIN_WAIT)
        app.save_screenshot(str(OUT / "detail.svg"))
        print("detail.svg")

        # Price context plus the catalyst read, on the same name.
        await pilot.press("w")
        await pilot.pause(WHY_WAIT)
        app.save_screenshot(str(OUT / "catalyst.svg"))
        print("catalyst.svg")

        # Ranked proposals across the whole shortlist.
        await pilot.press("p")
        await pilot.pause(RANK_WAIT)
        app.save_screenshot(str(OUT / "rank.svg"))
        print("rank.svg")

        # Exclusions, back on the screen.
        await pilot.press("escape")
        await pilot.pause(1)
        await pilot.press("x")
        await pilot.pause(1)
        app.save_screenshot(str(OUT / "excluded.svg"))
        print("excluded.svg")


if __name__ == "__main__":
    asyncio.run(main())
