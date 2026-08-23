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


async def broker_bpr_for(session, account, structure: Structure) -> float | None:
    """The broker's buying-power figure for one structure, or `None` on any
    failure.

    Uses the *isolated* margin requirement — the margin-only figure the
    formula claims to estimate — rather than the account-wide change in
    buying power, which blends in offsets against existing positions and the
    premium flow. The dry-run is a calculation; nothing here ever places an
    order.
    """
    order = order_for(structure)
    if order is None:
        return None
    try:
        async with _gate().dry_run:
            effect = await account.get_order_buying_power_effect(session, order)
    except Exception:
        return None
    value = effect.isolated_order_margin_requirement
    if value is None or value < 0:
        return None
    return float(value)
