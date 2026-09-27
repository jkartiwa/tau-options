"""The broker's own buying-power figure, via the order dry-run calculation.

`payoff.bpr()` is a formula estimate. This module asks the broker instead: one
POST per structure to the account's order dry-run endpoint
(`/accounts/{id}/orders/dry-run`), which calculates an order's buying-power
effect without placing anything. That endpoint is the only one called here,
which matters because the token is allowed to place live orders.

On a portfolio-margin account the formula's naked-margin model is the wrong
model, so the broker's figure wins whenever there is one. Any failure (missing
scope, network error, timeout, rate limit, SDK exception) falls back to the
formula estimate without raising.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from weakref import WeakKeyDictionary

from tastytrade import Session
from tastytrade.account import Account
from tastytrade.order import (
    BuyingPowerEffect,
    InstrumentType,
    Leg,
    LimitOrder,
    OrderAction,
    OrderTimeInForce,
)

from tau.build import Structure
from tau.payoff import Side

log = logging.getLogger(__name__)

# Dry-run POSTs in flight per event loop, not per batch: a rank pass prices
# several symbols concurrently, and the account API rate-limits.
MAX_CONCURRENT = 4

# The smallest price increment the broker accepts on a limit order. The net
# premium is a sum of float mids, and an order priced at 1.0749999999999997 is
# rejected outright, so the limit is rounded to this.
TICK = Decimal("0.01")

# Consecutive dry-run failures that stop broker pricing for a while. Without
# it, a broker that times out every POST makes each symbol wait out the full
# read timeout, and the stall grows with the shortlist.
MAX_CONSECUTIVE_FAILURES = 3

# How long a trip lasts. The SDK reads the dry-run response without
# `validate_response`, so a rate limit arrives as a `KeyError` or a JSON decode
# error, indistinguishable from any other failure. Recovering on a clock
# instead of classifying errors keeps a long TUI session from losing broker
# pricing to one bad patch. After the cooldown a single probe decides whether
# to re-enable or trip again.
BREAKER_COOLDOWN = 120.0

# The cancel message a caller's time budget uses. A dry run cut off by the
# budget is a broker that did not answer in time, so it counts toward the
# breaker like a read timeout would (`CancelledError` is not an `Exception`,
# so it would otherwise go uncounted). Any other cancellation, such as Ctrl-C
# or a TUI worker shutting down, is re-raised untouched.
BUDGET_EXPIRED = "tau: broker pull budget expired"


def cancel_for_budget(task: asyncio.Task) -> None:
    """Cancel a dry-run task so its failure counts toward the breaker."""
    task.cancel(BUDGET_EXPIRED)


class _LoopGate:
    """The asyncio primitives shared by everything running on one event loop.

    A lock cannot be awaited from a loop other than its own, so these are kept
    per loop (weakly keyed) rather than as module-level singletons.
    """

    def __init__(self) -> None:
        self.dry_run = asyncio.Semaphore(MAX_CONCURRENT)
        self.resolving = asyncio.Lock()


_gates: WeakKeyDictionary = WeakKeyDictionary()


def _gate() -> _LoopGate:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _LoopGate()
        _gates[loop] = gate
    return gate


@dataclass
class _State:
    """What the process has learned about the account and the broker's health.

    Process-wide, because neither varies by symbol. `margin_account` is
    meaningful once `account_resolved` is set: an `Account`, or `None` when
    there is no open margin account. An answer from the API is
    cached for the life of the process, since the account list does not change
    under a fixed token; a failed request is not an answer, so it is held until
    `account_retry_at` and then retried. The breaker counts consecutive
    failures, trips until `tripped_until` (0.0 = not tripped), and lets one
    `probing` call through once the cooldown is up.
    """

    account_resolved: bool = False
    margin_account: Account | None = None
    account_retry_at: float = 0.0
    consecutive_failures: int = 0
    tripped_until: float = 0.0
    probing: bool = False


_state = _State()


def dry_runs_disabled() -> bool:
    """Whether broker pricing is held back by a tripped breaker or a failed
    account lookup.

    Callers use this to skip the pull entirely, and the UI to mark the figures
    as estimates. It turns False once the cooldown is up, and the next call
    decides whether it stays that way.
    """
    return _breaker_holding() or time.monotonic() < _state.account_retry_at


def _breaker_holding() -> bool:
    return _state.tripped_until > 0.0 and time.monotonic() < _state.tripped_until


def _claim_probe() -> bool | None:
    """`None` when the breaker is holding, `True` for the one caller allowed
    to probe after a cooldown, `False` for an ordinary call."""
    if _state.tripped_until <= 0.0:
        return False
    if time.monotonic() < _state.tripped_until or _state.probing:
        return None
    _state.probing = True
    return True


def _record_failure() -> None:
    """Count one dry-run failure, tripping the breaker at the threshold.

    Concurrent requests keep failing after the trip, so the warning is logged
    only when the breaker goes from closed to tripped, not once per failure.
    """
    _state.consecutive_failures += 1
    if _state.consecutive_failures < MAX_CONSECUTIVE_FAILURES:
        return
    tripping = not _breaker_holding()
    _state.tripped_until = time.monotonic() + BREAKER_COOLDOWN
    if tripping:
        log.warning(
            "broker dry-run failed %d times in a row; buying power falls back "
            "to the formula estimate for the next %.0fs",
            _state.consecutive_failures,
            BREAKER_COOLDOWN,
        )


def _record_success() -> None:
    _state.consecutive_failures = 0
    _state.tripped_until = 0.0


async def margin_account(session: Session) -> Account | None:
    """The account the dry-run prices against: the open margin account.

    `None` when there is no such account or the account list cannot be read
    — callers fall back to the formula estimate either way. An answer is
    cached for the life of the process; a failure is held for
    `BREAKER_COOLDOWN` and then retried.
    """
    if _state.account_resolved:
        return _state.margin_account
    if time.monotonic() < _state.account_retry_at:
        return None
    async with _gate().resolving:
        if _state.account_resolved:
            return _state.margin_account
        if time.monotonic() < _state.account_retry_at:
            return None
        try:
            accounts = await Account.get(session)
        except Exception:
            _state.account_retry_at = time.monotonic() + BREAKER_COOLDOWN
            log.warning(
                "broker account list could not be read; buying power falls "
                "back to the formula estimate for the next %.0fs",
                BREAKER_COOLDOWN,
            )
            return None
        _state.account_retry_at = 0.0
        _state.margin_account = next(
            (a for a in accounts if not a.is_closed and a.margin_or_cash == "Margin"),
            None,
        )
        _state.account_resolved = True
        return _state.margin_account


def order_for(structure: Structure) -> LimitOrder | None:
    """The dry-run order whose buying-power effect answers for `structure`.

    `None` when there is no priced net premium (nothing a broker could price).
    The limit price is the structure's net premium per share, rounded to the
    broker's tick — positive for a credit, negative for a debit — so the
    request is valid and the response clean. The endpoint is a calculation
    preview either way.
    """
    premium = structure.net_premium
    if premium is None or not structure.legs:
        return None
    legs = [
        Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=built.leg.occ,
            action=(
                OrderAction.SELL_TO_OPEN
                if built.spec.side is Side.SHORT
                else OrderAction.BUY_TO_OPEN
            ),
            quantity=built.spec.qty,
        )
        for built in structure.legs
    ]
    return LimitOrder(
        time_in_force=OrderTimeInForce.DAY,
        legs=legs,
        price=Decimal(str(premium)).quantize(TICK, rounding=ROUND_HALF_UP),
    )


def margin_requirement(effect: BuyingPowerEffect) -> float | None:
    """The isolated margin requirement as a positive dollar figure, or `None`
    when the response carries no usable one.

    The SDK folds the API's separate debit/credit field into the sign, so a
    normal requirement (a debit against buying power) arrives negative. The
    magnitude is the requirement either way.
    """
    value = getattr(effect, "isolated_order_margin_requirement", None)
    if value is None:
        return None
    try:
        number = abs(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


async def broker_bpr_for(
    session: Session, account: Account, structure: Structure
) -> float | None:
    """The broker's buying-power figure for one structure, or `None` on any
    failure.

    Uses the *isolated* margin requirement, the figure the formula
    estimates, rather than the account-wide buying-power change, which mixes
    in offsets against existing positions and the premium received.

    Every failure counts toward the breaker and a success resets it. A
    cancellation from the caller's budget counts as a failure (see
    `BUDGET_EXPIRED`); any other cancellation is re-raised.
    """
    order = order_for(structure)
    if order is None:
        return None
    probe = _claim_probe()
    if probe is None:
        return None
    try:
        async with _gate().dry_run:
            effect = await account.get_order_buying_power_effect(session, order)
    except asyncio.CancelledError as exc:
        if BUDGET_EXPIRED not in exc.args:
            raise
        _record_failure()
        return None
    except Exception:
        _record_failure()
        return None
    finally:
        if probe:
            _state.probing = False
    _record_success()
    return margin_requirement(effect)
