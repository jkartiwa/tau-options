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


def _why_app(history=None, brief=None, calls=None):
    from datetime import UTC as _UTC

    from tau.catalyst import Brief
    from tau.history import Bar, History

    calls = calls if calls is not None else []
    history = history or History(
        symbol="HIGH",
        bars=tuple(
            Bar(day=TODAY - timedelta(days=i), open=100.0, high=120.0, low=80.0, close=100.0)
            for i in range(60, 0, -1)
        ),
        fetched_at=datetime.now(_UTC),
    )
    brief = brief or Brief(
        symbol="HIGH",
        classification="resolved",
        catalyst="Q2 earnings reported",
        key_dates=(),
        confidence="high",
        note="Event passed; IV should bleed.",
        headlines=(),
        fetched_at=datetime.now(_UTC),
    )

    async def history_loader(candidate):
        calls.append(("history", candidate.symbol))
        return history

    async def brief_loader(candidate):
        calls.append(("brief", candidate.symbol))
        return brief

    async def loader():
        return list(FIXTURE)

    return TauApp(
        loader=loader, history_loader=history_loader, brief_loader=brief_loader
    ), calls


async def _settle(a, predicate, tries=60):
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_why_loads_price_context_and_catalyst_on_w():
    a, calls = _why_app()
    async with a.run_test() as pilot:
        await pilot.pause()
        assert not calls  # cursor movement alone costs nothing
        await pilot.press("w")
        assert await _settle(a, lambda: "HIGH" in a._briefs)
        assert set(calls) == {("history", "HIGH"), ("brief", "HIGH")}
        rendered = str(a.query_one("#detail").content)
        assert "why vol is bid" in rendered and "resolved" in rendered
        assert "52w" in rendered


@pytest.mark.asyncio
async def test_why_is_cached_per_symbol():
    a, calls = _why_app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        assert await _settle(a, lambda: "HIGH" in a._briefs)
        await pilot.press("w")
        await pilot.pause()
        assert len(calls) == 2  # second press served from cache


@pytest.mark.asyncio
async def test_why_does_not_cancel_an_in_flight_chain_load():
    """Both are exclusive workers; sharing the default group would make one
    keypress silently kill the other's fetch."""
    from tau.chain import Cycle, Leg

    started = asyncio.Event()
    chain_calls = []

    async def slow_chain_loader(candidate):
        started.set()
        await asyncio.sleep(0.3)
        chain_calls.append(candidate.symbol)
        return Cycle(
            symbol=candidate.symbol,
            expiration=date(2026, 9, 4),
            dte=40,
            underlying=100.0,
            legs=(
                Leg("P85", "s1", 85.0, "P", bid=1.0, ask=1.2, delta=-0.16, iv=0.3),
                Leg("C115", "s2", 115.0, "C", bid=0.8, ask=1.0, delta=0.17, iv=0.3),
            ),
            fetched_at=datetime.now(UTC),
        )

    a, _ = _why_app()
    a._chain_loader = slow_chain_loader
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        await asyncio.wait_for(started.wait(), timeout=2)
        await pilot.press("w")  # must not cancel the chain worker
        assert await _settle(a, lambda: chain_calls == ["HIGH"])
        assert "HIGH" in a._cycles


@pytest.mark.asyncio
async def test_why_reports_failure_instead_of_rendering_a_blank():
    async def boom(candidate):
        raise RuntimeError("no news")

    a, _ = _why_app()
    a._brief_loader = boom
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        assert await _settle(a, lambda: "catalyst failed" in a._why_status)
        assert "HIGH" in a._history  # the price half still landed


def _proposal_loader_factory(proposals_by_symbol):
    """Fake loader that resolves synchronously via on_done, per candidate."""

    async def loader(candidates, on_done):
        for c in candidates:
            on_done(proposals_by_symbol[c.symbol])

    return loader


def _fake_proposal(symbol, dte=40, credit=5.0, bpr=2000.0, pop=0.75, spread_cost=0.10):
    from tau.chain import Cycle, Leg, Strangle

    put = Leg(f"P{symbol}", f"sP{symbol}", 90.0, "P", bid=credit / 2 - 0.05, ask=credit / 2 + 0.05, delta=-0.16, iv=0.3)
    call = Leg(f"C{symbol}", f"sC{symbol}", 110.0, "C", bid=credit / 2 - 0.05, ask=credit / 2 + 0.05, delta=0.16, iv=0.3)
    cy = Cycle(symbol=symbol, expiration=date(2026, 9, 4), dte=dte, underlying=100.0,
               legs=(put, call), fetched_at=datetime.now(UTC))
    st = Strangle(put, call, target_delta=0.16)

    class FakeProposal:
        candidate = None
        cycle = cy
        strangle = st
        error = None

        @property
        def ok(self):
            return True

        @property
        def symbol(self):
            return symbol

        @property
        def credit(self):
            return round(put.mid + call.mid, 2)

        @property
        def bpr(self):
            return bpr

        @property
        def roc(self):
            return (self.credit * 100) / bpr

        @property
        def annualized_roc(self):
            return self.roc * 365 / dte

        @property
        def pop(self):
            return pop

        @property
        def spread_cost(self):
            return spread_cost

        @property
        def be_over_em(self):
            return 1.0

    return FakeProposal()


@pytest.mark.asyncio
async def test_rank_view_prices_the_passing_shortlist():
    async def loader():
        return list(FIXTURE)  # HIGH, ERN(excluded by earnings), CHEAP pass by default

    proposals = {
        "HIGH": _fake_proposal("HIGH", credit=10.0, bpr=5000.0),  # ROC 20%
        "CHEAP": _fake_proposal("CHEAP", credit=1.0, bpr=200.0),  # ROC 50%, richer
        "MID": _fake_proposal("MID", credit=1.0, bpr=500.0),
    }
    a = TauApp(loader=loader, proposal_loader=_proposal_loader_factory(proposals))
    async with a.run_test() as pilot:
        await pilot.pause()
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]  # screen order, default IVR sort
        await pilot.press("p")
        await pilot.pause()
        assert a.mode == "rank"
        # CHEAP's credit/BPR beats HIGH's, so ANN% rank reorders them.
        ranked = [c.symbol for c in a._rank_rows]
        assert ranked.index("CHEAP") < ranked.index("HIGH")


@pytest.mark.asyncio
async def test_rank_view_reuses_cached_proposals_on_reentry():
    async def loader():
        return [FIXTURE[0]]  # just HIGH

    calls = []

    async def track_loader(candidates, on_done):
        calls.append([c.symbol for c in candidates])
        on_done(_fake_proposal("HIGH"))

    a = TauApp(loader=loader, proposal_loader=track_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.press("p")
        await pilot.pause()
        assert calls == [["HIGH"]]  # second entry served from cache, no refetch


@pytest.mark.asyncio
async def test_reprice_forces_a_refetch():
    async def loader():
        return [FIXTURE[0]]

    calls = []

    async def track_loader(candidates, on_done):
        calls.append([c.symbol for c in candidates])
        on_done(_fake_proposal("HIGH"))

    a = TauApp(loader=loader, proposal_loader=track_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert calls == [["HIGH"], ["HIGH"]]


@pytest.mark.asyncio
async def test_escape_returns_to_screen_view():
    async def loader():
        return list(FIXTURE)

    a = TauApp(loader=loader, proposal_loader=_proposal_loader_factory(
        {"HIGH": _fake_proposal("HIGH"), "CHEAP": _fake_proposal("CHEAP"), "MID": _fake_proposal("MID")}
    ))
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert a.mode == "rank"
        await pilot.press("escape")
        assert a.mode == "screen"
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]
