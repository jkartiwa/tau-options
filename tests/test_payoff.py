from math import inf

import pytest

from tau.payoff import (
    OptionType,
    PayoffLeg,
    Side,
    bpr,
    breakevens,
    max_loss,
    max_profit,
    net_premium,
    payoff_at,
    pop_between,
    pop_over_intervals,
    profitable_intervals,
    slope_left,
    slope_right,
    worst_loss_down,
    worst_loss_up,
)

C, P = OptionType.CALL, OptionType.PUT
LONG, SHORT = Side.LONG, Side.SHORT


def leg(type_, side, strike, mid, qty=1):
    return PayoffLeg(type=type_, side=side, strike=strike, mid=mid, qty=qty)


# Short strangle, P90 / C110, 1.00 a side => 2.00 credit.
STRANGLE = (leg(P, SHORT, 90, 1.0), leg(C, SHORT, 110, 1.0))

# Put credit spread: short P90 @3.00, long P85 @1.00 => 2.00 credit, 5 wide.
PUT_SPREAD = (leg(P, SHORT, 90, 3.0), leg(P, LONG, 85, 1.0))

# Iron condor, 5 wide each side, 2.00 credit.
CONDOR = (
    leg(P, SHORT, 90, 2.0),
    leg(P, LONG, 85, 1.0),
    leg(C, SHORT, 110, 2.0),
    leg(C, LONG, 115, 1.0),
)

# Jade lizard: naked put plus a call spread whose credit exceeds its width,
# so there is no upside risk left. 5.50 credit against a 2-wide call spread.
LIZARD = (
    leg(P, SHORT, 90, 4.0),
    leg(C, SHORT, 110, 2.0),
    leg(C, LONG, 112, 0.5),
)

# Broken wing put butterfly: long 80, short two 90s, long 95. Credit 0.50,
# the wide wing carries the risk and the narrow one caps it.
BROKEN_WING = (
    leg(P, LONG, 80, 0.5),
    leg(P, SHORT, 90, 3.0, qty=2),
    leg(P, LONG, 95, 5.0),
)


def test_net_premium_is_positive_for_a_credit():
    assert net_premium(STRANGLE) == pytest.approx(2.0)
    assert net_premium(PUT_SPREAD) == pytest.approx(2.0)


def test_net_premium_is_negative_for_a_debit():
    debit = (leg(C, LONG, 100, 3.0), leg(C, SHORT, 110, 1.0))
    assert net_premium(debit) == pytest.approx(-2.0)


def test_strangle_payoff_and_breakevens():
    assert payoff_at(STRANGLE, 100) == pytest.approx(200.0)
    assert payoff_at(STRANGLE, 80) == pytest.approx(-800.0)
    assert breakevens(STRANGLE) == pytest.approx([88.0, 112.0])


def test_put_spread_payoff_and_max_loss():
    assert payoff_at(PUT_SPREAD, 100) == pytest.approx(200.0)
    assert payoff_at(PUT_SPREAD, 85) == pytest.approx(-300.0)
    # width 5 - credit 2
    assert max_loss(PUT_SPREAD) == pytest.approx(-300.0)
    assert max_profit(PUT_SPREAD) == pytest.approx(200.0)
    assert breakevens(PUT_SPREAD) == pytest.approx([88.0])


def test_open_tails_are_reported_by_slope():
    assert slope_right(STRANGLE) < 0  # open upside
    assert slope_left(STRANGLE) > 0  # open downside
    assert slope_right(CONDOR) == 0
    assert slope_left(CONDOR) == 0


def test_unbounded_upside_loss_is_infinite():
    assert max_loss(STRANGLE) == -inf
    assert worst_loss_up(STRANGLE, 100) == inf


def test_downside_worst_case_is_finite_because_price_stops_at_zero():
    # short P90 at 1.00: worst case is the whole strike less the credit
    assert worst_loss_down(STRANGLE, 100) == pytest.approx(8800.0)


def test_condor_is_defined_risk_both_ways():
    assert max_profit(CONDOR) == pytest.approx(200.0)
    assert max_loss(CONDOR) == pytest.approx(-300.0)
    assert worst_loss_up(CONDOR, 100) == pytest.approx(300.0)
    assert worst_loss_down(CONDOR, 100) == pytest.approx(300.0)
    assert breakevens(CONDOR) == pytest.approx([88.0, 112.0])


def test_condor_margin_is_one_width_not_two():
    """The claim the whole engine exists for: an engine that summed two
    verticals would charge 2x width and report half the true return."""
    assert bpr(CONDOR, 100.0) == pytest.approx(300.0)


def test_lizard_has_no_upside_risk():
    assert worst_loss_up(LIZARD, 100.0) == 0.0
    assert payoff_at(LIZARD, 1000) == pytest.approx(350.0)


def test_a_lizard_shaped_structure_that_is_not_a_lizard_is_caught():
    """Same shape, thinner credit: the call spread is 5 wide against 4.50 of
    total credit, so the upside loses. Only pricing can tell them apart."""
    not_a_lizard = (
        leg(P, SHORT, 90, 3.0),
        leg(C, SHORT, 110, 2.0),
        leg(C, LONG, 115, 0.5),
    )
    assert worst_loss_up(not_a_lizard, 100.0) == pytest.approx(50.0)


def test_lizard_margin_is_the_naked_put_plus_the_call_side_credit():
    # naked put at 90 with spot 100 and 4.00 premium: max(1400, 1300, 50)
    assert bpr(LIZARD, 100.0) == pytest.approx(1400.0 + 150.0)


def test_broken_wing_payoff_is_asymmetric_and_capped():
    assert payoff_at(BROKEN_WING, 90) == pytest.approx(550.0)
    assert payoff_at(BROKEN_WING, 95) == pytest.approx(50.0)
    assert payoff_at(BROKEN_WING, 200) == pytest.approx(50.0)
    assert max_profit(BROKEN_WING) == pytest.approx(550.0)
    assert max_loss(BROKEN_WING) == pytest.approx(-450.0)
    assert breakevens(BROKEN_WING) == pytest.approx([84.5])
    assert bpr(BROKEN_WING, 100.0) == pytest.approx(450.0)


def test_broken_wing_max_profit_sits_above_the_credit():
    """Why the return metric is max_profit/bpr and not credit/bpr: the credit
    is 0.50 but the structure can make 5.50."""
    assert net_premium(BROKEN_WING) == pytest.approx(0.5)
    assert max_profit(BROKEN_WING) == pytest.approx(550.0)


def test_strangle_margin_reduces_to_the_larger_side_plus_the_other_premium():
    # call side: max((0.20*100 - 10 + 1)*100, (0.10*110 + 1)*100) = 1200
    # put side:  max((0.20*100 - 10 + 1)*100, (0.10*90 + 1)*100)  = 1100
    assert bpr(STRANGLE, 100.0) == pytest.approx(1200.0 + 100.0)


def test_profitable_intervals_are_bounded_by_the_breakevens():
    assert profitable_intervals(STRANGLE) == [(88.0, 112.0)]
    assert profitable_intervals(PUT_SPREAD) == [(88.0, inf)]
    # the broken wing pays above its single breakeven, all the way up
    assert profitable_intervals(BROKEN_WING) == [(84.5, inf)]


def test_pop_over_intervals_matches_the_two_breakeven_form():
    over = pop_over_intervals([(88.0, 112.0)], 100.0, 0.30, 45)
    assert over == pytest.approx(pop_between(100.0, 88.0, 112.0, 0.30, 45))
    assert 0.0 < over < 1.0


def test_pop_of_an_open_ended_region_counts_the_whole_tail():
    pop = pop_over_intervals([(88.0, inf)], 100.0, 0.30, 45)
    assert pop > pop_over_intervals([(88.0, 112.0)], 100.0, 0.30, 45)
    assert pop < 1.0


def test_pop_is_none_on_degenerate_inputs():
    assert pop_over_intervals([(88.0, 112.0)], 100.0, 0.0, 45) is None
    assert pop_over_intervals([(88.0, 112.0)], 100.0, 0.30, 0) is None


def test_bpr_is_none_without_a_spot():
    assert bpr(STRANGLE, 0.0) is None
