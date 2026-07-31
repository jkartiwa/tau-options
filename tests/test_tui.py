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

    async def chain_loader(candidate, expiration=None, target_dte=45):
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

    async def slow_chain_loader(candidate, expiration=None, target_dte=45):
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

    async def loader(candidates, on_done, target_dte=45, target_delta=0.16):
        for c in candidates:
            on_done(proposals_by_symbol[c.symbol])

    return loader


def _fake_proposal(
    symbol, dte=40, credit=5.0, bpr=2000.0, pop=0.75, spread_cost=0.10, theta=0.05
):
    """A real `Proposal` over a synthetic chain, with only the figures a test
    pins overridden.

    Subclassing rather than reimplementing the interface is deliberate: a
    hand-written stand-in silently stops covering every metric added to
    Proposal after it, and the rank view reads those metrics by name.
    """
    from tau.chain import Cycle, Leg, Strangle
    from tau.propose import Proposal

    half = credit / 2
    put = Leg(f"P{symbol}", f"sP{symbol}", 90.0, "P", bid=half - 0.05, ask=half + 0.05,
              delta=-0.16, theta=-theta / 2, iv=0.3)
    call = Leg(f"C{symbol}", f"sC{symbol}", 110.0, "C", bid=half - 0.05, ask=half + 0.05,
               delta=0.16, theta=-theta / 2, iv=0.3)
    cy = Cycle(symbol=symbol, expiration=date(2026, 9, 4), dte=dte, underlying=100.0,
               legs=(put, call), fetched_at=datetime.now(UTC))
    st = Strangle(put, call, target_delta=0.16)

    class FakeProposal(Proposal):
        @property
        def bpr(self):
            return bpr

        @property
        def pop(self):
            return pop

        @property
        def spread_cost(self):
            return spread_cost

        @property
        def be_over_em(self):
            return 1.0

    return FakeProposal(candidate=cand(symbol, 50.0), cycle=cy, strangle=st)


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
        await pilot.press("P")
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

    async def track_loader(candidates, on_done, target_dte=45, target_delta=0.16):
        calls.append([c.symbol for c in candidates])
        on_done(_fake_proposal("HIGH"))

    a = TauApp(loader=loader, proposal_loader=track_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("P")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.press("P")
        await pilot.pause()
        assert calls == [["HIGH"]]  # second entry served from cache, no refetch


@pytest.mark.asyncio
async def test_reprice_forces_a_refetch():
    async def loader():
        return [FIXTURE[0]]

    calls = []

    async def track_loader(candidates, on_done, target_dte=45, target_delta=0.16):
        calls.append([c.symbol for c in candidates])
        on_done(_fake_proposal("HIGH"))

    a = TauApp(loader=loader, proposal_loader=track_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("P")
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
        await pilot.press("P")
        await pilot.pause()
        assert a.mode == "rank"
        await pilot.press("escape")
        assert a.mode == "screen"
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]


# ---- symbol jump ----


@pytest.mark.asyncio
async def test_slash_moves_the_cursor_and_keeps_every_row_visible():
    """Filtering answers "show me only these", which the thresholds already
    do. Typing a ticker asks "take me to this one", and the rows either side
    are the context the list was being read for."""
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]
        await pilot.press("/")
        await pilot.press("m", "i")
        await pilot.pause()
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]  # nothing hidden
        assert a.selected.symbol == "MID"  # cursor moved instead


@pytest.mark.asyncio
async def test_enter_keeps_the_landing_row_and_returns_focus_to_the_table():
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.press("c")
        await pilot.press("enter")
        await pilot.pause()
        assert a.selected.symbol == "CHEAP"
        assert a.query_one("#table").has_focus
        assert not a.searching


@pytest.mark.asyncio
async def test_escape_puts_the_cursor_back_where_the_jump_started():
    """A cancelled jump has to be a no-op, or `/` becomes a keypress you
    hesitate over."""
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        a.query_one("#table").move_cursor(row=2)
        await pilot.pause()
        assert a.selected.symbol == "MID"
        await pilot.press("/")
        await pilot.press("h")
        await pilot.pause()
        assert a.selected.symbol == "HIGH"  # jumped
        await pilot.press("escape")
        await pilot.pause()
        assert a.selected.symbol == "MID"  # and back


@pytest.mark.asyncio
async def test_escape_cancels_the_jump_before_leaving_the_page():
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        a.mode = "rank"
        await pilot.press("/")
        await pilot.press("h")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert a.mode == "rank"  # the page survives a cancelled jump
        await pilot.press("escape")
        await pilot.pause()
        assert a.mode == "screen"


@pytest.mark.asyncio
async def test_a_jump_to_an_excluded_symbol_says_why_it_is_not_there():
    """Typing the ticker of a name the screen dropped otherwise looks
    exactly like the tool being broken."""
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.press("i", "l", "l")  # ILLIQ, excluded on liquidity
        await pilot.pause()
        assert "ILLIQ excluded" in a._jump_miss
        assert "liquidity 1 < 3" in a._jump_miss


@pytest.mark.asyncio
async def test_a_jump_to_an_unknown_symbol_reports_the_miss():
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.press("z", "z")
        await pilot.pause()
        assert "no match for ZZ" in a._jump_miss


@pytest.mark.asyncio
async def test_jump_keys_do_not_trigger_bindings():
    """`q` typed into the jump bar is a letter, not quit."""
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.press("q")
        await pilot.pause()
        assert a.is_running


# ---- help ----


@pytest.mark.asyncio
async def test_help_overlay_opens_and_any_key_closes_it():
    from tau.tui.help import HelpScreen

    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(a.screen, HelpScreen)
        await pilot.press("j")
        await pilot.pause()
        assert not isinstance(a.screen, HelpScreen)


# ---- expirations ----


def _cycle_at(symbol, expiration, dte):
    from tau.chain import Cycle, Leg

    return Cycle(
        symbol=symbol,
        expiration=expiration,
        dte=dte,
        underlying=100.0,
        legs=(
            Leg("P90", "s1", 90.0, "P", bid=1.0, ask=1.2, delta=-0.16, theta=-0.03, iv=0.3),
            Leg("C110", "s2", 110.0, "C", bid=0.8, ask=1.0, delta=0.17, theta=-0.02, iv=0.3),
        ),
        fetched_at=datetime.now(UTC),
        expirations=(
            (date(2026, 8, 21), 17),
            (date(2026, 9, 18), 45),
            (date(2026, 10, 16), 73),
        ),
    )


def _expiration_app():
    asked = []

    async def loader():
        return [FIXTURE[0]]

    async def chain_loader(candidate, expiration=None, target_dte=45):
        asked.append(expiration)
        if expiration is None:
            return _cycle_at(candidate.symbol, date(2026, 9, 18), 45)
        dte = {date(2026, 8, 21): 17, date(2026, 9, 18): 45, date(2026, 10, 16): 73}
        return _cycle_at(candidate.symbol, expiration, dte[expiration])

    return TauApp(loader=loader, chain_loader=chain_loader), asked


@pytest.mark.asyncio
async def test_expiration_keys_walk_the_term_structure():
    a, asked = _expiration_app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        assert await _settle(a, lambda: "HIGH" in a._cycles)
        assert a._cycles["HIGH"].expiration == date(2026, 9, 18)
        await pilot.press(">")
        assert await _settle(
            a, lambda: a._cycles["HIGH"].expiration == date(2026, 10, 16)
        )
        await pilot.press("<")
        assert await _settle(
            a, lambda: a._cycles["HIGH"].expiration == date(2026, 9, 18)
        )
        assert asked == [None, date(2026, 10, 16), date(2026, 9, 18)]


@pytest.mark.asyncio
async def test_expiration_walk_stops_at_the_ends_instead_of_wrapping():
    a, _ = _expiration_app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        assert await _settle(a, lambda: "HIGH" in a._cycles)
        await pilot.press("<")  # to the front month
        assert await _settle(
            a, lambda: a._cycles["HIGH"].expiration == date(2026, 8, 21)
        )
        await pilot.press("<")  # nothing nearer
        await pilot.pause()
        assert "nearest" in a._detail_status
        assert a._cycles["HIGH"].expiration == date(2026, 8, 21)


@pytest.mark.asyncio
async def test_expiration_keys_say_so_when_no_chain_is_loaded():
    a, asked = _expiration_app()
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press(">")
        await pilot.pause()
        assert "load a chain first" in a._detail_status
        assert asked == []


# ---- the account ----


def _book():
    """A short strangle on HIGH, opened for 3.00 and now worth 1.50 — half
    the credit collected, which is where the usual take-profit rule fires."""
    from tau.portfolio import Book, Position

    def leg(strike, right, open_price, mark_price):
        return Position(
            f"H26{right}", "HIGH", "Equity Option", -2.0,
            date(2026, 9, 18), strike, right,
            open_price=open_price, mark_price=mark_price,
            multiplier=100.0, opened_at=date(2026, 7, 1),
        )

    return Book(
        positions=(leg(90.0, "P", 2.00, 1.00), leg(110.0, "C", 1.00, 0.50)),
        net_liq=100_000.0,
        maintenance=25_000.0,
        account_number="5WX",
        requirements={"HIGH": 4_200.0},
    )


@pytest.mark.asyncio
async def test_the_screener_carries_a_marker_not_a_position_column():
    """Positions have their own page. What the screener keeps is the one
    fact that changes whether a row is a trade at all — that you are already
    short the name — and it costs no horizontal space."""
    async def loader():
        return list(FIXTURE)

    async def book_loader():
        return _book()

    a = TauApp(loader=loader, book_loader=book_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert await _settle(a, lambda: a.has_book)
        assert "POS" not in a._screen_columns()
        assert a._marker("HIGH", "\u00b7") == "[yellow]\u25c6[/yellow]"
        assert a._marker("MID", "\u00b7") == "\u00b7"


@pytest.mark.asyncio
async def test_the_held_marker_outranks_a_star():
    """A star is a note to yourself; being short the name is a fact."""
    async def loader():
        return list(FIXTURE)

    async def book_loader():
        return _book()

    a = TauApp(loader=loader, book_loader=book_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert await _settle(a, lambda: a.has_book)
        await pilot.press("space")  # star HIGH, which is also held
        assert "HIGH" in a._starred
        assert a._marker("HIGH", "\u00b7") == "[yellow]\u25c6[/yellow]"


# ---- positions page ----


@pytest.mark.asyncio
async def test_p_opens_the_positions_page_with_its_own_rows():
    async def loader():
        return list(FIXTURE)

    async def book_loader():
        return _book()

    a = TauApp(loader=loader, book_loader=book_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert await _settle(a, lambda: a.has_book)
        await pilot.press("p")
        await pilot.pause()
        assert a.mode == "positions"
        assert [t.underlying for t in a._trades] == ["HIGH"]
        assert a._columns_mode[0] == "positions"
        rendered = str(a.query_one("#detail").content)
        assert "90P" in rendered  # the leg detail, not a screener candidate
        assert "credit" in rendered and "P/L" in rendered


@pytest.mark.asyncio
async def test_escape_returns_from_positions_to_the_screener():
    async def loader():
        return list(FIXTURE)

    async def book_loader():
        return _book()

    a = TauApp(loader=loader, book_loader=book_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert await _settle(a, lambda: a.has_book)
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert a.mode == "screen"
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]


@pytest.mark.asyncio
async def test_candidate_actions_are_inert_on_a_position_row():
    """`c` and `w` price and classify a screen candidate. A position row is
    not one, and pressing them there must do nothing rather than raise."""
    async def loader():
        return list(FIXTURE)

    async def book_loader():
        return _book()

    calls = []

    async def chain_loader(candidate, expiration=None, target_dte=45):
        calls.append(candidate.symbol)
        raise AssertionError("should never be reached from the positions page")

    a = TauApp(loader=loader, book_loader=book_loader, chain_loader=chain_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert await _settle(a, lambda: a.has_book)
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("c")
        await pilot.press("w")
        await pilot.press("space")
        await pilot.pause()
        assert calls == []
        assert a.is_running


@pytest.mark.asyncio
async def test_positions_page_is_empty_rather_than_broken_without_an_account():
    async def loader():
        return list(FIXTURE)

    async def boom():
        raise RuntimeError("forbidden")

    a = TauApp(loader=loader, book_loader=boom)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert await _settle(a, lambda: bool(a._book_error))
        await pilot.press("p")
        await pilot.pause()
        assert a.mode == "positions"
        assert a._trades == []
        assert "no open positions" in str(a.query_one("#detail").content)


@pytest.mark.asyncio
async def test_existing_short_premium_is_called_out_in_the_detail_pane():
    async def loader():
        return list(FIXTURE)

    async def book_loader():
        return _book()

    a = TauApp(loader=loader, book_loader=book_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert await _settle(a, lambda: a.has_book)
        rendered = str(a.query_one("#detail").content)
        assert "held" in rendered
        assert "already short premium" in rendered


@pytest.mark.asyncio
async def test_an_unreadable_account_never_blocks_the_screen():
    """The book is an enrichment. A grant without account access is a normal
    configuration, not a failure of the tool."""
    async def loader():
        return list(FIXTURE)

    async def boom():
        raise RuntimeError("forbidden")

    a = TauApp(loader=loader, book_loader=boom)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert await _settle(a, lambda: bool(a._book_error))
        assert symbols(a) == ["HIGH", "CHEAP", "MID"]  # screen unaffected
        assert not a.has_book
        assert "account unavailable" in str(a.query_one("#meta").content)


@pytest.mark.asyncio
async def test_refresh_rereads_the_account_too():
    """A fill or an assignment moves the book without touching the screen,
    so a refresh that skipped it would leave the position columns describing
    a book that no longer exists."""
    async def loader():
        return list(FIXTURE)

    calls = []

    async def book_loader():
        calls.append(1)
        return _book()

    a = TauApp(loader=loader, book_loader=book_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert await _settle(a, lambda: len(calls) == 1)
        await pilot.press("r")
        assert await _settle(a, lambda: len(calls) == 2)
