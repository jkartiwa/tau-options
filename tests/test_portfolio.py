from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tau.portfolio import Book, Position, parse_position


@dataclass
class FakeRaw:
    """The shape `Account.get_positions` returns, minus everything the
    parser doesn't read."""

    symbol: str
    underlying_symbol: str
    quantity: Decimal
    quantity_direction: str
    instrument_type: str = "Equity Option"


def test_short_quantity_is_signed_negative():
    """The API sends an unsigned quantity with the direction beside it. A
    short strangle that sums to +2 downstream is the bug this prevents."""
    p = parse_position(
        FakeRaw("AAPL  260918P00150000", "AAPL", Decimal("2"), "Short")
    )
    assert p.quantity == -2
    assert p.is_short


def test_long_quantity_stays_positive():
    p = parse_position(
        FakeRaw("AAPL  260918P00150000", "AAPL", Decimal("2"), "Long")
    )
    assert p.quantity == 2
    assert not p.is_short


def test_occ_symbol_yields_expiration_strike_and_right():
    p = parse_position(
        FakeRaw("AAPL  260918C00150500", "AAPL", Decimal("1"), "Long")
    )
    assert p.expiration == date(2026, 9, 18)
    assert p.strike == 150.5
    assert p.right == "C"
    assert p.is_option


def test_equity_position_has_no_option_fields():
    p = parse_position(FakeRaw("AAPL", "AAPL", Decimal("100"), "Long", "Equity"))
    assert p.strike is None and p.right is None
    assert not p.is_option
    assert "shares" in p.describe()


SHORT_PUT = Position("A 26P", "AAPL", "Equity Option", -2.0, date(2026, 9, 18), 150.0, "P")
LONG_CALL = Position("M 26C", "MSFT", "Equity Option", 1.0, date(2026, 9, 18), 400.0, "C")
SHARES = Position("NVDA", "NVDA", "Equity", 100.0)
BOOK = Book(
    positions=(SHORT_PUT, LONG_CALL, SHARES),
    net_liq=100_000.0,
    maintenance=25_000.0,
)


def test_short_premium_is_distinguished_from_merely_holding():
    """Owning shares is not selling premium against them — only the first
    makes another short strangle a concentration."""
    assert BOOK.short_premium_in("AAPL")
    assert not BOOK.short_premium_in("MSFT")  # long option, not short
    assert not BOOK.short_premium_in("NVDA")  # equity only
    assert BOOK.holds("NVDA")


def test_contracts_counts_options_only_and_signed():
    assert BOOK.contracts("AAPL") == -2
    assert BOOK.contracts("MSFT") == 1
    assert BOOK.contracts("NVDA") == 0  # shares are not contracts
    assert BOOK.contracts("TSLA") == 0


def test_symbol_lookup_is_case_insensitive():
    assert BOOK.for_symbol("aapl") == (SHORT_PUT,)


def test_sizing_against_net_liq():
    assert BOOK.pct_of_net_liq(5_000.0) == 0.05
    assert BOOK.utilization == 0.25


def test_an_unknown_account_never_invents_a_denominator():
    """Every sizing figure has to vanish rather than default when the account
    could not be read — a percentage of an assumed net liq is a wrong number,
    not a rough one."""
    empty = Book()
    assert empty.pct_of_net_liq(5_000.0) is None
    assert empty.utilization is None
    assert empty.describe("AAPL") is None
    assert not empty.holds("AAPL")
