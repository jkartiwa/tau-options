from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest

from tau.chain import (
    Cycle,
    Leg,
    build_strangle,
    be_vs_expected_move,
    choose_expiration,
    select_strikes,
    strike_ladder,
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


# A ladder built so every component of tastytrade's weighted expected-move
# formula (ATM straddle, 1st and 2nd OTM strangle) is independently checkable:
# straddle@100 mid 6.00, wing1 (95p+105c) mid 3.00, wing2 (90p+110c) mid 1.20.
EM_LEGS = (
    leg(100, "C", 0.50, bid=2.95, ask=3.05, iv=0.30),
    leg(100, "P", -0.50, bid=2.90, ask=3.10, iv=0.32),
    leg(105, "C", 0.30, bid=1.45, ask=1.55, iv=0.29),
    leg(95, "P", -0.30, bid=1.45, ask=1.55, iv=0.31),
    leg(110, "C", 0.16, bid=0.55, ask=0.65, iv=0.28),
    leg(90, "P", -0.16, bid=0.55, ask=0.65, iv=0.33),
)


def test_atm_iv_blends_call_and_put_at_the_nearest_strike():
    cy = cycle(EM_LEGS, underlying=100.0)
    assert cy.atm_iv == pytest.approx((0.30 + 0.32) / 2)


def test_expected_move_uses_tastytrade_weighted_formula_when_wings_priced():
    cy = cycle(EM_LEGS, underlying=100.0)
    assert cy.expected_move == pytest.approx(0.6 * 6.0 + 0.3 * 3.0 + 0.1 * 1.2)
    assert cy.expected_move_method == "weighted"


def test_expected_move_falls_back_to_straddle_only_when_wings_missing():
    atm_only = EM_LEGS[:2]  # just the 100-strike call and put
    cy = cycle(atm_only, underlying=100.0)
    assert cy.expected_move == pytest.approx(6.0 * 0.85)
    assert cy.expected_move_method == "straddle×0.85"


def test_expected_move_is_none_when_no_strike_has_both_sides():
    # LEGS has puts at 80/85/90 and calls at 110/115/120 — no strike carries
    # both, so there is no straddle to anchor on.
    cy = cycle(LEGS, underlying=100.0)
    assert cy.expected_move is None
    assert cy.expected_move_method is None


def test_expected_move_is_none_when_atm_straddle_leg_unpriced():
    legs = (leg(100, "C", 0.50, bid=None, ask=None), leg(100, "P", -0.50))
    cy = cycle(legs, underlying=100.0)
    assert cy.expected_move is None


def test_be_vs_expected_move_uses_the_weighted_move():
    cy = cycle(EM_LEGS, underlying=100.0)
    st = build_strangle(cy)  # nearest-0.16Δ: put@90 (mid .60), call@110 (mid .60)
    assert (st.put.strike, st.call.strike) == (90, 110)
    # credit 1.20, breakevens (88.8, 111.2); nearer one is 11.2 away
    em = 0.6 * 6.0 + 0.3 * 3.0 + 0.1 * 1.2
    assert be_vs_expected_move(cy, st) == pytest.approx(11.2 / em)


def test_strike_ladder_groups_call_and_put_by_strike():
    ladder = strike_ladder(EM_LEGS)
    assert [row.strike for row in ladder] == [90, 95, 100, 105, 110]
    row100 = ladder[2]
    assert row100.call.right == "C" and row100.put.right == "P"


def test_strike_window_spans_the_wings_on_a_dense_ladder():
    """The original bug: a count cap stopped the window short of the 16-delta
    strikes on densely struck names."""
    strikes = [FakeStrike(s) for s in range(500, 900)]  # 400 one-point strikes
    sel = select_strikes(strikes, underlying=684.0, dte=40, iv_hint=0.194)
    prices = [s.strike_price for s in sel]
    # one sigma is ~45 points here; the window must reach well beyond it
    assert min(prices) <= 684 - 90
    assert max(prices) >= 684 + 90
    assert len(sel) <= 95  # still thinned enough to keep the pass fast


def test_strike_window_keeps_the_near_money_ladder_unbroken():
    """Multi-leg structures place a wing a fixed number of dollars from their
    short leg. Striding right through the money would delete the strike that
    wing points at, and the spread would come back narrower than its label."""
    from tau.chain import UNSTRIDED_CORE

    strikes = [FakeStrike(s) for s in range(500, 900)]
    sel = select_strikes(strikes, underlying=684.0, dte=40, iv_hint=0.194)
    prices = sorted(s.strike_price for s in sel)
    near = [p for p in prices if abs(p - 684) < UNSTRIDED_CORE]
    gaps = {b - a for a, b in zip(near, near[1:])}
    assert gaps == {1}, f"near-the-money ladder is not contiguous: {near}"


def test_strike_window_without_spot_falls_back_to_the_middle():
    strikes = [FakeStrike(s) for s in range(50, 150)]
    sel = select_strikes(strikes, underlying=None, dte=40)
    assert sel and len(sel) <= 90


@dataclass(frozen=True)
class FakeExpiration:
    expiration_date: date
    days_to_expiration: int
    expiration_type: str


@dataclass(frozen=True)
class FakeChain:
    expirations: tuple


def test_choose_expiration_excludes_weeklies():
    chain = FakeChain(
        expirations=(
            FakeExpiration(date(2026, 9, 4), 40, "Weekly"),  # closer to target
            FakeExpiration(date(2026, 9, 18), 54, "Regular"),
            FakeExpiration(date(2026, 10, 16), 82, "Regular"),
        )
    )
    exp = choose_expiration(chain, target_dte=45)
    assert exp.expiration_type == "Regular"
    assert exp.expiration_date == date(2026, 9, 18)


def test_choose_expiration_none_when_only_weeklies_available():
    chain = FakeChain(
        expirations=(FakeExpiration(date(2026, 9, 4), 40, "Weekly"),)
    )
    assert choose_expiration(chain, target_dte=45) is None
