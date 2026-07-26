import asyncio
from datetime import UTC, datetime
from datetime import date, timedelta

import pytest

from tau.screen import Candidate
from tau.tui.app import TauApp

TODAY = date.today()


def cand(symbol, ivr, liq=4, iv30=30.0, hv30=25.0, earnings=None):
    return Candidate(
        symbol=symbol,
        ivr=ivr,
        ivp=50.0,
        iv30=iv30,
        hv30=hv30,
        liquidity=liq,
        beta=1.0,
        earnings_date=earnings,
    )


FIXTURE = [
    cand("HIGH", 90.0),
    cand("MID", 45.0),
    cand("LOW", 10.0),
    cand("ILLIQ", 80.0, liq=1),
    cand("ERN", 70.0, earnings=TODAY + timedelta(days=10)),
    cand("CHEAP", 60.0, iv30=20.0, hv30=40.0),
]


def app() -> TauApp:
    async def loader():
        return list(FIXTURE)

    return TauApp(loader=loader)


def symbols(a: TauApp) -> list[str]:
    return [c.symbol for c in a._rows]


@pytest.mark.asyncio
async def test_default_filters_and_rank():
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        # LOW fails IVR, ILLIQ fails liquidity, ERN reports in 10d.
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]


@pytest.mark.asyncio
async def test_raising_ivr_refilters_without_refetch():
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        calls = []
        a._loader = lambda: calls.append(1)  # would blow up if awaited
        await pilot.press(*["]"] * 5)  # 30 -> 55
        assert a.min_ivr == 55.0
        assert symbols(a) == ["HIGH", "CHEAP"]  # MID at 45 drops out
        assert not calls


@pytest.mark.asyncio
async def test_sort_by_iv_hv_puts_cheap_vol_last():
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")  # IVR -> IV/HV
        assert symbols(a)[-1] == "CHEAP"  # iv30 < hv30, selling below realized


@pytest.mark.asyncio
async def test_excluded_view_shows_reasons():
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")
        assert set(symbols(a)) == {c.symbol for c in FIXTURE}
        illiq = next(c for c in a._rows if c.symbol == "ILLIQ")
        assert "liquidity 1 < 3" in illiq.excluded


@pytest.mark.asyncio
async def test_earnings_cycle_to_zero_admits_earnings_name():
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        assert "ERN" not in symbols(a)  # reports in 10d, inside the 45d window
        await pilot.press("e")  # 45 -> 60, still inside
        assert "ERN" not in symbols(a)
        await pilot.press("e")  # 60 -> 0, filter disabled
        assert a.earnings_days == 0
        assert "ERN" in symbols(a)


@pytest.mark.asyncio
async def test_star_toggles():
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("space")
        assert a._starred == {"HIGH"}
        await pilot.press("space")
        assert a._starred == set()


@pytest.mark.asyncio
async def test_detail_pane_renders_and_chain_loads_on_enter():
    """Guards the Textual base-class collision that silently deadlocked the
    app: mounting a widget whose helper shadowed MessagePump._context stopped
    message dispatch entirely."""
    from datetime import date as _date

    from tau.chain import Cycle, Leg

    cycle = Cycle(
        symbol="HIGH",
        expiration=_date(2026, 9, 4),
        dte=40,
        underlying=100.0,
        legs=(
            Leg("P85", "s1", 85.0, "P", bid=1.0, ask=1.2, delta=-0.16, iv=0.30),
            Leg("C115", "s2", 115.0, "C", bid=0.8, ask=1.0, delta=0.17, iv=0.30),
        ),
        fetched_at=datetime.now(UTC),
    )

    calls = []

    async def chain_loader(candidate):
        calls.append(candidate.symbol)
        return cycle

    async def loader():
        return list(FIXTURE)

    a = TauApp(loader=loader, chain_loader=chain_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        pane = a.query_one("#detail")
        assert "HIGH" in str(pane.content)
        assert not calls  # cursor movement alone never pulls a chain
        await pilot.press("enter")
        await pilot.pause()
        for _ in range(50):
            if "HIGH" in calls:
                break
            await asyncio.sleep(0.05)
        assert calls == ["HIGH"]
        assert a._cycles["HIGH"] is cycle
        rendered = str(a.query_one("#detail").content)
        assert "strangle" in rendered and "credit" in rendered
