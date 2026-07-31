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
    """One held instrument. `quantity` is signed — negative is short.

    Prices are per share, as the API sends them; the multiplier is carried
    alongside rather than folded in, so a leg keeps quoting in the units its
    chain quotes in."""

    symbol: str  # as held: an OCC symbol for an option, the ticker for stock
    underlying: str
    instrument_type: str
    quantity: float
    expiration: date | None = None
    strike: float | None = None
    right: str | None = None  # "C" or "P"
    open_price: float | None = None  # average fill, per share
    mark_price: float | None = None  # what it is worth now, per share
    multiplier: float = 1.0
    opened_at: date | None = None

    @property
    def is_option(self) -> bool:
        return self.right is not None

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    def _signed_value(self, price: float | None) -> float | None:
        """What this leg is worth *to the seller*, in dollars.

        Positive means the position took money in — a short leg opened for
        credit. Negating the quantity is what does that: the same expression
        then reports a long leg as the debit it is, so mixed structures net
        out correctly without special-casing either side."""
        if price is None:
            return None
        return price * -self.quantity * self.multiplier

    @property
    def credit(self) -> float | None:
        return self._signed_value(self.open_price)

    @property
    def value(self) -> float | None:
        """What closing it now would return — negative when it costs money."""
        return self._signed_value(self.mark_price)

    def describe(self) -> str:
        if not self.is_option:
            return f"{self.quantity:+g} shares"
        strike = f"{self.strike:g}" if self.strike is not None else "?"
        return f"{self.quantity:+g}× {strike}{self.right}"


@dataclass(frozen=True)
class Trade:
    """Legs on one underlying sharing an expiration.

    A strangle is one decision, not two — it was opened together, it is
    managed together, and it will be closed together. Listing its legs
    separately would make the position page a ledger, and a ledger cannot
    answer the only question worth asking of an open trade: is it time to
    take this off."""

    underlying: str
    expiration: date | None
    legs: tuple[Position, ...]

    @property
    def contracts(self) -> float:
        """Size of the trade, unsigned — the contract count you would enter
        to close it. Legs of a spread share it; the widest leg defines it."""
        return max((abs(leg.quantity) for leg in self.legs), default=0.0)

    @property
    def is_short_premium(self) -> bool:
        return any(leg.is_option and leg.is_short for leg in self.legs)

    @property
    def credit(self) -> float | None:
        """Net dollars taken in at open. Negative for a debit structure."""
        values = [leg.credit for leg in self.legs]
        return None if any(v is None for v in values) else sum(values)

    @property
    def value(self) -> float | None:
        values = [leg.value for leg in self.legs]
        return None if any(v is None for v in values) else sum(values)

    @property
    def pnl(self) -> float | None:
        """Credit taken in, less what it would cost to close now."""
        credit, value = self.credit, self.value
        if credit is None or value is None:
            return None
        return credit - value

    @property
    def pnl_pct(self) -> float | None:
        """Profit as a share of the credit received — the number the usual
        management rules are written in. "Close at 50%" is a rule you can
        act on; a dollar figure is one you have to divide first."""
        credit, pnl = self.credit, self.pnl
        if pnl is None or not credit or credit <= 0:
            return None
        return pnl / credit

    def dte(self, today: date) -> int | None:
        if self.expiration is None:
            return None
        return (self.expiration - today).days

    def days_held(self, today: date) -> int | None:
        opened = [leg.opened_at for leg in self.legs if leg.opened_at]
        return (today - min(opened)).days if opened else None

    def describe(self) -> str:
        """The structure in the shorthand it would be quoted in."""
        options = sorted(
            (leg for leg in self.legs if leg.is_option),
            key=lambda leg: (leg.strike or 0),
        )
        if not options:
            return " · ".join(leg.describe() for leg in self.legs)
        strikes = "/".join(
            f"{leg.strike:g}{leg.right}" for leg in options
        )
        when = self.expiration.strftime("%d %b") if self.expiration else "?"
        sign = "-" if self.is_short_premium else "+"
        return f"{sign}{self.contracts:g}× {when} {strikes}"


def group_trades(positions: tuple[Position, ...]) -> tuple[Trade, ...]:
    """Legs into trades, by underlying and expiration.

    Two cycles open on the same name are two trades — they expire on
    different days and are managed on different clocks — so the expiration
    is part of the key, not just the symbol. Equity sits in its own group
    with no expiration."""
    grouped: dict[tuple[str, date | None], list[Position]] = {}
    for p in positions:
        grouped.setdefault((p.underlying, p.expiration), []).append(p)
    return tuple(
        Trade(underlying=symbol, expiration=expiration, legs=tuple(legs))
        for (symbol, expiration), legs in sorted(
            grouped.items(), key=lambda kv: (kv[0][0], kv[0][1] or date.max)
        )
    )


@dataclass(frozen=True)
class Book:
    """The account as the screen needs to see it: what is held, and how much
    room is left."""

    positions: tuple[Position, ...] = ()
    net_liq: float | None = None
    maintenance: float | None = None
    account_number: str | None = None
    fetched_at: datetime | None = None
    # Maintenance requirement per underlying, from the margin report. The
    # broker's own number, unlike the estimate a proposal carries.
    requirements: dict[str, float] | None = None

    @property
    def trades(self) -> tuple[Trade, ...]:
        return group_trades(self.positions)

    def requirement(self, symbol: str) -> float | None:
        if not self.requirements:
            return None
        return self.requirements.get(symbol.upper())

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
        """One line summarising the exposure already on this name.

        Grouped into trades rather than listed leg by leg: what matters to a
        screener candidate is "you are short a strangle here", not that two
        separate contracts exist."""
        held = self.for_symbol(symbol)
        if not held:
            return None
        return " · ".join(t.describe() for t in group_trades(held))

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


def _num(value) -> float | None:
    return None if value is None else float(value)


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
    # `mark_price` is what the position is worth now; `close_price` is the
    # previous session's settle, which is what the API has to offer outside
    # market hours. Preferring the first and falling back keeps the page
    # readable at night, when a premium seller is most likely reading it.
    mark = _num(getattr(raw, "mark_price", None))
    if mark is None:
        mark = _num(getattr(raw, "close_price", None))
    opened = getattr(raw, "created_at", None)
    return Position(
        symbol=raw.symbol,
        underlying=raw.underlying_symbol,
        instrument_type=str(getattr(raw.instrument_type, "value", raw.instrument_type)),
        quantity=quantity,
        expiration=expiration,
        strike=strike,
        right=right,
        open_price=_num(getattr(raw, "average_open_price", None)),
        mark_price=mark,
        multiplier=float(getattr(raw, "multiplier", 1) or 1),
        opened_at=opened.date() if opened is not None else None,
    )


def parse_requirements(report) -> dict[str, float]:
    """Per-underlying maintenance requirement from the margin report."""
    out: dict[str, float] = {}
    for entry in getattr(report, "groups", None) or []:
        symbol = getattr(entry, "underlying_symbol", None)
        requirement = getattr(entry, "maintenance_requirement", None)
        if symbol and requirement is not None:
            out[symbol.upper()] = float(requirement)
    return out


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
    positions = await account.get_positions(session, include_marks=True)
    balances = await account.get_balances(session)
    # The margin report is the broker's own per-underlying requirement, and
    # it is the one number here that is not an estimate. It is also the most
    # expendable: losing it costs a column, so it never fails the book.
    requirements: dict[str, float] | None = None
    try:
        requirements = parse_requirements(
            await account.get_margin_requirements(session)
        )
    except Exception:
        pass
    return Book(
        positions=tuple(parse_position(p) for p in positions),
        net_liq=float(balances.net_liquidating_value),
        maintenance=float(balances.maintenance_requirement),
        account_number=account.account_number,
        fetched_at=datetime.now(UTC),
        requirements=requirements,
    )
