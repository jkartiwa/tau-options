"""What the account already holds.

A screen that ignores the book will rank a name you are already short at the
top of the list and call it an opportunity. It isn't one — a second strangle
on the same underlying is not a second trade, it is a bigger version of the
first, and the risk it adds is not the risk the ranking shows. The same goes
for the capital: a buying-power estimate in dollars means nothing until it is
a share of the account it would be drawn from.

Read scope covers positions and balances, so this costs two REST calls and no
new permission — the grant still cannot place an order.

Everything degrades to `None`. A grant without account access, a network
failure, or a session that was never authenticated leaves `book` unset and
every view renders exactly as it did before, minus the position columns. The
book is an enrichment, never a prerequisite.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from tastytrade import Session
from tastytrade.account import Account

# OCC 21-character option symbol: 6-char padded root, YYMMDD, C/P, then the
# strike in thousandths. tastytrade returns positions in this format, and it
# carries the strike and right that CurrentPosition itself does not expose.
OCC = re.compile(r"^(?P<root>.{6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<right>[CP])(?P<strike>\d{8})$")

SHORT = "Short"
EQUITY_OPTION = "Equity Option"


@dataclass(frozen=True)
class Position:
    """One held instrument. `quantity` is signed — negative is short."""

    symbol: str  # as held: an OCC symbol for an option, the ticker for stock
    underlying: str
    instrument_type: str
    quantity: float
    expiration: date | None = None
    strike: float | None = None
    right: str | None = None  # "C" or "P"

    @property
    def is_option(self) -> bool:
        return self.right is not None

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    def describe(self) -> str:
        if not self.is_option:
            return f"{self.quantity:+g} shares"
        strike = f"{self.strike:g}" if self.strike is not None else "?"
        when = self.expiration.strftime("%d %b") if self.expiration else "?"
        return f"{self.quantity:+g}× {when} {strike}{self.right}"


@dataclass(frozen=True)
class Book:
    """The account as the screen needs to see it: what is held, and how much
    room is left."""

    positions: tuple[Position, ...] = ()
    net_liq: float | None = None
    maintenance: float | None = None
    account_number: str | None = None
    fetched_at: datetime | None = None

    def for_symbol(self, symbol: str) -> tuple[Position, ...]:
        return tuple(p for p in self.positions if p.underlying == symbol.upper())

    def contracts(self, symbol: str) -> float:
        """Net option contracts held on the underlying, signed."""
        return sum(p.quantity for p in self.for_symbol(symbol) if p.is_option)

    def short_premium_in(self, symbol: str) -> bool:
        """Whether any short option leg is already open on this underlying —
        the case where adding a strangle concentrates rather than
        diversifies."""
        return any(p.is_option and p.is_short for p in self.for_symbol(symbol))

    def holds(self, symbol: str) -> bool:
        return bool(self.for_symbol(symbol))

    def describe(self, symbol: str) -> str | None:
        """One line summarising the exposure already on this name."""
        held = self.for_symbol(symbol)
        if not held:
            return None
        return " · ".join(p.describe() for p in held)

    def pct_of_net_liq(self, dollars: float | None) -> float | None:
        """A dollar requirement as a fraction of the account. This is the
        number that decides whether a trade is sized sanely; the raw BPR
        estimate cannot say, because it does not know the account."""
        if dollars is None or not self.net_liq:
            return None
        return dollars / self.net_liq

    @property
    def utilization(self) -> float | None:
        """Maintenance requirement over net liq — how much of the account is
        already committed before this trade is added."""
        if self.maintenance is None or not self.net_liq:
            return None
        return self.maintenance / self.net_liq


def parse_position(raw) -> Position:
    """One `CurrentPosition` from the SDK, flattened.

    Quantity arrives unsigned with the direction in a separate field, which
    is a foot-gun everywhere downstream — a short strangle summing to +2 reads
    as long. It is signed here, once, at the boundary."""
    quantity = float(raw.quantity)
    if raw.quantity_direction == SHORT:
        quantity = -quantity
    expiration = strike = right = None
    match = OCC.match(raw.symbol)
    if match:
        expiration = date(
            2000 + int(match["yy"]), int(match["mm"]), int(match["dd"])
        )
        strike = int(match["strike"]) / 1000
        right = match["right"]
    return Position(
        symbol=raw.symbol,
        underlying=raw.underlying_symbol,
        instrument_type=str(getattr(raw.instrument_type, "value", raw.instrument_type)),
        quantity=quantity,
        expiration=expiration,
        strike=strike,
        right=right,
    )


async def fetch_book(session: Session, account_number: str | None = None) -> Book:
    """Positions and balances for one account.

    With several accounts on the grant and none named, the first open one is
    used — picking silently is wrong, so the chosen number is carried on the
    Book and shown in the UI. Set TAU_ACCOUNT to pin a different one."""
    accounts = await Account.get(session)
    if not isinstance(accounts, list):
        accounts = [accounts]
    accounts = [a for a in accounts if not a.is_closed]
    if not accounts:
        raise ValueError("no open accounts on this grant")
    account = next(
        (a for a in accounts if a.account_number == account_number), accounts[0]
    )
    positions = await account.get_positions(session)
    balances = await account.get_balances(session)
    return Book(
        positions=tuple(parse_position(p) for p in positions),
        net_liq=float(balances.net_liquidating_value),
        maintenance=float(balances.maintenance_requirement),
        account_number=account.account_number,
        fetched_at=datetime.now(UTC),
    )
