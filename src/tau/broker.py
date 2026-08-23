"""The broker's own buying-power figure, via the order dry-run calculation.

`payoff.bpr()` is a formula and says so. This module asks the broker what the
margin actually is: one POST per structure to the account's order dry-run
endpoint (`/accounts/{id}/orders/dry-run`), which calculates the
buying-power effect of an order without placing, canceling, or replacing
anything. That endpoint is all this module ever calls.

The line matters because the token can place live orders. The captain's
account runs on portfolio margin, where the formula's naked-margin model is
simply the wrong model — measured 2026-08-20/21, formula $3,980 vs the
broker's isolated $3,651 on an AAPL strangle, and $28,335 vs $37,010 on MU.
The broker's figure is the truth; the formula is the fallback. Any failure
here — 403 insufficient scopes, network error, timeout, rate limit, SDK
exception, missing env — silently lands back on the formula estimate. Nothing
in tau crashes, stalls, or changes shape because the broker did not answer.
"""

import asyncio
import logging
from decimal import ROUND_HALF_UP, Decimal
from weakref import WeakKeyDictionary

from tastytrade.account import Account
from tastytrade.order import (
    InstrumentType,
    Leg,
    LimitOrder,
    OrderAction,
    OrderTimeInForce,
)

from tau.build import Structure
from tau.payoff import Side

log = logging.getLogger(__name__)

# Dry-run POSTs in flight, counted across everything running on one event
# loop rather than per batch: a rank pass prices six symbols concurrently, so
# a per-batch cap would multiply out to six times this number. They are cheap
# calculation calls, but the account API rate-limits, and the failure mode is
# a graceful fallback anyway — the cap keeps a rank pass from landing as a
# burst.
MAX_CONCURRENT = 4

# The smallest price increment the broker accepts on a limit order. The net
# premium is a sum of `(bid + ask) / 2` floats, so half-cent mids and binary
# noise are both routine; an order priced at 1.0749999999999997 comes back
# rejected with no buying-power body, which reads here as "no broker figure"
# for no reason the caller could ever see.
TICK = Decimal("0.01")

# Consecutive dry-run failures that stop the process trying again. The
# account list resolving fine while every POST times out is the case the
# per-symbol deadline cannot bound: nothing caches that outcome, so each
# symbol pays the full read-timeout wait again and the stall grows with
# `--top`. Three in a row is a broker that is not answering, not a blip, and
# the formula estimate covers every symbol after it.
MAX_CONSECUTIVE_FAILURES = 3

# A rate-limited dry-run is the API answering and asking for less load, so it
# is retried on the same exponential backoff the chain fetch uses and never
# counted against the breaker. The breaker is for an account API that will
# not answer this token at all; letting a burst of 429s latch it would turn
# the whole feature off for the run over transient load — the one thing the
# graceful fallback must not be mistaken for.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BASE_BACKOFF = 1.0


class _LoopGate:
    """The asyncio primitives shared by everything running on one event loop.

    Both belong to the loop that awaits them, so neither can be a module-level
    singleton — a lock created under one loop and awaited under another is an
    error. Keyed weakly by running loop they are process-wide in practice (one
    loop per run) and still correct across tests.
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


# False = not resolved yet, None = resolved to no usable account. The account
# list and the token are fixed for the lifetime of the process, so a failed
# resolution (scoped-down token, network down) cannot recover in-process, and
# re-calling the API on every batch would just stack 403s. One resolution per
# process, success or failure; the formula estimate covers the gap.
_margin_account: Account | None | bool = False

# Consecutive dry-run failures so far, and whether the breaker has tripped.
# Process-wide for the same reason the account is: the condition being
# tracked — an account API that will not answer this token — does not vary by
# symbol, so learning it once is the whole point.
_consecutive_failures = 0
_tripped = False


def dry_runs_disabled() -> bool:
    """Whether the circuit breaker has given up on the account API.

    Callers skip the pull entirely rather than queue POSTs that will not be
    made; `broker_bpr_for` enforces the same thing for anyone who does not.
    """
    return _tripped


def is_rate_limited(exc: Exception) -> bool:
    """Whether a failed call was backpressure rather than a refusal."""
    text = str(exc).lower()
    return "429" in text or "too many requests" in text


def _record_failure() -> None:
    global _consecutive_failures, _tripped
    _consecutive_failures += 1
    if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not _tripped:
        _tripped = True
        log.warning(
            "broker dry-run failed %d times in a row; buying power falls back "
            "to the formula estimate for the rest of this run",
            _consecutive_failures,
        )


def _record_success() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


async def margin_account(session) -> Account | None:
    """The account the dry-run prices against: the open margin account.

    `None` when there is no such account or the account list cannot be read
    — callers fall back to the formula estimate. Resolved once per process;
    see `_margin_account` for why.
    """
    global _margin_account
    if _margin_account is not False:
        return _margin_account or None
    async with _gate().resolving:
        if _margin_account is not False:
            return _margin_account or None
        try:
            accounts = await Account.get(session)
        except Exception:
            _margin_account = None
            return None
        _margin_account = next(
            (a for a in accounts if not a.is_closed and a.margin_or_cash == "Margin"),
            None,
        )
        return _margin_account


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


def margin_requirement(effect) -> float | None:
    """The isolated margin requirement as a positive dollar figure, or `None`
    when the response carries no usable one.

    The API does not send negative numbers; it sends a magnitude beside an
    `-effect` field naming the direction, and the SDK folds the two together
    — `set_sign_for` rewrites the value to `-abs(value)` when
    `isolated-order-margin-requirement-effect` is `Debit`. A margin
    requirement is a debit against buying power, so the ordinary successful
    response arrives here *negative*, and reading that as garbage would
    silently turn the whole feature off. The sign is the effect field, after
    the SDK is done with it: negative means Debit and its magnitude is the
    requirement. A credit-signed or zero requirement is not a margin figure
    this can use, and neither is a value that will not compare.
    """
    value = getattr(effect, "isolated_order_margin_requirement", None)
    if value is None:
        return None
    direction = getattr(effect, "isolated_order_margin_requirement_effect", None)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if str(direction) == "Debit" or number < 0:
        number = abs(number)
    return number if number > 0 else None


async def broker_bpr_for(session, account, structure: Structure) -> float | None:
    """The broker's buying-power figure for one structure, or `None` on any
    failure.

    Uses the *isolated* margin requirement — the margin-only figure the
    formula claims to estimate — rather than the account-wide change in
    buying power, which blends in offsets against existing positions and the
    premium flow. The dry-run is a calculation; nothing here ever places an
    order.

    A rate-limited attempt backs off and tries again without counting against
    the breaker; anything else counts once and gives up.
    """
    order = order_for(structure)
    if order is None:
        return None
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        if _tripped:
            return None
        try:
            async with _gate().dry_run:
                effect = await account.get_order_buying_power_effect(session, order)
        except Exception as exc:
            if not is_rate_limited(exc):
                _record_failure()
                return None
            if attempt == RATE_LIMIT_RETRIES:
                return None
            # outside the gate on purpose: a backing-off call holding a slot
            # would throttle the calls that are not being rate-limited
            await asyncio.sleep(RATE_LIMIT_BASE_BACKOFF * 2**attempt)
            continue
        _record_success()
        return margin_requirement(effect)
    return None
