"""The broker dry-run layer: order construction and the buying-power pull,
with every failure mode landing back on the formula estimate."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tau.broker import broker_bpr_for, margin_account, order_for
from tau.build import build
from tau.chain import Cycle, Leg
from tau.payoff import OptionType, Side
from tau.strategy import Bias, Delta, LegSpec, Strategy

C, P = OptionType.CALL, OptionType.PUT
SHORT = Side.SHORT


@pytest.fixture(autouse=True)
def _reset_account_cache():
    """`margin_account` resolves once per process; each test starts clean."""
    from tau import broker as broker_mod

    broker_mod._margin_account = False
    yield
    broker_mod._margin_account = False


def leg(strike, option_type, delta, mid):
    return Leg(
        occ=f"SPX 26OCT26{strike:g}",
        streamer=f"s{option_type}{strike:g}",
        strike=float(strike),
        type=option_type,
        bid=mid - 0.01,
        ask=mid + 0.01,
        delta=delta,
        iv=0.30,
    )


def strangle():
    """A short strangle at 20Δ/20Δ, net credit $2.40/share, priced and built
    through the real engine so the order construction is exercised on the
    real `Structure` type."""
    cy = Cycle(
        symbol="TEST",
        expiration=date(2026, 10, 16),
        dte=45,
        underlying=100.0,
        legs=(
            leg(90, P, -0.20, 1.20),
            leg(110, C, 0.20, 1.20),
        ),
        fetched_at=datetime.now(UTC),
    )
    strategy = Strategy(
        name="t-strangle",
        bias=Bias.NEUTRAL,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20)),
            LegSpec("short_call", type=C, side=SHORT, strike=Delta(0.20)),
        ],
    )
    label, specs = strategy.variants()[0]
    structure = build(strategy, label, specs, cy)
    assert structure.complete
    return structure


def test_order_for_builds_a_dry_run_limit_order():
    from tastytrade.order import InstrumentType, OrderAction, OrderTimeInForce

    order = order_for(strangle())
    assert order is not None
    assert order.price == Decimal("2.4")  # net credit per share
    assert order.time_in_force is OrderTimeInForce.DAY
    legs = order.legs
    assert [(leg.symbol, leg.action, leg.quantity) for leg in legs] == [
        ("SPX 26OCT2690", OrderAction.SELL_TO_OPEN, 1),
        ("SPX 26OCT26110", OrderAction.SELL_TO_OPEN, 1),
    ]
    assert all(leg.instrument_type is InstrumentType.EQUITY_OPTION for leg in legs)


def test_order_for_is_none_without_priced_legs():
    cy = Cycle(
        symbol="TEST",
        expiration=date(2026, 10, 16),
        dte=45,
        underlying=100.0,
        legs=(),
        fetched_at=datetime.now(UTC),
    )
    strategy = Strategy(
        name="t-none",
        bias=Bias.NEUTRAL,
        legs=[LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20))],
    )
    label, specs = strategy.variants()[0]
    assert order_for(build(strategy, label, specs, cy)) is None


@pytest.mark.asyncio
async def test_broker_bpr_uses_the_isolated_requirement():
    class FakeEffect:
        isolated_order_margin_requirement = Decimal("3651.00")
        change_in_buying_power = Decimal("-3980.00")

    calls = []

    class FakeAccount:
        async def get_order_buying_power_effect(self, session, order):
            calls.append(order)
            return FakeEffect()

    value = await broker_bpr_for(None, FakeAccount(), strangle())
    assert value == pytest.approx(3651.0)
    # the account-wide change (which would blend in offsets and premium flow)
    # is not what the formula claims to estimate
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_broker_bpr_falls_back_on_a_403_style_failure():
    class FakeAccount:
        async def get_order_buying_power_effect(self, session, order):
            raise RuntimeError("403: insufficient scopes")

    assert await broker_bpr_for(None, FakeAccount(), strangle()) is None


@pytest.mark.asyncio
async def test_broker_bpr_falls_back_on_a_generic_exception():
    class FakeAccount:
        async def get_order_buying_power_effect(self, session, order):
            raise ValueError("connection reset")

    assert await broker_bpr_for(None, FakeAccount(), strangle()) is None


@pytest.mark.asyncio
async def test_margin_account_picks_the_open_margin_account(monkeypatch):
    from tau import broker as broker_mod

    cash = SimpleNamespace(is_closed=False, margin_or_cash="Cash")
    margin = SimpleNamespace(is_closed=False, margin_or_cash="Margin")
    closed_margin = SimpleNamespace(is_closed=True, margin_or_cash="Margin")

    async def fake_get(session):
        return [cash, margin, closed_margin]

    monkeypatch.setattr(broker_mod.Account, "get", fake_get)
    assert await margin_account(None) is margin
    # resolved once per process: a second call must not hit the API again
    async def boom(session):
        raise AssertionError("account list fetched twice")

    monkeypatch.setattr(broker_mod.Account, "get", boom)
    assert await margin_account(None) is margin


@pytest.mark.asyncio
async def test_margin_account_falls_back_when_the_account_list_cannot_be_read(monkeypatch):
    from tau import broker as broker_mod

    async def fake_get(session):
        raise RuntimeError("403: insufficient scopes")

    monkeypatch.setattr(broker_mod.Account, "get", fake_get)
    assert await margin_account(None) is None


@pytest.mark.asyncio
async def test_margin_account_is_none_when_there_is_no_margin_account(monkeypatch):
    from tau import broker as broker_mod

    async def fake_get(session):
        return [SimpleNamespace(is_closed=False, margin_or_cash="Cash")]

    monkeypatch.setattr(broker_mod.Account, "get", fake_get)
    assert await margin_account(None) is None


def half_cent_put():
    """A single short put on a 2.115/2.135 market. A two-cent-wide quote puts
    the mid on a half cent, which is the ordinary case, not an exotic one."""
    cy = Cycle(
        symbol="TEST",
        expiration=date(2026, 10, 16),
        dte=45,
        underlying=100.0,
        legs=(leg(90, P, -0.20, 2.125),),
        fetched_at=datetime.now(UTC),
    )
    strategy = Strategy(
        name="t-csp",
        bias=Bias.NEUTRAL,
        legs=[LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20))],
    )
    label, specs = strategy.variants()[0]
    structure = build(strategy, label, specs, cy)
    assert structure.complete
    return structure


def test_order_price_is_rounded_to_the_broker_tick():
    """A sub-penny limit price is rejected by the broker with no
    buying-power body, which this module can only read as "no figure" — so
    the price that goes out has to sit on a whole cent."""
    structure = half_cent_put()
    assert structure.net_premium == pytest.approx(2.125)
    order = order_for(structure)
    assert order.price == Decimal("2.13")
    assert -order.price.as_tuple().exponent <= 2


@pytest.mark.asyncio
async def test_account_resolution_happens_once_under_concurrency(monkeypatch):
    """Six pipelines start at once at the top of a scan. They must produce
    one account-list request between them, not six."""
    import asyncio

    from tau import broker as broker_mod

    margin = SimpleNamespace(is_closed=False, margin_or_cash="Margin")
    calls = []

    async def fake_get(session):
        calls.append(session)
        await asyncio.sleep(0.01)
        return [margin]

    monkeypatch.setattr(broker_mod.Account, "get", fake_get)
    resolved = await asyncio.gather(*(margin_account(None) for _ in range(6)))
    assert calls == [None]
    assert all(a is margin for a in resolved)


def test_a_debit_signed_margin_requirement_is_read_as_the_requirement():
    """The API sends a magnitude beside an `-effect` field and the SDK folds
    the two together, rewriting the value to `-abs(value)` when the effect is
    `Debit`. A margin requirement is a debit, so the ordinary successful
    response arrives negative — reading that as garbage turns the whole
    feature off with nothing to show for it."""
    from tau.broker import margin_requirement

    sdk_signed = SimpleNamespace(
        isolated_order_margin_requirement=Decimal("-3651.00")
    )
    assert margin_requirement(sdk_signed) == pytest.approx(3651.0)

    spelled_out = SimpleNamespace(
        isolated_order_margin_requirement=Decimal("3651.00"),
        isolated_order_margin_requirement_effect="Debit",
    )
    assert margin_requirement(spelled_out) == pytest.approx(3651.0)


def test_a_missing_zero_or_unusable_margin_requirement_is_no_figure():
    from tau.broker import margin_requirement

    assert margin_requirement(SimpleNamespace()) is None
    assert margin_requirement(
        SimpleNamespace(isolated_order_margin_requirement=None)
    ) is None
    assert margin_requirement(
        SimpleNamespace(isolated_order_margin_requirement=Decimal("0"))
    ) is None
    assert margin_requirement(
        SimpleNamespace(isolated_order_margin_requirement="not a number")
    ) is None


@pytest.mark.asyncio
async def test_a_debit_signed_dry_run_produces_a_broker_figure():
    """End to end through the module's own entry point: the negative figure
    the SDK hands back is what a live dry-run looks like."""
    class FakeEffect:
        isolated_order_margin_requirement = Decimal("-3651.00")

    class FakeAccount:
        async def get_order_buying_power_effect(self, session, order):
            return FakeEffect()

    value = await broker_bpr_for(None, FakeAccount(), strangle())
    assert value == pytest.approx(3651.0)


@pytest.mark.asyncio
async def test_the_breaker_stops_calling_after_consecutive_failures(caplog):
    """Three failures in a row is a broker that is not answering. Every call
    after that is skipped, and the reason is said once."""
    import logging

    from tau import broker as broker_mod

    attempts = []

    class DeadAccount:
        async def get_order_buying_power_effect(self, session, order):
            attempts.append(order)
            raise TimeoutError("read timeout")

    account = DeadAccount()
    with caplog.at_level(logging.WARNING, logger="tau.broker"):
        for _ in range(10):
            assert await broker_bpr_for(None, account, strangle()) is None

    assert len(attempts) == broker_mod.MAX_CONSECUTIVE_FAILURES
    assert broker_mod.dry_runs_disabled()
    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_a_success_between_failures_keeps_the_breaker_closed():
    """The breaker counts *consecutive* failures: an API that answers most of
    the time is working, not broken."""
    from tau import broker as broker_mod

    class FakeEffect:
        isolated_order_margin_requirement = Decimal("-3651.00")

    outcomes = iter([None, None, FakeEffect(), None, None])

    class FlakyAccount:
        async def get_order_buying_power_effect(self, session, order):
            result = next(outcomes)
            if result is None:
                raise RuntimeError("connection reset")
            return result

    values = [
        await broker_bpr_for(None, FlakyAccount(), strangle()) for _ in range(5)
    ]
    assert values == [None, None, pytest.approx(3651.0), None, None]
    assert not broker_mod.dry_runs_disabled()
