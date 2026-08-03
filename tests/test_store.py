import json
from datetime import UTC, date, datetime

import pytest

from tau import store
from tau.chain import Cycle, Leg
from tau.payoff import OptionType, Side
from tau.propose import Proposal, propose_on
from tau.screen import Candidate
from tau.strategies import STRATEGIES
from tau.strategy import Bias, Delta, LegSpec, Require, Strategy

C, P = OptionType.CALL, OptionType.PUT
SHORT = Side.SHORT

PUT_DELTAS = {80: -0.08, 85: -0.12, 90: -0.20, 95: -0.32, 100: -0.50}
CALL_DELTAS = {100: 0.50, 105: 0.30, 110: 0.20, 115: 0.12, 120: 0.08}
PUT_MIDS = {80: 0.50, 85: 0.80, 90: 1.20, 95: 2.00, 100: 3.50}
CALL_MIDS = {100: 3.50, 105: 2.00, 110: 1.20, 115: 0.80, 120: 0.50}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TAU_DATA_DIR", str(tmp_path))
    return tmp_path


def cand(symbol="TEST"):
    return Candidate(
        symbol=symbol, ivr=50.0, ivp=50.0, iv30=30.0, hv30=25.0,
        liquidity=4, beta=1.0, earnings_date=None,
    )


def leg(strike, option_type, delta, mid):
    return Leg(
        occ=f"{option_type}{strike:g}", streamer=f"s{option_type}{strike:g}",
        strike=float(strike), type=option_type,
        bid=mid - 0.01, ask=mid + 0.01, delta=delta, iv=0.30,
    )


def cycle(symbol="TEST", dte=45):
    legs = [leg(k, P, d, PUT_MIDS[k]) for k, d in PUT_DELTAS.items()]
    legs += [leg(k, C, d, CALL_MIDS[k]) for k, d in CALL_DELTAS.items()]
    return Cycle(
        symbol=symbol, expiration=date(2026, 9, 18), dte=dte, underlying=100.0,
        legs=tuple(legs), fetched_at=datetime.now(UTC),
    )


def rows(sql, *params):
    conn = store.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_identity_is_stable_for_the_same_definition():
    a = store.strategy_identity(STRATEGIES["strangle"])
    b = store.strategy_identity(STRATEGIES["strangle"])
    assert a == b


def test_identity_changes_when_a_leg_or_a_constraint_changes():
    base = Strategy(
        name="t", bias=Bias.NEUTRAL,
        legs=[LegSpec("p", type=P, side=SHORT, strike=Delta(0.16))],
        require=[Require("pop", ">=", 0.5)],
    )
    wider = Strategy(
        name="t", bias=Bias.NEUTRAL,
        legs=[LegSpec("p", type=P, side=SHORT, strike=Delta(0.30))],
        require=[Require("pop", ">=", 0.5)],
    )
    stricter = Strategy(
        name="t", bias=Bias.NEUTRAL,
        legs=[LegSpec("p", type=P, side=SHORT, strike=Delta(0.16))],
        require=[Require("pop", ">=", 0.7)],
    )
    digests = {store.strategy_identity(s)[1] for s in (base, wider, stricter)}
    assert len(digests) == 3


def test_a_pick_records_the_definition_and_the_variant_that_produced_it():
    """The whole reason the strategy layer is worth building: without this the
    corpus can never answer "how did 16-delta strangles do versus 30-delta
    jade lizards"."""
    p = propose_on(cand("SPY"), cycle("SPY"))
    scan_id = store.log_scan({"date": "2026-08-03"}, [cand("SPY")])
    assert store.log_picks(scan_id, [p]) == 1

    row = rows(
        "SELECT d.name, d.digest, k.symbol, k.variant, k.dte, k.annualized_roc, "
        "k.legs_json FROM pick k JOIN strategy_def d ON d.id = k.strategy_def_id"
    )[0]
    name, digest, symbol, variant, dte, ann, legs_json = row
    assert (name, symbol, dte) == (p.best.strategy.name, "SPY", 45)
    assert variant == p.best.variant
    assert ann == pytest.approx(p.best.annualized_roc)
    assert digest == store.strategy_identity(p.best.strategy)[1]
    legs = json.loads(legs_json)
    assert len(legs) == len(p.best.legs)
    assert {leg["strike"] for leg in legs} == {b.leg.strike for b in p.best.legs}


def test_a_definition_is_stored_once_across_scans():
    scan_a = store.log_scan({}, [])
    scan_b = store.log_scan({}, [])
    store.log_picks(scan_a, [propose_on(cand("A"), cycle("A"))])
    store.log_picks(scan_b, [propose_on(cand("B"), cycle("B"))])
    names = [r[0] for r in rows("SELECT name FROM strategy_def")]
    assert len(names) == len(set(names))
    assert len(rows("SELECT * FROM pick")) == 2


def test_a_symbol_that_priced_nothing_is_recorded_with_its_reason():
    """A name that had no tradable structure today is part of the record. An
    absent row would be indistinguishable from never having looked."""
    scan_id = store.log_scan({}, [])
    failed = Proposal(cand("XYZ"), error="no option chain for XYZ")
    assert store.log_picks(scan_id, [failed]) == 1
    row = rows("SELECT symbol, strategy_def_id, variant, error FROM pick")[0]
    assert row == ("XYZ", None, None, "no option chain for XYZ")


def test_an_open_profit_tail_is_stored_as_absent_not_as_a_number():
    """SQLite takes infinity happily and hands it back to queries that cannot
    reason about it."""
    assert store._finite(float("inf")) is None
    assert store._finite(-float("inf")) is None
    assert store._finite(3.0) == 3.0
