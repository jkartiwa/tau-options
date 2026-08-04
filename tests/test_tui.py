import asyncio
from datetime import UTC, datetime
from datetime import date, timedelta

import pytest

from tau.payoff import OptionType
from tau.screen import Candidate
from tau.tui.app import TauApp

C, P = OptionType.CALL, OptionType.PUT

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


def _row_text(a: TauApp, index: int) -> str:
    from textual.widgets import DataTable

    table = a.query_one("#table", DataTable)
    return " ".join(str(cell) for cell in table.get_row_at(index))


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
            Leg("P85", "s1", 85.0, P, bid=1.0, ask=1.2, delta=-0.16, iv=0.30),
            Leg("C115", "s2", 115.0, C, bid=0.8, ask=1.0, delta=0.17, iv=0.30),
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
        assert a._proposals["HIGH"].cycle is cycle
        rendered = str(a.query_one("#detail").content)
        assert "strangle" in rendered and "credit" in rendered
        assert "variants passed" in rendered


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
                Leg("P85", "s1", 85.0, P, bid=1.0, ask=1.2, delta=-0.16, iv=0.3),
                Leg("C115", "s2", 115.0, C, bid=0.8, ask=1.0, delta=0.17, iv=0.3),
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
        assert "HIGH" in a._proposals


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


PUT_DELTAS = {80: -0.08, 85: -0.12, 90: -0.20, 95: -0.32, 100: -0.50}
CALL_DELTAS = {100: 0.50, 105: 0.30, 110: 0.20, 115: 0.12, 120: 0.08}
PUT_MIDS = {80: 0.50, 85: 0.80, 90: 1.20, 95: 2.00, 100: 3.50}
CALL_MIDS = {100: 3.50, 105: 2.00, 110: 1.20, 115: 0.80, 120: 0.50}


def _proposal(symbol, dte=40):
    """A real proposal off a real ladder, run through the real engine — the
    rank view reads structures now, and a duck-typed stand-in would only prove
    the stand-in works. Return on capital is identical across these, so `dte`
    alone decides the annualized ordering."""
    from tau.chain import Cycle, Leg
    from tau.propose import propose_on
    from tau.screen import Candidate as Cand

    def leg(strike, option_type, delta, mid):
        return Leg(
            occ=f"{option_type}{strike:g}{symbol}",
            streamer=f"s{option_type}{strike:g}{symbol}",
            strike=float(strike), type=option_type,
            bid=mid - 0.01, ask=mid + 0.01, delta=delta, iv=0.30,
        )

    legs = [leg(k, P, d, PUT_MIDS[k]) for k, d in PUT_DELTAS.items()]
    legs += [leg(k, C, d, CALL_MIDS[k]) for k, d in CALL_DELTAS.items()]
    cy = Cycle(symbol=symbol, expiration=date(2026, 9, 4), dte=dte,
               underlying=100.0, legs=tuple(legs), fetched_at=datetime.now(UTC))
    candidate = Cand(symbol=symbol, ivr=50.0, ivp=50.0, iv30=30.0, hv30=25.0,
                     liquidity=4, beta=1.0, earnings_date=None)
    return propose_on(candidate, cy)


@pytest.mark.asyncio
async def test_rank_view_prices_the_passing_shortlist():
    async def loader():
        return list(FIXTURE)  # HIGH, ERN(excluded by earnings), CHEAP pass by default

    proposals = {
        "HIGH": _proposal("HIGH", dte=80),
        "CHEAP": _proposal("CHEAP", dte=20),  # same trade, annualizes higher
        "MID": _proposal("MID", dte=40),
    }
    a = TauApp(loader=loader, proposal_loader=_proposal_loader_factory(proposals))
    async with a.run_test() as pilot:
        await pilot.pause()
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]  # screen order, default IVR sort
        await pilot.press("p")
        await pilot.pause()
        assert a.mode == "rank"
        # Same return on capital, shorter tenor — ANN% rank reorders them.
        assert [c.symbol for c in a._rank_rows] == ["CHEAP", "MID", "HIGH"]
        # the winning structure is named, not just its numbers
        assert a._proposals["CHEAP"].best.strategy.name in _row_text(a, 0)


@pytest.mark.asyncio
async def test_rank_view_reuses_cached_proposals_on_reentry():
    async def loader():
        return [FIXTURE[0]]  # just HIGH

    calls = []

    async def track_loader(candidates, on_done):
        calls.append([c.symbol for c in candidates])
        on_done(_proposal("HIGH"))

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
        on_done(_proposal("HIGH"))

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
        {"HIGH": _proposal("HIGH"), "CHEAP": _proposal("CHEAP"), "MID": _proposal("MID")}
    ))
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert a.mode == "rank"
        await pilot.press("escape")
        assert a.mode == "screen"
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]


@pytest.mark.asyncio
async def test_enter_in_the_rank_view_opens_every_variant_considered():
    """The drill-in exists so a rejection can be read. Failures stay in the
    list with their reasons rather than leaving a name looking empty."""
    async def loader():
        return [FIXTURE[0]]

    a = TauApp(
        loader=loader,
        proposal_loader=_proposal_loader_factory({"HIGH": _proposal("HIGH")}),
    )
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert a.mode == "variants"
        assert a._variants_symbol == "HIGH"
        rows = a._variant_rows
        assert len(rows) == len(a._proposals["HIGH"].structures)
        assert any(s.ok for s in rows) and any(not s.ok for s in rows)
        # passing variants first, and a rejected one still carries its reason
        assert rows[0].ok
        rejected = next(s for s in rows if s.complete and not s.ok)
        assert rejected.failures[0].reason


@pytest.mark.asyncio
async def test_escape_walks_back_one_view_at_a_time():
    async def loader():
        return [FIXTURE[0]]

    a = TauApp(
        loader=loader,
        proposal_loader=_proposal_loader_factory({"HIGH": _proposal("HIGH")}),
    )
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        assert a.mode == "variants"
        await pilot.press("escape")
        assert a.mode == "rank"
        await pilot.press("escape")
        assert a.mode == "screen"


@pytest.mark.asyncio
async def test_variants_from_the_screen_view_loads_the_chain_first():
    """`v` on an unpriced name has nothing to show, so it fetches rather than
    opening an empty table — and the fetched cycle becomes a full proposal,
    so ranking it afterwards costs nothing."""
    from tau.chain import Cycle, Leg

    calls = []

    async def chain_loader(candidate):
        calls.append(candidate.symbol)
        return Cycle(
            symbol=candidate.symbol, expiration=date(2026, 9, 4), dte=40,
            underlying=100.0,
            legs=(
                Leg("P85", "s1", 85.0, P, bid=1.0, ask=1.2, delta=-0.16, iv=0.3),
                Leg("C115", "s2", 115.0, C, bid=0.8, ask=1.0, delta=0.17, iv=0.3),
            ),
            fetched_at=datetime.now(UTC),
        )

    async def loader():
        return [FIXTURE[0]]

    a = TauApp(loader=loader, chain_loader=chain_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("v")
        assert await _settle(a, lambda: "HIGH" in a._proposals)
        assert a.mode == "screen"  # first press fetched; it did not open blank
        await pilot.press("v")
        await pilot.pause()
        assert a.mode == "variants"
        assert calls == ["HIGH"]


@pytest.mark.asyncio
async def test_strategy_picker_toggles_without_refetching():
    """Turning a strategy off is a view over structures already in hand, so it
    must re-rank with no further calls to the pricing loader."""
    async def loader():
        return [FIXTURE[0]]

    calls = []

    async def track_loader(candidates, on_done):
        calls.append([c.symbol for c in candidates])
        on_done(_proposal("HIGH"))

    a = TauApp(loader=loader, proposal_loader=track_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        before = a.proposal_for("HIGH").best.strategy.name
        assert len(a._enabled) == 6

        await pilot.press("S")
        await pilot.pause()
        # the picker opens on the first strategy; turn it off and close
        first = a._strategies[0].name
        await pilot.press("space")
        await pilot.press("escape")
        await pilot.pause()

        assert first not in a._enabled
        assert len(a._enabled) == 5
        assert first not in {
            s.strategy.name for s in a.proposal_for("HIGH").structures
        }
        assert calls == [["HIGH"]]  # no refetch
        if before == first:
            assert a.proposal_for("HIGH").best.strategy.name != first


@pytest.mark.asyncio
async def test_picker_will_not_leave_every_strategy_disabled():
    """An empty rank view reads as a broken scan rather than a filter."""
    async def loader():
        return [FIXTURE[0]]

    a = TauApp(loader=loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("S")
        await pilot.pause()
        picker = a.screen
        picker.action_enable_none()
        assert len(picker._enabled) == 1
        picker.action_toggle()  # the last one must survive
        assert len(picker._enabled) == 1
        picker.action_enable_all()
        assert len(picker._enabled) == 6
        await pilot.press("escape")
        await pilot.pause()
        assert len(a._enabled) == 6
