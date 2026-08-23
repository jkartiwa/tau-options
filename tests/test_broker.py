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
    broker_mod._account_retry_at = 0.0
    yield
    broker_mod._margin_account = False
    broker_mod._account_retry_at = 0.0


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
    `Debit` and dropping the field itself. A margin requirement is a debit, so
    the ordinary successful response arrives negative — reading that as
    garbage turns the whole feature off with nothing to show for it. The
    magnitude is the requirement whichever way it is signed."""
    from tau.broker import margin_requirement

    sdk_signed = SimpleNamespace(
        isolated_order_margin_requirement=Decimal("-3651.00")
    )
    assert margin_requirement(sdk_signed) == pytest.approx(3651.0)

    unsigned = SimpleNamespace(
        isolated_order_margin_requirement=Decimal("3651.00")
    )
    assert margin_requirement(unsigned) == pytest.approx(3651.0)


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


@pytest.mark.asyncio
async def test_the_breaker_re_enables_itself_after_the_cooldown(monkeypatch):
    """A TUI session runs for hours. One rough patch must not end broker
    pricing for the rest of it, so the trip is time-boxed and a single probe
    decides whether it stays."""
    import asyncio

    from tau import broker as broker_mod

    monkeypatch.setattr(broker_mod, "BREAKER_COOLDOWN", 0.05)

    class FakeEffect:
        isolated_order_margin_requirement = Decimal("-3651.00")

    failing = True
    attempts = []

    class FlakyAccount:
        async def get_order_buying_power_effect(self, session, order):
            attempts.append(order)
            if failing:
                raise RuntimeError("read timeout")
            return FakeEffect()

    account = FlakyAccount()
    for _ in range(broker_mod.MAX_CONSECUTIVE_FAILURES):
        assert await broker_bpr_for(None, account, strangle()) is None
    assert broker_mod.dry_runs_disabled()

    # held for the cooldown: no further calls reach the account
    spent = len(attempts)
    assert await broker_bpr_for(None, account, strangle()) is None
    assert len(attempts) == spent

    await asyncio.sleep(0.06)
    assert not broker_mod.dry_runs_disabled()
    failing = False
    assert await broker_bpr_for(None, account, strangle()) == pytest.approx(3651.0)
    assert not broker_mod.dry_runs_disabled()
    # the counter reset with it, so the next blip starts from zero
    assert broker_mod._consecutive_failures == 0


@pytest.mark.asyncio
async def test_a_failed_probe_trips_the_breaker_again(monkeypatch):
    import asyncio

    from tau import broker as broker_mod

    monkeypatch.setattr(broker_mod, "BREAKER_COOLDOWN", 0.05)

    attempts = []

    class DeadAccount:
        async def get_order_buying_power_effect(self, session, order):
            attempts.append(order)
            raise RuntimeError("read timeout")

    account = DeadAccount()
    for _ in range(broker_mod.MAX_CONSECUTIVE_FAILURES):
        assert await broker_bpr_for(None, account, strangle()) is None
    assert broker_mod.dry_runs_disabled()

    await asyncio.sleep(0.06)
    spent = len(attempts)
    assert await broker_bpr_for(None, account, strangle()) is None
    assert len(attempts) == spent + 1  # exactly one probe went out
    assert broker_mod.dry_runs_disabled()

    # and it is held again, not left open
    assert await broker_bpr_for(None, account, strangle()) is None
    assert len(attempts) == spent + 1


@pytest.mark.asyncio
async def test_only_one_probe_goes_out_after_a_cooldown(monkeypatch):
    """A rank pass has several dry-runs in flight at once. They must not all
    become probes the instant the cooldown lapses."""
    import asyncio

    from tau import broker as broker_mod

    monkeypatch.setattr(broker_mod, "BREAKER_COOLDOWN", 0.05)

    started = asyncio.Event()
    release = asyncio.Event()
    attempts = []

    class SlowAccount:
        async def get_order_buying_power_effect(self, session, order):
            attempts.append(order)
            if len(attempts) > broker_mod.MAX_CONSECUTIVE_FAILURES:
                started.set()
                await release.wait()
            raise RuntimeError("read timeout")

    account = SlowAccount()
    for _ in range(broker_mod.MAX_CONSECUTIVE_FAILURES):
        assert await broker_bpr_for(None, account, strangle()) is None
    assert broker_mod.dry_runs_disabled()

    await asyncio.sleep(0.06)
    probe = asyncio.ensure_future(broker_bpr_for(None, account, strangle()))
    await started.wait()
    spent = len(attempts)

    second = asyncio.ensure_future(broker_bpr_for(None, account, strangle()))
    _, pending = await asyncio.wait({second}, timeout=0.2)
    if pending:  # it queued behind the probe instead of standing down
        second.cancel()
        release.set()
        pytest.fail("a second caller probed while the first probe was out")
    assert second.result() is None
    assert len(attempts) == spent

    release.set()
    assert await probe is None


@pytest.mark.asyncio
async def test_a_transient_account_failure_is_retried_after_the_cooldown(monkeypatch):
    """A 429 or a read timeout on the account list says nothing about the
    token. Caching it as "no account" would end broker pricing for the rest of
    an hours-long session over a blip, on the one path a rank pass hits
    six-wide at its most concurrent moment — so the failure is time-boxed on
    the breaker's clock and then tried once more."""
    import asyncio

    from tau import broker as broker_mod

    monkeypatch.setattr(broker_mod, "BREAKER_COOLDOWN", 0.05)
    margin = SimpleNamespace(is_closed=False, margin_or_cash="Margin")
    calls = []

    async def flaky(session):
        calls.append(session)
        if len(calls) == 1:
            raise TimeoutError("read timeout")
        return [margin]

    monkeypatch.setattr(broker_mod.Account, "get", flaky)

    assert await margin_account(None) is None
    # and it says so: the marker the rank view reads is the same one answer
    assert broker_mod.dry_runs_disabled()

    # inside the cooldown the failure is remembered rather than re-attempted
    assert await margin_account(None) is None
    assert len(calls) == 1

    await asyncio.sleep(0.06)
    assert not broker_mod.dry_runs_disabled()
    assert await margin_account(None) is margin
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_positively_resolved_absence_of_a_margin_account_is_final(monkeypatch):
    """The account list answering with no open margin account is an answer,
    not a failure: the token and the account list are fixed for the process,
    so re-asking every batch would only stack requests."""
    from tau import broker as broker_mod

    calls = []

    async def fake_get(session):
        calls.append(session)
        return [SimpleNamespace(is_closed=False, margin_or_cash="Cash")]

    monkeypatch.setattr(broker_mod.Account, "get", fake_get)
    assert await margin_account(None) is None
    assert await margin_account(None) is None
    assert len(calls) == 1
    assert not broker_mod.dry_runs_disabled()


@pytest.mark.asyncio
async def test_the_trip_is_logged_once_when_the_failures_land_together(caplog):
    """The sequential loop hides this. `_claim_probe` short-circuits calls
    four and up, so only three failures are ever recorded there — but that is
    not the shape production has. A shortlist starts every dry-run at once, so
    all ten are past the breaker check before the first of them fails, and the
    counter keeps climbing after the trip. tau calls no `basicConfig`, so a
    line per failure goes to stderr through `logging.lastResort`, straight
    into the middle of `tau rank`'s table — ten deep per symbol, and a rank
    pass runs six symbols wide."""
    import asyncio
    import logging

    from tau import broker as broker_mod

    class DeadAccount:
        async def get_order_buying_power_effect(self, session, order):
            # yields, so every caller is past `_claim_probe` before any of
            # them fails — which is what makes them concurrent
            await asyncio.sleep(0)
            raise TimeoutError("read timeout")

    account = DeadAccount()
    with caplog.at_level(logging.WARNING, logger="tau.broker"):
        results = await asyncio.gather(
            *(broker_bpr_for(None, account, strangle()) for _ in range(10))
        )

    assert all(value is None for value in results)
    assert broker_mod._consecutive_failures > broker_mod.MAX_CONSECUTIVE_FAILURES
    assert broker_mod.dry_runs_disabled()
    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_a_failed_probe_after_the_cooldown_says_so_again(caplog):
    """The trip is announced on the way in, and a probe that fails after the
    cooldown is a new way in. Silencing that would leave a session with no
    record of anything past the first two minutes."""
    import asyncio
    import logging

    from tau import broker as broker_mod

    monkey = broker_mod.BREAKER_COOLDOWN
    broker_mod.BREAKER_COOLDOWN = 0.05
    try:
        class DeadAccount:
            async def get_order_buying_power_effect(self, session, order):
                raise TimeoutError("read timeout")

        account = DeadAccount()
        with caplog.at_level(logging.WARNING, logger="tau.broker"):
            for _ in range(5):
                assert await broker_bpr_for(None, account, strangle()) is None
            assert len(caplog.records) == 1

            await asyncio.sleep(0.06)
            assert not broker_mod.dry_runs_disabled()
            assert await broker_bpr_for(None, account, strangle()) is None
            assert broker_mod.dry_runs_disabled()
            assert len(caplog.records) == 2
    finally:
        broker_mod.BREAKER_COOLDOWN = monkey


class OnlyDryRun:
    """An account that answers the dry-run calculation and refuses everything
    else — `place_order`, `delete_order`, `replace_order` and any other
    attribute the SDK exposes all raise on the way in.

    The grant carries trading scope so the dry-run works, which means an
    accidental call really would reach the live order book. This account is
    how that stays testable rather than reviewable-by-eye.
    """

    def __init__(self):
        self.touched: list[str] = []

    async def get_order_buying_power_effect(self, session, order):
        self.touched.append("get_order_buying_power_effect")
        return SimpleNamespace(isolated_order_margin_requirement=Decimal("-3651.00"))

    def __getattr__(self, name):
        raise AssertionError(
            f"tau reached for Account.{name}; only the dry-run calculation "
            "is allowed on this token"
        )


@pytest.mark.asyncio
async def test_pricing_a_structure_touches_only_the_dry_run_calculation():
    account = OnlyDryRun()
    assert await broker_bpr_for(None, account, strangle()) == pytest.approx(3651.0)
    assert account.touched == ["get_order_buying_power_effect"]


@pytest.mark.asyncio
async def test_a_whole_proposal_is_priced_without_touching_the_order_book(monkeypatch):
    """The pipeline level: every structure in a proposal's shortlist gets a
    broker figure, and the account never sees anything but the calculation."""
    from tau import broker as broker_mod
    from tau import propose as propose_mod
    from tau.screen import Candidate

    cy = strangle().cycle
    strategy = Strategy(
        name="t-strangle",
        bias=Bias.NEUTRAL,
        legs=[
            LegSpec("short_put", type=P, side=SHORT, strike=Delta(0.20)),
            LegSpec("short_call", type=C, side=SHORT, strike=Delta(0.20)),
        ],
    )
    candidate = Candidate(
        symbol="TEST", ivr=None, ivp=None, iv30=None, hv30=None,
        liquidity=None, beta=None, earnings_date=None,
    )
    proposal = propose_mod.propose_on(candidate, cy, [strategy])
    account = OnlyDryRun()

    async def only_dry_run(session):
        return account

    monkeypatch.setattr(broker_mod, "margin_account", only_dry_run)

    enriched = await propose_mod.enrich_with_broker_bpr(object(), proposal)
    priced = [s for s in enriched.structures if s.bpr_source == "broker"]
    assert priced and all(s.bpr == pytest.approx(3651.0) for s in priced)
    assert set(account.touched) == {"get_order_buying_power_effect"}
