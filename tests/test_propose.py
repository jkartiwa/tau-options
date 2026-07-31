from datetime import UTC, date, datetime

import pytest

from tau.chain import Cycle, Leg, Strangle
from tau.propose import (
    Proposal,
    naked_side_requirement,
    pop_between,
    rank_proposals,
    strangle_bpr,
)
from tau.screen import Candidate


def cand(symbol="TEST", iv30=30.0):
    return Candidate(
        symbol=symbol, ivr=50.0, ivp=50.0, iv30=iv30, hv30=25.0,
        liquidity=4, beta=1.0, earnings_date=None,
    )


def leg(strike, right, delta, bid, ask, iv=0.30):
    return Leg(
        occ=f"{right}{strike}", streamer=f"s{right}{strike}", strike=strike,
        right=right, bid=bid, ask=ask, delta=delta, iv=iv,
    )


def cycle(legs, underlying=100.0, dte=45):
    return Cycle(
        symbol="TEST", expiration=date(2026, 9, 4), dte=dte,
        underlying=underlying, legs=tuple(legs), fetched_at=datetime.now(UTC),
    )


PUT = leg(85, "P", -0.16, 1.0, 1.2)
CALL = leg(115, "C", 0.17, 0.8, 1.0)
CY = cycle((PUT, CALL))
STRANGLE = Strangle(PUT, CALL, target_delta=0.16)


def test_naked_requirement_uses_the_greater_of_two_formulas():
    # far OTM, cheap premium -> the 10%-of-strike floor should bind
    req = naked_side_requirement(spot=100.0, strike=50.0, premium=0.10)
    assert req == pytest.approx(max(
        (0.20 * 100 - 50 + 0.10) * 100,
        (0.10 * 50 + 0.10) * 100,
        50.0,
    ))


def test_naked_requirement_floor_applies_to_tiny_premium():
    req = naked_side_requirement(spot=10.0, strike=9.0, premium=0.01)
    assert req >= 50.0


def test_strangle_bpr_charges_larger_side_plus_other_premium():
    bpr = strangle_bpr(100.0, STRANGLE)
    put_req = naked_side_requirement(100.0, 85, 1.1)  # put mid
    call_req = naked_side_requirement(100.0, 115, 0.9)  # call mid
    if put_req >= call_req:
        expected = put_req + 0.9 * 100
    else:
        expected = call_req + 1.1 * 100
    assert bpr == pytest.approx(expected)


def test_bpr_none_when_strangle_incomplete():
    incomplete = Strangle(None, CALL, reason="no priced put leg with greeks")
    assert strangle_bpr(100.0, incomplete) is None


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


def test_proposal_roc_and_annualized():
    p = Proposal(cand(), CY, STRANGLE)
    assert p.ok
    assert p.credit == pytest.approx(2.0)  # put mid 1.1 + call mid 0.9
    bpr = strangle_bpr(100.0, STRANGLE)
    assert p.bpr == pytest.approx(bpr)
    assert p.roc == pytest.approx((2.0 * 100) / bpr)
    assert p.annualized_roc == pytest.approx(p.roc * 365 / 45)


def test_proposal_spread_cost_is_round_trip_over_credit():
    p = Proposal(cand(), CY, STRANGLE)
    total_spread = PUT.spread + CALL.spread
    assert p.spread_cost == pytest.approx(total_spread / STRANGLE.credit)


def test_proposal_reports_error_and_is_not_ok():
    p = Proposal(cand(), error="no option chain for XYZ")
    assert not p.ok
    assert p.credit is None
    assert p.bpr is None
    assert p.roc is None


def test_proposal_incomplete_strangle_is_not_ok():
    incomplete = Strangle(None, CALL, reason="no priced put leg with greeks")
    p = Proposal(cand(), CY, incomplete, error=incomplete.reason)
    assert not p.ok
    assert p.roc is None


def test_rank_orders_by_metric_descending_failed_last():
    good_high = Proposal(cand("HIGH"), cycle((leg(85,"P",-0.16,1.0,1.2), leg(115,"C",0.17,3.0,3.2))),
                          Strangle(leg(85,"P",-0.16,1.0,1.2), leg(115,"C",0.17,3.0,3.2), target_delta=0.16))
    good_low = Proposal(cand("LOW"), CY, STRANGLE)
    failed = Proposal(cand("FAIL"), error="boom")
    ranked = rank_proposals([good_low, failed, good_high], key="credit")
    assert [p.symbol for p in ranked] == ["HIGH", "LOW", "FAIL"]


def test_rank_default_key_is_annualized_roc():
    fast = Proposal(cand("FAST"), cycle((PUT, CALL), dte=10), STRANGLE)
    slow = Proposal(cand("SLOW"), cycle((PUT, CALL), dte=90), STRANGLE)
    ranked = rank_proposals([slow, fast])
    assert ranked[0].symbol == "FAST"  # same ROC, shorter DTE annualizes higher


def theta_leg(strike, right, delta, bid, ask, theta):
    return Leg(
        occ=f"{right}{strike}", streamer=f"s{right}{strike}", strike=strike,
        right=right, bid=bid, ask=ask, delta=delta, theta=theta, iv=0.30,
    )


def test_theta_is_positive_for_the_seller():
    """Greeks arrive signed for a long position, where a day passing is a
    loss. The seller is on the other side, so a structure that decays must
    report a positive number here — the sign is the whole point."""
    put = theta_leg(85, "P", -0.16, 1.0, 1.2, theta=-0.03)
    call = theta_leg(115, "C", 0.17, 1.0, 1.2, theta=-0.02)
    st = Strangle(put, call, target_delta=0.16)
    assert st.theta == pytest.approx(0.05)
    p = Proposal(cand(), cycle((put, call)), st)
    assert p.theta_day == pytest.approx(5.0)  # per contract, in dollars


def test_theta_yield_is_decay_over_capital():
    put = theta_leg(85, "P", -0.16, 1.0, 1.2, theta=-0.03)
    call = theta_leg(115, "C", 0.17, 1.0, 1.2, theta=-0.02)
    p = Proposal(cand(), cycle((put, call)), Strangle(put, call, target_delta=0.16))
    assert p.theta_yield == pytest.approx(p.theta_day / p.bpr)


def test_theta_is_none_when_a_leg_has_no_greeks():
    """Half a structure's decay is a wrong number, not an imprecise one —
    the same rule the credit follows."""
    put = theta_leg(85, "P", -0.16, 1.0, 1.2, theta=-0.03)
    call = leg(115, "C", 0.17, 1.0, 1.2)  # no theta
    st = Strangle(put, call, target_delta=0.16)
    assert st.theta is None
    assert Proposal(cand(), cycle((put, call)), st).theta_day is None
    assert Proposal(cand(), cycle((put, call)), st).theta_yield is None


def test_rank_by_theta_yield():
    rich = theta_leg(85, "P", -0.16, 1.0, 1.2, theta=-0.10)
    rich_call = theta_leg(115, "C", 0.17, 1.0, 1.2, theta=-0.10)
    thin = theta_leg(85, "P", -0.16, 1.0, 1.2, theta=-0.01)
    thin_call = theta_leg(115, "C", 0.17, 1.0, 1.2, theta=-0.01)
    fast = Proposal(cand("FAST"), cycle((rich, rich_call)),
                    Strangle(rich, rich_call, target_delta=0.16))
    slow = Proposal(cand("SLOW"), cycle((thin, thin_call)),
                    Strangle(thin, thin_call, target_delta=0.16))
    ranked = rank_proposals([slow, fast], key="theta_yield")
    assert [p.symbol for p in ranked] == ["FAST", "SLOW"]
