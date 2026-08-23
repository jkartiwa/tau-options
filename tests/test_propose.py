from datetime import UTC, date, datetime
from math import erf, log, sqrt

import pytest

from tau.build import MAX_DELTA_MISS, evaluate, evaluate_all
from tau.chain import Cycle, Leg
from tau.payoff import OptionType, Side
from tau.propose import (
    Proposal,
    naked_side_requirement,
    pop_between,
    propose_on,
    rank_proposals,
)
from tau.screen import Candidate
from tau.strategies import STRATEGIES
from tau.strategy import Bias, Delta, LegSpec, Require, Strategy, with_min_pop

C, P = OptionType.CALL, OptionType.PUT
SHORT = Side.SHORT

PUT_DELTAS = {80: -0.08, 85: -0.12, 90: -0.20, 95: -0.32, 100: -0.50}
CALL_DELTAS = {100: 0.50, 105: 0.30, 110: 0.20, 115: 0.12, 120: 0.08}
PUT_MIDS = {80: 0.50, 85: 0.80, 90: 1.20, 95: 2.00, 100: 3.50}
CALL_MIDS = {100: 3.50, 105: 2.00, 110: 1.20, 115: 0.80, 120: 0.50}
SPREAD = 0.02


def cand(symbol="TEST", iv30=30.0):
    return Candidate(
        symbol=symbol, ivr=50.0, ivp=50.0, iv30=iv30, hv30=25.0,
        liquidity=4, beta=1.0, earnings_date=None,
    )


def leg(strike, option_type, delta, mid, spread=SPREAD):
    return Leg(
        occ=f"{option_type}{strike:g}",
        streamer=f"s{option_type}{strike:g}",
        strike=float(strike),
        type=option_type,
        bid=mid - spread / 2,
        ask=mid + spread / 2,
        delta=delta,
        iv=0.30,
    )


def ladder():
    legs = [leg(k, P, d, PUT_MIDS[k]) for k, d in PUT_DELTAS.items()]
    legs += [leg(k, C, d, CALL_MIDS[k]) for k, d in CALL_DELTAS.items()]
    return tuple(legs)


def cycle(legs=None, underlying=100.0, dte=45):
    return Cycle(
        symbol="TEST", expiration=date(2026, 9, 18), dte=dte,
        underlying=underlying,
        legs=legs if legs is not None else ladder(),
        fetched_at=datetime.now(UTC),
    )


SHIPPED = tuple(STRATEGIES.values())


def proposal(symbol="TEST", cy=None, strategies=SHIPPED):
    cy = cy or cycle()
    return Proposal(cand(symbol), cy, tuple(evaluate_all(strategies, cy)))


def test_naked_requirement_uses_the_greater_of_two_formulas():
    # far OTM, cheap premium -> the 10%-of-strike floor should bind
    req = naked_side_requirement(
        spot=100.0, strike=50.0, premium=0.10, option_type=P
    )
    assert req == pytest.approx(max(
        (0.20 * 100 - 50 + 0.10) * 100,
        (0.10 * 50 + 0.10) * 100,
        50.0,
    ))


def test_naked_requirement_floor_applies_to_tiny_premium():
    req = naked_side_requirement(
        spot=10.0, strike=9.0, premium=0.01, option_type=P
    )
    assert req >= 50.0


def test_naked_requirement_otm_term_is_side_aware():
    # spot 100, premium 2.00. K=90 is OTM for a put by 10 and ITM for a
    # call; K=110 is the mirror image.
    assert naked_side_requirement(100.0, 90.0, 2.00, P) == pytest.approx(
        max((0.20 * 100 - 10 + 2.00) * 100, (0.10 * 90 + 2.00) * 100, 50.0)
    )
    assert naked_side_requirement(100.0, 110.0, 2.00, C) == pytest.approx(
        max((0.20 * 100 - 10 + 2.00) * 100, (0.10 * 110 + 2.00) * 100, 50.0)
    )


def test_naked_requirement_charges_an_itm_short_no_otm_credit():
    # ITM has no out-of-the-money distance to subtract: the 20% term is
    # charged in full. A short put at 110 against spot 100 is ITM.
    itm_put = naked_side_requirement(100.0, 110.0, 2.00, P)
    assert itm_put == pytest.approx(
        max((0.20 * 100 - 0.0 + 2.00) * 100, (0.10 * 110 + 2.00) * 100, 50.0)
    )
    assert itm_put == pytest.approx(2200.0)

    itm_call = naked_side_requirement(100.0, 90.0, 2.00, C)
    assert itm_call == pytest.approx(
        max((0.20 * 100 - 0.0 + 2.00) * 100, (0.10 * 90 + 2.00) * 100, 50.0)
    )
    assert itm_call == pytest.approx(2200.0)


def test_missing_underlying_quote_is_reported_as_a_data_gap():
    # Every metric fails closed without a spot price, so the constraint
    # tally would otherwise describe a dropped feed as a market condition.
    p = propose_on(cand(), cycle(underlying=None), SHIPPED)
    assert p.best is None
    assert p.error == "no underlying quote"


def test_missing_underlying_quote_survives_strategy_narrowing():
    p = propose_on(cand(), cycle(underlying=None), SHIPPED)
    narrowed = p.only({next(iter(STRATEGIES))})
    assert narrowed.error == "no underlying quote"


def test_pop_symmetric_breakevens_near_half_with_slight_drift_correction():
    # symmetric breakevens around spot -> the driftless-lognormal median
    # shift pushes PoP slightly above 0.5, never below.
    p = pop_between(spot=100.0, lower=90.0, upper=110.0, iv=0.30, dte=45)
    assert p is not None
    assert 0.5 < p < 0.7


def test_pop_wider_breakevens_increase_probability():
    narrow = pop_between(100.0, 95.0, 105.0, 0.30, 45)
    wide = pop_between(100.0, 80.0, 120.0, 0.30, 45)
    assert wide > narrow


def test_pop_handles_degenerate_inputs():
    assert pop_between(0.0, 90.0, 110.0, 0.3, 45) is None
    assert pop_between(100.0, 110.0, 90.0, 0.3, 45) is None  # inverted
    assert pop_between(100.0, 90.0, 110.0, 0.0, 45) is None
    assert pop_between(100.0, 90.0, 110.0, 0.3, 0) is None


def test_proposal_searches_every_strategy_over_one_cycle():
    p = proposal()
    assert p.ok
    names = {s.strategy.name for s in p.structures}
    assert names == set(STRATEGIES)


def test_best_is_the_highest_returning_structure_across_all_strategies():
    p = proposal()
    passing = [s for s in p.structures if s.ok]
    assert p.best is not None
    assert p.best.annualized_roc == pytest.approx(
        max(s.annualized_roc for s in passing)
    )
    assert p.annualized_roc == pytest.approx(p.best.annualized_roc)
    assert p.label.startswith(p.best.strategy.name)


def test_a_strategy_picks_its_own_winner_before_the_cross_comparison():
    """`rank` decides which of a strategy's own variants competes; the common
    metric decides between families. A strategy ranking on POP must put its
    highest-POP variant forward, not its highest-returning one."""
    wide = Strategy(
        name="t-pop-strangle",
        bias=Bias.NEUTRAL,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.08, 0.32])),
            LegSpec("short_call", type=C, side=SHORT, strike=Delta([0.08, 0.32])),
        ],
        rank="pop",
    )
    cy = cycle()
    own = [s for s in evaluate(wide, cy) if s.ok]
    p = Proposal(cand(), cy, tuple(own))
    assert p.best.pop == pytest.approx(max(s.pop for s in own))
    # and that is deliberately not the highest-returning variant
    assert p.best.annualized_roc < max(s.annualized_roc for s in own)


def test_best_across_strategies_never_picks_a_pop_floor_failure():
    """The cross-strategy comparison must not resurrect a variant a
    strategy's own `best()` already rejected for its pop: the naive
    highest-annualized_roc variant here fails the floor, so the winner has to
    be a lower-returning, passing one instead."""
    wide = Strategy(
        name="t-pop-gate",
        bias=Bias.NEUTRAL,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.08, 0.32])),
            LegSpec("short_call", type=C, side=SHORT, strike=Delta([0.08, 0.32])),
        ],
        require=[Require("pop", ">=", 0.70)],
    )
    cy = cycle()
    structures = tuple(evaluate(wide, cy))
    naive_top = max(structures, key=lambda s: s.annualized_roc)
    assert naive_top.pop < 0.70

    p = Proposal(cand(), cy, structures)
    assert p.best is not None
    assert p.best.pop >= 0.70
    assert p.best.annualized_roc < naive_top.annualized_roc


def test_delegated_figures_come_from_the_winning_structure():
    p = proposal()
    best = p.best
    assert p.credit == best.credit
    assert p.bpr == pytest.approx(best.bpr)
    assert p.roc == pytest.approx(best.roc)
    assert p.pop == pytest.approx(best.pop)
    assert p.spread_cost == pytest.approx(best.spread_cost)
    assert p.bias == str(best.strategy.bias)


def test_variants_ranks_everything_considered_failures_included():
    p = proposal()
    ordered = p.variants()
    assert len(ordered) == len(p.structures)
    passing = [s for s in ordered if s.ok]
    assert ordered[: len(passing)] == passing
    # the whole search is kept, not just what passed
    assert any(not s.ok for s in ordered)


def test_proposal_reports_error_and_is_not_ok():
    p = Proposal(cand(), error="no option chain for XYZ")
    assert not p.ok
    assert p.best is None
    assert p.credit is None
    assert p.bpr is None
    assert p.roc is None


def test_a_cycle_where_every_variant_fails_says_so_rather_than_going_blank():
    """A market condition, not a data problem — and the two read differently
    in the rank view, so they must not collapse into one silence."""
    impossible = Strategy(
        name="t-impossible",
        bias=Bias.NEUTRAL,
        legs=[LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20))],
        require=[Require("credit", ">=", 1_000)],
    )
    cy = cycle()
    structures = tuple(evaluate(impossible, cy))
    p = Proposal(cand(), cy, structures)
    assert p.best is None
    assert all(s.complete for s in structures)
    from tau.propose import _no_structure_reason

    assert "failed a constraint" in _no_structure_reason(cy, structures)


# --- the degraded-chain reproduction, from the code-health review's exp_a3.py ---
#
# A Black-Scholes ladder rather than this file's hand-set one, because the
# question is whether the *ranking* moves when quotes go missing, and that
# needs deltas and mids that stay mutually consistent as strikes are removed.

BS_SPOT, BS_IV, BS_DTE = 100.0, 0.30, 45
BS_STRIKES = [70, 75, 80, 82.5, 85, 87.5, 90, 92.5, 95, 97.5, 100,
              102.5, 105, 107.5, 110, 115, 120, 125]


def _bs(strike, option_type):
    """Mid and delta for one contract, under a flat 30% vol."""
    sigma = BS_IV * sqrt(BS_DTE / 365)
    d1 = (log(BS_SPOT / strike) + 0.5 * sigma * sigma) / sigma
    d2 = d1 - sigma

    def norm(x):
        return 0.5 * (1 + erf(x / sqrt(2)))

    if option_type is C:
        return BS_SPOT * norm(d1) - strike * norm(d2), norm(d1)
    return strike * norm(-d2) - BS_SPOT * norm(-d1), -norm(-d1)


def bs_ladder(unquoted=frozenset()):
    """The full ladder, with `unquoted` contracts carrying greeks but no
    market — which is exactly what `fetch_cycle` builds for a leg whose quote
    never arrived before the DXLink timeout."""
    legs = []
    for strike in BS_STRIKES:
        for option_type in (C, P):
            mid, delta = _bs(strike, option_type)
            quoted = (strike, option_type) not in unquoted
            legs.append(Leg(
                occ=f"{option_type}{strike:g}",
                streamer=f"s{option_type}{strike:g}",
                strike=float(strike),
                type=option_type,
                bid=mid - 0.02 if quoted else None,
                ask=mid + 0.02 if quoted else None,
                delta=delta,
                iv=BS_IV,
            ))
    return tuple(legs)


def test_a_dropout_no_longer_changes_which_structure_wins():
    """The review's exp_a3: the same market, priced twice, differing only in
    which contracts happened to quote before the timeout.

    Every put below 95 goes missing, which used to collapse the whole delta
    ladder onto the 95 strike and hand back the *first* label — a 29.5-delta
    contract shipped as `cash-secured-put · 16Δ`, ok=True, ranked above the
    fully quoted copy of the identical market. The winner must now be the same
    structure either way, and it must be the one whose label is true.
    """
    csp = (STRATEGIES["cash-secured-put"],)
    full_cycle = Cycle(
        symbol="FULL", expiration=date(2026, 9, 18), dte=BS_DTE,
        underlying=BS_SPOT, legs=bs_ladder(), fetched_at=datetime.now(UTC),
    )
    unquoted = {(k, P) for k in BS_STRIKES if k < 95}
    degraded_cycle = Cycle(
        symbol="DEGR", expiration=date(2026, 9, 18), dte=BS_DTE,
        underlying=BS_SPOT, legs=bs_ladder(unquoted), fetched_at=datetime.now(UTC),
    )
    full = propose_on(cand("FULL"), full_cycle, csp)
    degraded = propose_on(cand("DEGR"), degraded_cycle, csp)

    assert full.best is not None and degraded.best is not None
    # Same winner, same contract, same economics — the dropout cost the two
    # variants that could not be built honestly, and nothing else.
    assert degraded.best.variant == full.best.variant == "30Δ"
    assert degraded.best.legs[0].leg.strike == full.best.legs[0].leg.strike == 95.0
    assert degraded.best.annualized_roc == pytest.approx(full.best.annualized_roc)
    # and the label describes the contract it holds
    assert abs(degraded.best.legs[0].leg.delta) == pytest.approx(0.295, abs=1e-3)
    assert degraded.best.worst_off_target <= MAX_DELTA_MISS

    refused = [s for s in degraded.structures if not s.complete]
    assert {s.variant for s in refused} == {"16Δ", "20Δ"}


def test_rank_orders_by_metric_descending_failed_last():
    # One strategy only: across all six the credit-ranked winner may be a
    # broken wing that priced as a debit and has no credit at all, which is a
    # true reading of the structure and a useless one for ordering a test.
    only = (STRATEGIES["strangle"],)
    rich = cycle(
        tuple(
            leg(x.strike, x.type, x.delta, (x.bid + x.ask) / 2 * 3)
            for x in ladder()
        )
    )
    good_high = proposal("HIGH", rich, only)
    good_low = proposal("LOW", strategies=only)
    failed = Proposal(cand("FAIL"), error="boom")
    ranked = rank_proposals([good_low, failed, good_high], key="credit")
    assert [p.symbol for p in ranked] == ["HIGH", "LOW", "FAIL"]


def test_rank_default_key_is_annualized_roc():
    fast = proposal("FAST", cycle(dte=10))
    slow = proposal("SLOW", cycle(dte=90))
    ranked = rank_proposals([slow, fast])
    assert ranked[0].symbol == "FAST"  # same ROC, shorter DTE annualizes higher


def test_only_narrows_the_search_to_the_enabled_strategies():
    p = proposal()
    kept = p.only({"strangle", "iron-condor"})
    assert {s.strategy.name for s in kept.structures} == {"strangle", "iron-condor"}
    assert kept.best.strategy.name in {"strangle", "iron-condor"}
    # the full proposal is untouched: re-enabling costs nothing
    assert len(p.structures) > len(kept.structures)


def test_disabling_the_winner_promotes_the_runner_up():
    p = proposal()
    winner = p.best.strategy.name
    rest = {s.strategy.name for s in p.structures} - {winner}
    narrowed = p.only(rest)
    assert narrowed.best is not None
    assert narrowed.best.strategy.name != winner
    assert narrowed.annualized_roc <= p.annualized_roc


def test_only_with_every_strategy_enabled_is_the_same_proposal():
    p = proposal()
    assert p.only({s.strategy.name for s in p.structures}) is p
    assert p.only(None) is p


def test_only_nothing_enabled_reports_a_reason_rather_than_going_blank():
    p = proposal().only(set())
    assert not p.ok
    assert p.best is None
    assert p.error == "no strategy enabled"


def test_a_cycle_that_mostly_could_not_be_priced_says_so_too():
    """The mixed case: the delta gate refuses most of the ladder and the few
    that priced then fail a constraint. Reporting only the constraint tally
    would read as a market with nothing on offer, when the actual finding is a
    chain that did not arrive."""
    from tau.propose import _no_structure_reason

    # Only the 95 put quotes below spot, so two of the three requested deltas
    # cannot be built at all — and the third is then held under a pop floor
    # nothing on this chain can clear.
    coarse = (leg(95, P, -0.295, 2.07), leg(100, P, -0.50, 3.50),
              leg(100, C, 0.50, 3.50), leg(105, C, 0.30, 2.00))
    demanding = with_min_pop((STRATEGIES["cash-secured-put"],), 0.99)
    cy = cycle(coarse)
    structures = tuple(evaluate(demanding[0], cy))

    assert sum(s.complete for s in structures) == 1
    reason = _no_structure_reason(cy, structures)
    assert "failed a constraint" in reason
    assert "2 of 3 never priced" in reason
    assert "no strike near that delta" in reason
# --- broker buying-power enrichment ---


def test_bpr_defaults_to_the_formula_estimate():
    p = proposal()
    best = p.best
    assert best.broker_bpr is None
    assert best.bpr_source == "estimate"


def test_broker_bpr_overrides_the_formula_figure_and_flows_into_roc():
    from dataclasses import replace

    p = proposal()
    best = p.best
    enriched = replace(best, broker_bpr=2500.0)
    assert enriched.bpr == 2500.0
    assert enriched.bpr_source == "broker"
    # return on capital is computed from whichever figure `bpr` reports
    assert enriched.roc == pytest.approx(enriched.max_profit / 2500.0)


@pytest.mark.asyncio
async def test_enrichment_uses_the_broker_figure_when_the_dry_run_succeeds(monkeypatch):
    from tau import broker as broker_mod
    from tau import propose as propose_mod

    p = proposal()
    fake_account = object()
    calls = []

    async def fake_margin(session):
        return fake_account

    async def fake_bpr(session, account, structure):
        assert account is fake_account
        calls.append(structure)
        # a proportional figure keeps the ranked shortlist on top, so the
        # re-ranked winner is verifiably broker-priced
        return structure.bpr * 0.9 if structure.bpr else None

    monkeypatch.setattr(broker_mod, "margin_account", fake_margin)
    monkeypatch.setattr(broker_mod, "broker_bpr_for", fake_bpr)

    enriched = await propose_mod.enrich_with_broker_bpr(object(), p)
    assert enriched is not p
    complete = [s for s in p.variants() if s.complete]
    priced = min(propose_mod.BROKER_BPR_TOP, len(complete))
    # exactly the top of the ranked shortlist got the broker figure
    assert sum(1 for s in enriched.structures if s.bpr_source == "broker") == priced
    assert len(calls) == priced
    for s in enriched.structures:
        if s.bpr_source == "broker":
            assert s.bpr == pytest.approx(s.broker_bpr)
    # the structure that ranked first on the formula is among them, and the
    # untouched tail still reports the formula estimate
    top = complete[0]
    enriched_top = next(s for s in enriched.structures if s.label == top.label)
    assert enriched_top.bpr_source == "broker"
    assert enriched.best.bpr_source == "broker"


@pytest.mark.asyncio
async def test_enrichment_falls_back_when_the_account_cannot_be_read(monkeypatch):
    """A 403-style failure resolving the account must leave the proposal
    exactly as it was — same figures, same structure count, no crash."""
    from tau import broker as broker_mod
    from tau import propose as propose_mod

    async def fake_margin(session):
        raise RuntimeError("403: insufficient scopes")

    monkeypatch.setattr(broker_mod, "margin_account", fake_margin)

    p = proposal()
    enriched = await propose_mod.enrich_with_broker_bpr(object(), p)
    assert enriched is p
    assert all(s.bpr_source == "estimate" for s in enriched.structures)


@pytest.mark.asyncio
async def test_enrichment_falls_back_on_a_generic_exception(monkeypatch):
    """A dry-run that blows up mid-batch must not take the rest of the
    proposal with it: every structure keeps the formula estimate."""
    from tau import broker as broker_mod
    from tau import propose as propose_mod

    async def fake_margin(session):
        return object()

    async def fake_bpr(session, account, structure):
        raise ValueError("connection reset")

    monkeypatch.setattr(broker_mod, "margin_account", fake_margin)
    monkeypatch.setattr(broker_mod, "broker_bpr_for", fake_bpr)

    p = proposal()
    enriched = await propose_mod.enrich_with_broker_bpr(object(), p)
    assert all(s.bpr_source == "estimate" for s in enriched.structures)
    assert len(enriched.structures) == len(p.structures)


@pytest.mark.asyncio
async def test_enrichment_with_no_session_is_a_noop():
    """No credentials means no dry-run and no crash — the formula estimate
    is the whole story."""
    from tau import propose as propose_mod

    p = proposal()
    assert await propose_mod.enrich_with_broker_bpr(None, p) is p


@pytest.mark.asyncio
async def test_dry_runs_in_flight_are_capped_across_concurrent_batches(monkeypatch):
    """A rank pass enriches several symbols at once. The cap on dry-run
    POSTs in flight has to hold across those batches, not reset per batch —
    otherwise the burst is a multiple of the cap and the account API
    rate-limits it back down to the formula estimate."""
    import asyncio

    from tau import broker as broker_mod
    from tau import propose as propose_mod

    in_flight = 0
    peak = 0

    class FakeEffect:
        isolated_order_margin_requirement = 2500.0

    class FakeAccount:
        async def get_order_buying_power_effect(self, session, order):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0.01)
                return FakeEffect()
            finally:
                in_flight -= 1

    account = FakeAccount()

    async def fake_margin(session):
        return account

    monkeypatch.setattr(broker_mod, "margin_account", fake_margin)

    batches = [proposal(symbol=f"T{i}") for i in range(3)]
    assert all(
        len([s for s in p.variants() if s.complete]) > broker_mod.MAX_CONCURRENT
        for p in batches
    )
    enriched = await asyncio.gather(
        *(propose_mod.enrich_with_broker_bpr(object(), p) for p in batches)
    )
    assert peak <= broker_mod.MAX_CONCURRENT
    # and the figures still landed
    assert all(e.best.bpr_source == "broker" for e in enriched)
