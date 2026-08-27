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
import time
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

# Consecutive dry-run failures that stop tau trying again for a while. The
# account list resolving fine while every POST times out is the case the
# per-symbol deadline cannot bound: nothing caches that outcome, so each
# symbol pays the full read-timeout wait again and the stall grows with
# `--top`. Three in a row is a broker that is not answering, not a blip, and
# the formula estimate covers every symbol after it.
MAX_CONSECUTIVE_FAILURES = 3

# How long a trip lasts. The dry-run endpoint is the one account method the
# SDK reads without `validate_response`, so a rate limit reaches this module
# as `KeyError('data')` or a JSON decode error — indistinguishable from any
# other failure, and not worth guessing at. Recovering on a clock instead of
# classifying the error costs nothing and covers the case that actually
# matters: a TUI session runs for hours, and one rough patch must not end
# broker pricing for the rest of it. After the cooldown a single probe
# decides whether to re-enable or trip again.
BREAKER_COOLDOWN = 120.0

# The reason a caller's phase budget cancels a dry-run task with. A dry run
# the budget cut off is a broker that did not answer in time — the same
# outcome as the read timeout the breaker already counts, and the one failure
# mode that never reached the counter, because `CancelledError` is not an
# `Exception`. Uncounted, it is exactly the stall the breaker exists to stop:
# a slow-but-healthy broker drains the shared gate, every symbol after the
# first times out on queueing alone, and nothing ever trips.
#
# A cancellation that carries no such reason — Ctrl-C, a TUI tearing down its
# worker — is somebody else's, and is re-raised untouched rather than turned
# into a silent formula fallback.
BUDGET_EXPIRED = "tau: broker pull budget expired"


def cancel_for_budget(task) -> None:
    """Cancel a dry-run task so its failure counts toward the breaker."""
    task.cancel(BUDGET_EXPIRED)


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


# False = not resolved yet, an Account or None = resolved. An answer the API
# actually gave is final: the account list and the token are fixed for the
# lifetime of the process, so an account list that came back with nothing but
# cash and closed accounts will not grow a margin account later, and
# re-calling on every batch would just stack requests.
#
# A resolution that *failed* is not an answer. A 429 or a read timeout says
# nothing about the token, and caching it as "no account" ends broker pricing
# for the rest of the process with nothing on screen to say why — over a blip,
# on the one path that a rank pass hits six-wide at its most concurrent
# moment. So a failure is held for `BREAKER_COOLDOWN` and then tried once
# more: the same time-boxed recovery the dry-run breaker gets, on the same
# clock, for the same reason — a TUI session runs for hours and one rough
# patch must not end broker pricing for the rest of it.
_margin_account: Account | None | bool = False
_account_retry_at = 0.0

# Consecutive dry-run failures so far, when the current trip expires (0.0 =
# not tripped), and whether the one post-cooldown probe is already out.
# Process-wide for the same reason the account is: the condition being
# tracked — an account API that will not answer this token — does not vary by
# symbol, so learning it once is the whole point.
_consecutive_failures = 0
_tripped_until = 0.0
_probing = False


def dry_runs_disabled() -> bool:
    """Whether broker pricing is currently held back, for either reason.

    The breaker tripping and the account failing to resolve disable the same
    thing on the same clock, and the on-screen marker reads off this one
    answer — a session where the account list 429'd once has to look different
    from one where the figures were always estimates. Callers skip the pull
    entirely rather than queue POSTs that will not be made; `broker_bpr_for`
    and `margin_account` enforce their own halves for anyone who does not.
    False again once the cooldown is up, at which point the next call is the
    attempt that decides whether it stays that way.
    """
    return _breaker_holding() or time.monotonic() < _account_retry_at


def _breaker_holding() -> bool:
    return _tripped_until > 0.0 and time.monotonic() < _tripped_until


def _claim_probe() -> bool | None:
    """`None` when the breaker is holding, `True` for the one caller allowed
    to probe after a cooldown, `False` for an ordinary call."""
    global _probing
    if _tripped_until <= 0.0:
        return False
    if time.monotonic() < _tripped_until or _probing:
        return None
    _probing = True
    return True


def _record_failure() -> None:
    """One dry-run failure, counted.

    The trip is announced on the way in and only on the way in. Failures
    arrive concurrently in production — every structure in a shortlist is
    past the breaker check before the first of them fails, so the counter
    crosses the threshold and then keeps climbing through the rest of the
    batch, ten deep per symbol and six symbols wide. A line per failure from
    there on is the same sentence sixty times, and with no `basicConfig`
    anywhere in tau it lands on stderr in the middle of `tau rank`'s table.
    Once the cooldown has expired the next failure is a fresh trip and says
    so again.
    """
    global _consecutive_failures, _tripped_until
    _consecutive_failures += 1
    if _consecutive_failures < MAX_CONSECUTIVE_FAILURES:
        return
    tripping = not _breaker_holding()
    _tripped_until = time.monotonic() + BREAKER_COOLDOWN
    if tripping:
        log.warning(
            "broker dry-run failed %d times in a row; buying power falls back "
            "to the formula estimate for the next %.0fs",
            _consecutive_failures,
            BREAKER_COOLDOWN,
        )


def _record_success() -> None:
    global _consecutive_failures, _tripped_until
    _consecutive_failures = 0
    _tripped_until = 0.0


async def margin_account(session) -> Account | None:
    """The account the dry-run prices against: the open margin account.

    `None` when there is no such account or the account list cannot be read
    — callers fall back to the formula estimate either way. An answer is
    cached for the life of the process; a failure is only held for
    `BREAKER_COOLDOWN` and then retried once. See `_margin_account` for why
    the two are not the same thing.
    """
    global _margin_account, _account_retry_at
    if _margin_account is not False:
        return _margin_account or None
    if time.monotonic() < _account_retry_at:
        return None
    async with _gate().resolving:
        if _margin_account is not False:
            return _margin_account or None
        if time.monotonic() < _account_retry_at:
            return None
        try:
            accounts = await Account.get(session)
        except Exception:
            _account_retry_at = time.monotonic() + BREAKER_COOLDOWN
            log.warning(
                "broker account list could not be read; buying power falls "
                "back to the formula estimate for the next %.0fs",
                BREAKER_COOLDOWN,
            )
            return None
        _account_retry_at = 0.0
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
    `isolated-order-margin-requirement-effect` field naming the direction,
    and the SDK folds the two together before anything here sees them —
    `set_sign_for` rewrites the value to `-abs(value)` when that effect is
    `Debit` and then drops the field, which `BuyingPowerEffect` does not
    declare and pydantic will not keep. The sign the SDK left behind is the
    only surviving record of the direction, so that is what this reads: a
    margin requirement is a debit against buying power, the ordinary
    successful response therefore arrives here *negative*, and reading that
    as garbage would silently turn the whole feature off.

    The magnitude is the requirement whichever way it is signed. A zero or
    non-numeric value is not a figure this can use.
    """
    value = getattr(effect, "isolated_order_margin_requirement", None)
    if value is None:
        return None
    try:
        number = abs(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


async def broker_bpr_for(session, account, structure: Structure) -> float | None:
    """The broker's buying-power figure for one structure, or `None` on any
    failure.

    Uses the *isolated* margin requirement — the margin-only figure the
    formula claims to estimate — rather than the account-wide change in
    buying power, which blends in offsets against existing positions and the
    premium flow. The dry-run is a calculation; nothing here ever places an
    order.

    Every failure counts against the breaker, and a success clears it — the
    endpoint gives this module no way to tell one failure from another, and
    the cooldown recovers from all of them alike. A caller's phase budget
    cutting the call short is one of those failures: see `BUDGET_EXPIRED`.
    Any other cancellation belongs to whoever raised it and is re-raised.
    """
    global _probing
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
            _probing = False
    _record_success()
    return margin_requirement(effect)
