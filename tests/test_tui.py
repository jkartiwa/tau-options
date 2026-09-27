import asyncio
from dataclasses import replace
from datetime import date, timedelta

import pytest
from textual.widgets import DataTable

from tau import broker as broker_mod
from tau import propose as propose_mod
from tau.catalyst import Brief
from tau.chain import Cycle, Leg
from tau.fmt import fmt
from tau.history import Bar, History
from tau.payoff import OptionType
from tau.propose import propose_on
from tau.tui.app import TauApp
from tau.tui.detail import DetailPane
from tests.factories import cand, cycle

C, P = OptionType.CALL, OptionType.PUT

TODAY = date.today()


FIXTURE = [
    cand("HIGH", ivr=90.0),
    cand("MID", ivr=45.0),
    cand("LOW", ivr=10.0),
    cand("ILLIQ", ivr=80.0, liquidity=1),
    cand("ERN", ivr=70.0, earnings_date=TODAY + timedelta(days=10)),
    cand("CHEAP", ivr=60.0, iv30=20.0, hv30=40.0),
]


def app(fixture=FIXTURE, **loaders) -> TauApp:
    async def loader():
        return list(fixture)

    return TauApp(loader=loader, **loaders)


def _strangle_cycle(symbol="HIGH") -> Cycle:
    """The smallest chain a strangle prices on: one 16Δ put, one 17Δ call."""
    return Cycle(
        symbol=symbol,
        expiration=date(2026, 9, 4),
        dte=40,
        underlying=100.0,
        legs=(
            Leg("P85", "s1", 85.0, P, bid=1.0, ask=1.2, delta=-0.16, iv=0.30),
            Leg("C115", "s2", 115.0, C, bid=0.8, ask=1.0, delta=0.17, iv=0.30),
        ),
    )


@pytest.fixture(autouse=True)
def _no_broker_network(monkeypatch):
    """The broker dry-run talks to the live account API; the test suite must
    never. The enrichment falls back to the formula estimate when the account
    is unreachable, and these stubs simulate exactly that."""

    async def no_account(session):
        return None

    async def no_bpr(session, account, structure):
        return None

    monkeypatch.setattr(broker_mod, "margin_account", no_account)
    monkeypatch.setattr(broker_mod, "broker_bpr_for", no_bpr)


def symbols(a: TauApp) -> list[str]:
    return [c.symbol for c in a._rows]


def _row_text(a: TauApp, index: int) -> str:
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
    cycle = _strangle_cycle()

    calls = []

    async def chain_loader(candidate):
        calls.append(candidate.symbol)
        return cycle

    a = app(chain_loader=chain_loader)
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
    calls = calls if calls is not None else []
    history = history or History(
        symbol="HIGH",
        bars=tuple(
            Bar(
                day=TODAY - timedelta(days=i),
                high=120.0,
                low=80.0,
                close=100.0,
            )
            for i in range(60, 0, -1)
        ),
    )
    brief = brief or Brief(
        symbol="HIGH",
        classification="resolved",
        catalyst="Q2 earnings reported",
        key_dates=(),
        confidence="high",
        note="Event passed; IV should bleed.",
        headlines=(),
    )

    async def history_loader(candidate):
        calls.append(("history", candidate.symbol))
        return history

    async def brief_loader(candidate):
        calls.append(("brief", candidate.symbol))
        return brief

    return app(history_loader=history_loader, brief_loader=brief_loader), calls


def _trip_broker_breaker() -> None:
    """Put the breaker in the state consecutive dry-run failures put it in.
    What counts as a failure and how it recovers is covered in test_broker;
    these tests are about what the screen says once it has tripped."""
    for _ in range(broker_mod.MAX_CONSECUTIVE_FAILURES):
        broker_mod._record_failure()


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
    started = asyncio.Event()
    chain_calls = []

    async def slow_chain_loader(candidate):
        started.set()
        await asyncio.sleep(0.3)
        chain_calls.append(candidate.symbol)
        return _strangle_cycle(candidate.symbol)

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


def _proposal(symbol, dte=40):
    """A real proposal off a real ladder, run through the real engine, since
    the rank view reads structures. Return on capital is identical across
    these, so `dte` alone decides the annualized ordering."""
    cy = cycle(dte=dte, symbol=symbol)
    return propose_on(cand(symbol), cy)


@pytest.mark.asyncio
async def test_rank_view_prices_the_passing_shortlist():
    proposals = {
        "HIGH": _proposal("HIGH", dte=80),
        "CHEAP": _proposal("CHEAP", dte=20),  # same trade, annualizes higher
        "MID": _proposal("MID", dte=40),
    }
    a = app(proposal_loader=_proposal_loader_factory(proposals))
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
    calls = []

    async def track_loader(candidates, on_done):
        calls.append([c.symbol for c in candidates])
        on_done(_proposal("HIGH"))

    a = app([FIXTURE[0]], proposal_loader=track_loader)
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
    calls = []

    async def track_loader(candidates, on_done):
        calls.append([c.symbol for c in candidates])
        on_done(_proposal("HIGH"))

    a = app([FIXTURE[0]], proposal_loader=track_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert calls == [["HIGH"], ["HIGH"]]


@pytest.mark.asyncio
async def test_reprice_mid_run_reprices_the_names_already_done():
    """`R` while a run is in flight restarts it over the whole pass. Waiting
    on the old run would leave the names it had finished blank, since their
    proposals were dropped and that run does not price them again."""
    release = asyncio.Event()
    calls = []

    async def slow_loader(candidates, on_done):
        calls.append([c.symbol for c in candidates])
        on_done(_proposal(candidates[0].symbol))
        await release.wait()
        for c in candidates[1:]:
            on_done(_proposal(c.symbol))

    a = app(proposal_loader=slow_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert set(a._proposals) == {"HIGH"}
        await pilot.press("R")
        await pilot.pause()
        release.set()
        await pilot.pause()
        assert [sorted(c) for c in calls] == [["CHEAP", "HIGH", "MID"]] * 2
        assert set(a._proposals) == {"HIGH", "CHEAP", "MID"}
        assert not a.pricing


@pytest.mark.asyncio
async def test_chain_load_does_not_cancel_an_in_flight_pricing_run():
    release = asyncio.Event()

    async def slow_loader(candidates, on_done):
        await release.wait()
        for c in candidates:
            on_done(_proposal(c.symbol))

    async def chain_loader(candidate):
        return _strangle_cycle(candidate.symbol)

    a = app(proposal_loader=slow_loader, chain_loader=chain_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.press("c")
        assert await _settle(a, lambda: "HIGH" in a._proposals)
        release.set()
        assert await _settle(a, lambda: set(a._proposals) == {"HIGH", "CHEAP", "MID"})
        assert await _settle(a, lambda: not a.pricing)


@pytest.mark.asyncio
async def test_refetch_stops_pricing_without_leaving_it_marked_in_progress():
    calls = []

    async def stalled_loader(candidates, on_done):
        calls.append([c.symbol for c in candidates])
        await asyncio.Event().wait()

    a = app(proposal_loader=stalled_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert await _settle(a, lambda: a.pricing)
        await pilot.press("r")
        assert await _settle(a, lambda: not a.pricing)
        await pilot.press("p")  # prices again rather than waiting on a dead run
        assert await _settle(a, lambda: len(calls) == 2)


@pytest.mark.asyncio
async def test_escape_returns_to_screen_view():
    a = app(
        proposal_loader=_proposal_loader_factory(
            {
                "HIGH": _proposal("HIGH"),
                "CHEAP": _proposal("CHEAP"),
                "MID": _proposal("MID"),
            }
        ),
    )
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

    a = app(
        [FIXTURE[0]],
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
    a = app(
        [FIXTURE[0]],
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
    calls = []

    async def chain_loader(candidate):
        calls.append(candidate.symbol)
        return _strangle_cycle(candidate.symbol)

    a = app([FIXTURE[0]], chain_loader=chain_loader)
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

    calls = []

    async def track_loader(candidates, on_done):
        calls.append([c.symbol for c in candidates])
        on_done(_proposal("HIGH"))

    a = app([FIXTURE[0]], proposal_loader=track_loader)
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
        assert first not in {s.strategy.name for s in a.proposal_for("HIGH").structures}
        assert calls == [["HIGH"]]  # no refetch
        if before == first:
            assert a.proposal_for("HIGH").best.strategy.name != first


@pytest.mark.asyncio
async def test_picker_will_not_leave_every_strategy_disabled():
    """An empty rank view reads as a broken scan rather than a filter."""

    a = app([FIXTURE[0]])
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("S")
        await pilot.pause()
        picker = a.screen
        picker.action_enable_only()
        assert len(picker._enabled) == 1
        picker.action_toggle()  # the last one must survive
        assert len(picker._enabled) == 1
        picker.action_enable_all()
        assert len(picker._enabled) == 6
        await pilot.press("escape")
        await pilot.pause()
        assert len(a._enabled) == 6


@pytest.mark.asyncio
async def test_chain_load_survives_missing_credentials(monkeypatch):
    """No env, no broker dry-run, no crash: the chain load still caches the
    proposal with its formula figures and the detail pane renders."""
    cycle = _strangle_cycle()

    async def chain_loader(candidate):
        return cycle

    def no_session():
        raise RuntimeError("TASTY_CLIENT_SECRET / TASTY_REFRESH_TOKEN not set (.env)")

    monkeypatch.setattr("tau.tui.app.get_session", no_session)

    a = app(chain_loader=chain_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        assert await _settle(a, lambda: "HIGH" in a._proposals)
        p = a._proposals["HIGH"]
        assert p.best is not None and p.best.bpr_source == "estimate"
        rendered = str(a.query_one("#detail").content)
        assert "strangle" in rendered and "BPR~" in rendered


@pytest.mark.asyncio
async def test_rank_table_marks_broker_and_formula_bpr_sources(monkeypatch):
    """Broker-sourced buying power renders plain under the `BPR` header;
    the formula estimate carries a tilde. The row itself has to say which
    model the number came from."""

    base = _proposal("HIGH")
    fake_account = object()

    async def fake_margin(session):
        return fake_account

    async def fake_bpr(session, account, structure):
        # a 10% haircut over the formula, applied to the whole shortlist:
        # the top-ranked structure stays on top, so `best` is broker-priced
        return structure.bpr * 0.9 if structure.bpr else None

    monkeypatch.setattr(broker_mod, "margin_account", fake_margin)
    monkeypatch.setattr(broker_mod, "broker_bpr_for", fake_bpr)

    enriched = await propose_mod.enrich_with_broker_bpr(object(), base)
    assert enriched.best.bpr_source == "broker"

    a = app([FIXTURE[0]], proposal_loader=_proposal_loader_factory({"HIGH": enriched}))
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        table = a.query_one("#table", DataTable)
        assert "BPR" in [c.label.plain for c in table.columns.values()]
        best = enriched.best
        row = list(table.get_row_at(0))
        assert row[6] == fmt(best.bpr, ",.0f")  # plain: broker-sourced

    # the same shortlist un-enriched falls back to the formula — tilde on
    a2 = app([FIXTURE[0]], proposal_loader=_proposal_loader_factory({"HIGH": base}))
    async with a2.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        table = a2.query_one("#table", DataTable)
        row = list(table.get_row_at(0))
        assert row[6].endswith("~")


@pytest.mark.asyncio
async def test_chain_load_renders_before_the_broker_answers(monkeypatch):
    """The drill-in must never wait on the account API. The variants show up
    on the formula figures the `~` marks as estimates, and the broker upgrades
    the rows it answers for afterwards."""
    cycle = _strangle_cycle()
    release = asyncio.Event()

    async def chain_loader(candidate):
        return cycle

    async def slow_enrich(session, proposal, *args, **kwargs):
        await release.wait()
        return replace(
            proposal,
            structures=tuple(
                replace(s, broker_bpr=2500.0) if s is proposal.best else s
                for s in proposal.structures
            ),
        )

    monkeypatch.setattr("tau.tui.app.get_session", lambda: object())
    monkeypatch.setattr(propose_mod, "enrich_with_broker_bpr", slow_enrich)

    a = app(chain_loader=chain_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        # cached and painted while the account API is still hanging
        assert await _settle(a, lambda: "HIGH" in a._proposals)
        assert not release.is_set()
        formula = a._proposals["HIGH"]
        assert formula.best is not None
        assert formula.best.bpr_source == "estimate"
        assert a._detail_status == ""
        assert "~" in str(a.query_one("#detail").content)

        release.set()
        assert await _settle(
            a, lambda: a._proposals["HIGH"].best.bpr_source == "broker"
        )
        assert a._proposals["HIGH"].best.bpr == pytest.approx(2500.0)


@pytest.mark.asyncio
async def test_the_variants_drill_in_upgrades_when_the_broker_answers(monkeypatch):
    """Opening the drill-in before enrichment returns freezes a snapshot of
    the formula structures. The broker figures landing behind it must reach
    the rendered rows, not just the proposal cache."""
    cycle = _strangle_cycle()
    release = asyncio.Event()

    async def chain_loader(candidate):
        return cycle

    async def slow_enrich(session, proposal, *args, **kwargs):
        await release.wait()
        return replace(
            proposal,
            structures=tuple(
                replace(s, broker_bpr=2500.0) if s.ok else s
                for s in proposal.structures
            ),
        )

    monkeypatch.setattr("tau.tui.app.get_session", lambda: object())
    monkeypatch.setattr(propose_mod, "enrich_with_broker_bpr", slow_enrich)

    a = app(chain_loader=chain_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        assert await _settle(a, lambda: "HIGH" in a._proposals)

        # drill in while the account API is still hanging
        await pilot.press("v")
        await pilot.pause()
        assert a.mode == "variants"
        assert not release.is_set()
        assert "~" in _row_text(a, 0)

        release.set()
        assert await _settle(
            a, lambda: a._proposals["HIGH"].best.bpr_source == "broker"
        )
        assert await _settle(a, lambda: "~" not in _row_text(a, 0))
        assert "2,500" in _row_text(a, 0)
        assert "BPR~" not in str(a.query_one("#detail").content)


@pytest.mark.asyncio
async def test_the_meta_line_says_when_the_broker_stopped_pricing(monkeypatch):
    """The breaker's own warning goes to a logger, and Textual redirects
    stderr for the life of the app. On screen the meta line is the only thing
    separating "the broker stopped answering" from "these were always
    estimates"."""
    a = app()
    async with a.run_test() as pilot:
        await pilot.pause()
        assert "broker BPR off" not in str(a.query_one("#meta").content)

        _trip_broker_breaker()
        assert broker_mod.dry_runs_disabled()
        a.refresh_meta()
        await pilot.pause()
        assert "broker BPR off" in str(a.query_one("#meta").content)


def _pane_lines(proposal):
    """The detail pane as it renders for a name in the rank view — the whole
    pane, because the winner is shown twice in it."""
    return DetailPane()._cycle_lines(proposal.candidate, proposal)


def _header_ann(lines):
    return next(ln for ln in lines if "· ANN " in ln).split("· ANN ")[-1].strip()


def _ladder_rows(lines):
    start = next(
        i for i, ln in enumerate(lines) if ln.endswith("credit / POP / ANN[/dim]")
    )
    rows = []
    for line in lines[start + 1 :]:
        if "variants passed" in line:
            break
        rows.append(line)
    return rows


def _family(proposal):
    return [
        s
        for s in proposal.structures
        if s.strategy.name == proposal.best.strategy.name and s.complete
    ]


def test_the_detail_ladder_compares_siblings_on_one_margin_model():
    """A ladder straddling the broker's bounded shortlist is shown on the
    formula estimate throughout. The `ANN` column has no source marker, so a
    mixed ladder would show a margin-model gap as a return difference."""
    p = _proposal("HIGH")
    family = _family(p)
    assert len(family) > 2

    def ann_column(proposal):
        return [row.split()[-1] for row in _ladder_rows(_pane_lines(proposal))]

    formula_column = ann_column(p)

    # every sibling priced 30% higher by the broker: a
    # uniform ladder moves together and stays the broker's
    uniform = replace(
        p,
        structures=tuple(
            replace(s, broker_bpr=s.bpr * 1.30) if s in family else s
            for s in p.structures
        ),
    )
    assert ann_column(uniform) != formula_column

    # one sibling left on the formula: the ladder is no longer comparable on
    # the broker's numbers, so every row falls back to the one they share
    mixed = replace(
        p,
        structures=tuple(
            replace(s, broker_bpr=s.bpr * 1.30)
            if s in family and s is not family[-1]
            else s
            for s in p.structures
        ),
    )
    assert ann_column(mixed) == formula_column


@pytest.mark.asyncio
async def test_a_breaker_trip_on_the_drill_in_path_reaches_the_meta_line(monkeypatch):
    """Working from screen mode, a user presses `c` on one name after
    another. That is a path the breaker can trip on, and nothing else
    repaints the chrome."""
    cycle = _strangle_cycle()

    async def chain_loader(candidate):
        return cycle

    async def trip_the_breaker(session, proposal, *args, **kwargs):
        _trip_broker_breaker()
        return proposal

    monkeypatch.setattr("tau.tui.app.get_session", lambda: object())
    monkeypatch.setattr("tau.propose.enrich_with_broker_bpr", trip_the_breaker)

    a = app(chain_loader=chain_loader)
    async with a.run_test() as pilot:
        await pilot.pause()
        assert "broker BPR off" not in str(a.query_one("#meta").content)
        await pilot.press("c")
        assert await _settle(a, lambda: broker_mod.dry_runs_disabled())
        assert await _settle(
            a, lambda: "broker BPR off" in str(a.query_one("#meta").content)
        )


def test_the_detail_pane_never_shows_two_different_anns_for_one_trade():
    """The winner appears twice in this pane: in its summary line and as the
    marked ladder row. When the ladder falls back to the formula estimate, the
    summary must follow, so one trade never shows two `ANN` figures."""
    p = _proposal("HIGH")
    family = _family(p)
    unpriced = next(s for s in reversed(family) if s.ok)
    mixed = replace(
        p,
        structures=tuple(
            replace(s, broker_bpr=s.bpr * 1.30)
            if s in family and s is not unpriced
            else s
            for s in p.structures
        ),
    )
    # the winner really did come back broker-priced, and its ladder really is
    # split across the two margin models — the case that produced the clash
    assert mixed.best.bpr_source == "broker"
    assert mixed.best.strategy.name == unpriced.strategy.name

    lines = _pane_lines(mixed)
    marked = next(row for row in _ladder_rows(lines) if row.startswith("\u203a"))
    assert _header_ann(lines) == marked.split()[-1]
    # and the pane says which model that is, rather than labelling a
    # formula-derived return with the broker's buying power beside it
    assert "BPR~" in next(ln for ln in lines if "· ANN " in ln)


def test_a_uniformly_priced_pane_keeps_the_broker_figure():
    """The fallback is the mixed ladder's, not a blanket retreat: when the
    broker priced the whole ladder the pane stays on its numbers and says so.
    """
    p = _proposal("HIGH")
    family = _family(p)
    uniform = replace(
        p,
        structures=tuple(
            replace(s, broker_bpr=s.bpr * 1.30) if s in family else s
            for s in p.structures
        ),
    )
    lines = _pane_lines(uniform)
    marked = next(row for row in _ladder_rows(lines) if row.startswith("\u203a"))
    assert _header_ann(lines) == marked.split()[-1]
    assert "(broker dry-run)" in next(ln for ln in lines if "· ANN " in ln)
