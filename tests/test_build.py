from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from tau.build import MAX_REF_MISS, best, build, evaluate, rank
from tau.chain import Cycle, Leg
from tau.payoff import OptionType, Side, pop_over_intervals, profitable_intervals
from tau.strategies import STRATEGIES
from tau.strategy import Bias, Delta, LegSpec, Ref, Require, Strategy

C, P = OptionType.CALL, OptionType.PUT
LONG, SHORT = Side.LONG, Side.SHORT

# A 5-point ladder around a spot of 100, with deltas that fall away
# symmetrically and mids that make each structure hand-checkable.
PUT_DELTAS = {80: -0.08, 85: -0.12, 90: -0.20, 95: -0.32, 100: -0.50}
CALL_DELTAS = {100: 0.50, 105: 0.30, 110: 0.20, 115: 0.12, 120: 0.08}
PUT_MIDS = {80: 0.50, 85: 0.80, 90: 1.20, 95: 2.00, 100: 3.50}
CALL_MIDS = {100: 3.50, 105: 2.00, 110: 1.20, 115: 0.80, 120: 0.50}
SPREAD = 0.02  # tight enough that the shipped spread_cost constraints pass


def leg(strike, option_type, delta, mid, spread=SPREAD, iv=0.30):
    return Leg(
        occ=f"{option_type}{strike:g}",
        streamer=f"s{option_type}{strike:g}",
        strike=float(strike),
        type=option_type,
        bid=mid - spread / 2,
        ask=mid + spread / 2,
        delta=delta,
        iv=iv,
    )


def ladder():
    legs = [leg(k, P, d, PUT_MIDS[k]) for k, d in PUT_DELTAS.items()]
    legs += [leg(k, C, d, CALL_MIDS[k]) for k, d in CALL_DELTAS.items()]
    return tuple(legs)


# The same ladder with a realistic equity smile laid over it: puts bid over
# calls in the wings, both sides meeting at 30% on the 100 strike so `atm_iv`
# is unchanged at 0.30 and only the *local* vols differ.
PUT_IVS = {80: 0.40, 85: 0.37, 90: 0.345, 95: 0.32, 100: 0.30}
CALL_IVS = {100: 0.30, 105: 0.29, 110: 0.28, 115: 0.275, 120: 0.27}


def skewed_ladder():
    legs = [leg(k, P, d, PUT_MIDS[k], iv=PUT_IVS[k]) for k, d in PUT_DELTAS.items()]
    legs += [leg(k, C, d, CALL_MIDS[k], iv=CALL_IVS[k]) for k, d in CALL_DELTAS.items()]
    return tuple(legs)


def cycle(legs=None, underlying=100.0, dte=45):
    return Cycle(
        symbol="TEST",
        expiration=date(2026, 9, 18),
        dte=dte,
        underlying=underlying,
        legs=legs if legs is not None else ladder(),
        fetched_at=datetime.now(UTC),
    )


def one(strategy, cy, variant):
    """The single named variant of a strategy, built against a cycle."""
    for label, specs in strategy.variants():
        if label == variant:
            return build(strategy, label, specs, cy)
    raise AssertionError(f"no variant {variant!r} in {strategy.name}")


STRANGLE_20 = Strategy(
    name="t-strangle",
    bias=Bias.NEUTRAL,
    legs=[
        LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20)),
        LegSpec("short_call", type=C, side=SHORT, strike=Delta(0.20)),
    ],
)


def test_delta_selection_picks_the_nearest_strike_and_reports_no_miss():
    structure = one(STRANGLE_20, cycle(), "20Δ/20Δ")
    assert structure.complete
    assert [b.leg.strike for b in structure.legs] == [90.0, 110.0]
    assert structure.worst_off_target == pytest.approx(0.0)
    assert structure.credit == pytest.approx(2.40)


def test_delta_miss_is_reported_not_folded_into_the_label():
    """The original bug in this codebase: a 0.38-delta leg returned labelled
    16-delta. A coarse ladder must stay visible."""
    coarse = (leg(70, P, -0.40, 1.0), leg(130, C, 0.38, 1.0))
    structure = one(STRANGLE_20, cycle(coarse), "20Δ/20Δ")
    assert structure.complete
    assert structure.worst_off_target == pytest.approx(0.20)


def test_reference_leg_resolves_by_dollar_offset():
    vertical = Strategy(
        name="t-vertical",
        bias=Bias.BULLISH,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20)),
            LegSpec("long_put", type=P, side=LONG, strike=Ref("short_put", offset=-5)),
        ],
    )
    structure = one(vertical, cycle(), "20Δ-5")
    assert [b.leg.strike for b in structure.legs] == [90.0, 85.0]
    assert structure.credit == pytest.approx(0.40)
    assert structure.max_loss == pytest.approx(-460.0)
    assert structure.bpr == pytest.approx(460.0)


def test_reference_leg_resolves_by_strike_count():
    vertical = Strategy(
        name="t-vertical-k",
        bias=Bias.BULLISH,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20)),
            LegSpec("long_put", type=P, side=LONG, strike=Ref("short_put", strikes=-2)),
        ],
    )
    structure = one(vertical, cycle(), "20Δ-2k")
    assert [b.leg.strike for b in structure.legs] == [90.0, 80.0]


def test_a_coarse_ladder_kills_the_variant_rather_than_mislabelling_the_width():
    """Same failure shape as the delta bug: asking for a 5-wide wing and
    silently getting a 10-wide one changes the margin and the max loss."""
    wide = (
        leg(80, P, -0.08, 0.50),
        leg(90, P, -0.20, 1.20),
        leg(100, P, -0.50, 3.50),
        leg(110, C, 0.20, 1.20),
    )
    vertical = Strategy(
        name="t-vertical",
        bias=Bias.BULLISH,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20)),
            LegSpec("long_put", type=P, side=LONG, strike=Ref("short_put", offset=-5)),
        ],
    )
    structure = one(vertical, cycle(wide), "20Δ-5")
    assert not structure.complete
    assert "ladder too coarse" in structure.reason
    assert structure.credit is None


def test_reference_running_off_the_ladder_is_a_reason_not_a_crash():
    vertical = Strategy(
        name="t-vertical-k",
        bias=Bias.BULLISH,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20)),
            LegSpec("long_put", type=P, side=LONG, strike=Ref("short_put", strikes=-9)),
        ],
    )
    structure = one(vertical, cycle(), "20Δ-9k")
    assert not structure.complete
    assert "runs off the ladder" in structure.reason


def test_an_unpriced_leg_never_yields_a_partial_credit():
    stripped = tuple(
        Leg(occ=x.occ, streamer=x.streamer, strike=x.strike, type=x.type, delta=x.delta)
        if x.type is C
        else x
        for x in ladder()
    )
    structure = one(STRANGLE_20, cycle(stripped), "20Δ/20Δ")
    assert not structure.complete
    assert "no priced call leg" in structure.reason
    assert structure.credit is None and structure.bpr is None


def test_a_leg_without_greeks_is_not_selectable():
    no_greeks = tuple(
        Leg(occ=x.occ, streamer=x.streamer, strike=x.strike, type=x.type,
            bid=x.bid, ask=x.ask)
        if x.type is P
        else x
        for x in ladder()
    )
    structure = one(STRANGLE_20, cycle(no_greeks), "20Δ/20Δ")
    assert not structure.complete
    assert "no priced put leg" in structure.reason


def test_condor_is_charged_one_width_end_to_end():
    """The claim the payoff engine exists for, all the way through
    resolution: two verticals charged separately would be 840."""
    structure = one(STRATEGIES["iron-condor"], cycle(), "20Δ-5/20Δ+5")
    assert [b.leg.strike for b in structure.legs] == [90.0, 85.0, 110.0, 115.0]
    assert structure.credit == pytest.approx(0.80)
    assert structure.bpr == pytest.approx(420.0)
    assert structure.max_profit == pytest.approx(80.0)
    assert structure.ok


def test_a_failed_constraint_is_kept_with_its_reason():
    """No lizard on this ladder today — and that is information, not a
    missing row."""
    structure = one(STRATEGIES["jade-lizard"], cycle(), "20Δ/20Δ+5")
    assert structure.complete
    assert not structure.ok
    assert structure.worst_loss_up == pytest.approx(340.0)
    assert "worst_loss_up 340 not <= 0" in structure.failures[0].reason


def test_a_lizard_passes_when_the_credit_actually_covers_the_wing():
    """The defining property is a pricing outcome: same legs, richer calls."""
    skewed = (
        leg(90, P, -0.20, 3.00),
        leg(100, P, -0.50, 6.00),
        leg(100, C, 0.50, 6.50),
        leg(110, C, 0.20, 3.00),
        leg(115, C, 0.12, 0.50),
    )
    structure = one(STRATEGIES["jade-lizard"], cycle(skewed), "20Δ/20Δ+5")
    assert structure.worst_loss_up == 0.0
    assert structure.ok
    # naked put at 90 with spot 100 and 3.00 premium, plus the call credit
    assert structure.bpr == pytest.approx(1300.0 + 250.0)


def test_spread_cost_counts_every_leg_it_has_to_cross():
    structure = one(STRATEGIES["iron-condor"], cycle(), "20Δ-5/20Δ+5")
    assert structure.spread_cost == pytest.approx(4 * SPREAD / 0.80)


def test_spread_cost_weights_a_doubled_leg_twice():
    fly = Strategy(
        name="t-fly",
        bias=Bias.BULLISH,
        legs=[
            LegSpec("body", type=P, side=SHORT, strike=Delta(0.32), qty=2),
            LegSpec("near", type=P, side=LONG, strike=Ref("body", offset=5)),
            LegSpec("far", type=P, side=LONG, strike=Ref("body", offset=-10)),
        ],
    )
    structure = one(fly, cycle(), "32Δ+5-10")
    assert structure.leg_count == 4
    # two body contracts and one of each wing: four markets to cross
    assert structure.spread_cost == pytest.approx(
        4 * SPREAD / abs(structure.net_premium)
    )


def test_a_structure_that_costs_too_much_to_cross_is_failed_not_ranked():
    """The live AAPL case: two legs cost ~99% of the credit to cross. On a
    four-legger it is worse, and return on capital cannot see it."""
    wide = tuple(
        leg(x.strike, x.type, x.delta, (x.bid + x.ask) / 2, spread=0.30)
        for x in ladder()
    )
    structure = one(STRATEGIES["iron-condor"], cycle(wide), "20Δ-5/20Δ+5")
    assert structure.complete
    assert not structure.ok
    assert "spread_cost" in structure.failures[0].reason


def test_a_zero_premium_structure_reports_no_spread_share_rather_than_dividing():
    fly = Strategy(
        name="t-flat-fly",
        bias=Bias.BULLISH,
        legs=[
            LegSpec("body", type=P, side=SHORT, strike=Delta(0.32), qty=2),
            LegSpec("near", type=P, side=LONG, strike=Ref("body", offset=5)),
            LegSpec("far", type=P, side=LONG, strike=Ref("body", offset=-15)),
        ],
        require=[Require("spread_cost", "<=", 0.25)],
    )
    structure = one(fly, cycle(), "32Δ+5-15")
    assert structure.net_premium == pytest.approx(0.0)
    assert structure.spread_cost is None
    assert "spread_cost unavailable" in structure.failures[0].reason


def test_evaluate_returns_every_variant_including_the_failures():
    structures = evaluate(STRATEGIES["jade-lizard"], cycle())
    assert len(structures) == STRATEGIES["jade-lizard"].variant_count
    assert any(not s.ok for s in structures)


def test_variants_that_resolve_to_the_same_contracts_collapse_to_one():
    """A coarse ladder maps several requested widths onto one strike. Two
    rows for one trade means one of the labels is wrong."""
    strategy = Strategy(
        name="t-vertical-wide",
        bias=Bias.BULLISH,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20)),
            # -10 and -11 both land on the 80 strike of a 5-point ladder
            LegSpec(
                "long_put", type=P, side=LONG, strike=Ref("short_put", offset=[-10, -11])
            ),
        ],
    )
    assert strategy.variant_count == 2
    structures = evaluate(strategy, cycle())
    assert len(structures) == 1
    # the surviving row is the one that asked for closest to what it got
    assert structures[0].variant == "20Δ-10"


def test_best_never_returns_a_variant_that_failed_a_constraint():
    structures = evaluate(STRATEGIES["jade-lizard"], cycle())
    assert all(not s.ok for s in structures)
    assert best(structures) is None


def test_rank_keeps_failures_at_the_back_rather_than_dropping_them():
    structures = evaluate(STRATEGIES["iron-condor"], cycle())
    ordered = rank(structures)
    assert len(ordered) == len(structures)
    passing = [s for s in ordered if s.ok]
    assert ordered[: len(passing)] == passing
    values = [s.annualized_roc for s in passing]
    assert values == sorted(values, reverse=True)


def test_return_is_max_profit_over_capital_not_credit_over_capital():
    structure = one(STRATEGIES["iron-condor"], cycle(), "20Δ-5/20Δ+5")
    assert structure.roc == pytest.approx(80.0 / 420.0)
    assert structure.annualized_roc == pytest.approx(structure.roc * 365 / 45)


def test_metrics_are_none_rather_than_wrong_without_a_spot():
    structure = one(STRANGLE_20, cycle(underlying=None), "20Δ/20Δ")
    assert structure.complete
    assert structure.bpr is None
    assert structure.roc is None
    assert structure.worst_loss_up is None


def test_max_ref_miss_is_a_fraction_of_the_requested_offset():
    assert 0 < MAX_REF_MISS < 1


def test_best_never_returns_a_variant_that_fails_the_pop_floor():
    """A wider/higher-delta strangle carries more credit and a higher
    annualized_roc, but a lower pop. Ranked on annualized_roc alone, `best`
    would pick the worse-odds variant; the pop floor must stop it."""
    strategy = Strategy(
        name="t-pop-gate",
        bias=Bias.NEUTRAL,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta([0.08, 0.32])),
            LegSpec("short_call", type=C, side=SHORT, strike=Delta([0.08, 0.32])),
        ],
        require=[Require("pop", ">=", 0.70)],
    )
    structures = evaluate(strategy, cycle())
    naive_top = max(structures, key=lambda s: s.annualized_roc)
    assert naive_top.pop < 0.70  # the naive top-ann% pick fails the floor
    assert not naive_top.ok

    winner = best(structures)
    assert winner is not None
    assert winner.pop >= 0.70
    assert winner.annualized_roc < naive_top.annualized_roc


def test_a_flat_smile_leaves_pop_on_the_atm_number():
    """Requirement: a symmetric chain must be unchanged. The default ladder
    quotes 30% on every strike, so the per-boundary read has to land exactly
    where the single-ATM-vol read did."""
    structure = one(STRANGLE_20, cycle(), "20Δ/20Δ")
    cy = structure.cycle
    assert cy.atm_iv == pytest.approx(0.30)
    assert structure.pop == pop_over_intervals(
        profitable_intervals(structure.payoff_legs), 100.0, 0.30, 45
    )


def test_put_over_call_skew_pulls_pop_below_the_atm_only_number():
    """Requirement: a skewed chain must report a lower POP. Same strikes,
    same credit, same `atm_iv` — only the wings' own vols differ, and the
    fatter downside has to show up as worse odds."""
    structure = one(STRANGLE_20, cycle(legs=skewed_ladder()), "20Δ/20Δ")
    assert structure.cycle.atm_iv == pytest.approx(0.30)  # unchanged by the smile
    atm_only = pop_over_intervals(
        profitable_intervals(structure.payoff_legs), 100.0, 0.30, 45
    )
    assert structure.pop < atm_only
    # 87.60 breakeven off a 35.7% put, 112.40 off a 27.76% call
    assert structure.pop == pytest.approx(0.7337, abs=5e-4)
    assert atm_only == pytest.approx(0.7632, abs=5e-4)


def test_pop_falls_back_to_atm_when_only_the_atm_strike_carries_iv():
    """Degrade, never fail: strip every leg's own IV except the 100 strike,
    so each side quotes a single point and flat-extrapolates it out to both
    breakevens. That point is the ATM vol, so pop must still come back on the
    ATM number rather than None."""
    legs = tuple(
        replace(built, iv=0.30) if built.strike == 100.0 else replace(built, iv=None)
        for built in ladder()
    )
    structure = one(STRANGLE_20, cycle(legs=legs), "20Δ/20Δ")
    assert structure.cycle.atm_iv == pytest.approx(0.30)
    assert structure.pop == pop_over_intervals(
        profitable_intervals(structure.payoff_legs), 100.0, 0.30, 45
    )


def test_be_over_em_measures_the_nearer_breakeven_in_expected_moves():
    """Moved down from the strangle-era chain tests: the same read, now
    derived from the payoff's breakevens rather than a structure that knew
    it had exactly two."""
    cy = cycle()
    # straddle 7.00, 1st OTM strangle 4.00, 2nd 2.40, weighted 60/30/10
    em = 0.6 * 7.00 + 0.3 * 4.00 + 0.1 * 2.40
    assert cy.expected_move == pytest.approx(em)
    structure = one(STRANGLE_20, cy, "20Δ/20Δ")
    # 90/110 wings on a 2.40 credit break even at 87.60 and 112.40
    assert structure.breakevens == pytest.approx([87.60, 112.40])
    assert structure.be_over_em == pytest.approx(12.40 / em)
