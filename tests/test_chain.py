from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest

from tau.chain import (
    Cycle,
    Leg,
    build_strangle,
    be_vs_expected_move,
    select_strikes,
)


@dataclass(frozen=True)
class FakeStrike:
    strike_price: float
    call: str = "C"
    put: str = "P"
    call_streamer_symbol: str = "cs"
    put_streamer_symbol: str = "ps"


def leg(strike, right, delta, bid=1.0, ask=1.2, iv=0.30):
    return Leg(
        occ=f"{right}{strike}",
        streamer=f"s{right}{strike}",
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        delta=delta,
        iv=iv,
    )


def cycle(legs, underlying=100.0, dte=45):
    return Cycle(
        symbol="TEST",
        expiration=date(2026, 9, 4),
        dte=dte,
        underlying=underlying,
        legs=tuple(legs),
        fetched_at=datetime.now(UTC),
    )


LEGS = (
    leg(80, "P", -0.10),
    leg(85, "P", -0.16, bid=1.0, ask=1.4),
    leg(90, "P", -0.30),
    leg(110, "C", 0.30),
    leg(115, "C", 0.17, bid=0.8, ask=1.0),
    leg(120, "C", 0.09),
)


def test_picks_nearest_target_delta_each_side():
    st = build_strangle(cycle(LEGS))
    assert (st.put.strike, st.call.strike) == (85, 115)
    assert st.complete


def test_credit_and_breakevens():
    st = build_strangle(cycle(LEGS))
    assert st.credit == pytest.approx(2.1)
    lower, upper = st.breakevens
    assert (lower, upper) == pytest.approx((82.9, 117.1))
    assert st.worst_spread == pytest.approx(0.4)


def test_unpriced_leg_is_never_partially_credited():
    legs = (leg(85, "P", -0.16, bid=None, ask=None), leg(115, "C", 0.17))
    st = build_strangle(cycle(legs))
    assert not st.complete
    assert st.credit is None
    assert "put" in st.reason


def test_leg_without_greeks_is_not_selectable():
    legs = (
        Leg(occ="P85", streamer="s", strike=85, right="P", bid=1.0, ask=1.2),
        leg(115, "C", 0.17),
    )
    st = build_strangle(cycle(legs))
    assert not st.complete


def test_off_target_reports_the_miss():
    coarse = (leg(70, "P", -0.40), leg(130, "C", 0.38))
    st = build_strangle(cycle(coarse))
    assert st.complete
    assert round(st.off_target, 2) == 0.24  # 0.40 vs 0.16


def test_expected_move_and_be_ratio():
    cy = cycle(LEGS, underlying=100.0, dte=365)  # one year, iv 0.30 -> 30 pts
    assert round(cy.expected_move, 2) == 30.0
    st = build_strangle(cy)
    # nearest breakeven is 85 - 2.1 = 82.9, i.e. 17.1 points away
    assert round(be_vs_expected_move(cy, st), 3) == round(17.1 / 30.0, 3)


def test_strike_window_spans_the_wings_on_a_dense_ladder():
    """The original bug: a count cap stopped the window short of the 16-delta
    strikes on densely struck names."""
    strikes = [FakeStrike(s) for s in range(500, 900)]  # 400 one-point strikes
    sel = select_strikes(strikes, underlying=684.0, dte=40, iv_hint=0.194)
    prices = [s.strike_price for s in sel]
    # one sigma is ~45 points here; the window must reach well beyond it
    assert min(prices) <= 684 - 90
    assert max(prices) >= 684 + 90
    assert len(sel) <= 55  # still thinned enough to keep the pass fast


def test_strike_window_without_spot_falls_back_to_the_middle():
    strikes = [FakeStrike(s) for s in range(50, 150)]
    sel = select_strikes(strikes, underlying=None, dte=40)
    assert sel and len(sel) <= 52
