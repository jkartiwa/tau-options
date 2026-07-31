from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tau.portfolio import Book, Position, Trade, group_trades, parse_position


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


# ---- trades ----

SEP = date(2026, 9, 18)


def opt(underlying, strike, right, qty, open_price, mark_price, expiration=SEP):
    return Position(
        symbol=f"{underlying}{strike}{right}",
        underlying=underlying,
        instrument_type="Equity Option",
        quantity=qty,
        expiration=expiration,
        strike=strike,
        right=right,
        open_price=open_price,
        mark_price=mark_price,
        multiplier=100.0,
        opened_at=date(2026, 7, 1),
    )


STRANGLE = Trade(
    underlying="AAPL",
    expiration=SEP,
    legs=(
        opt("AAPL", 150.0, "P", -2.0, 2.00, 1.00),
        opt("AAPL", 200.0, "C", -2.0, 1.00, 0.50),
    ),
)


def test_credit_is_what_the_trade_took_in():
    """3.00 of premium on 2 contracts, at a 100 multiplier."""
    assert STRANGLE.credit == 600.0


def test_pnl_is_credit_less_the_cost_to_close():
    assert STRANGLE.value == 300.0  # 1.50 × 2 × 100 to buy it back
    assert STRANGLE.pnl == 300.0
    assert STRANGLE.pnl_pct == 0.5  # half the credit — the usual trigger


def test_a_losing_short_trade_reports_negative_pnl():
    losing = Trade(
        underlying="AAPL",
        expiration=SEP,
        legs=(opt("AAPL", 150.0, "P", -1.0, 2.00, 5.00),),
    )
    assert losing.credit == 200.0
    assert losing.pnl == -300.0
    assert losing.pnl_pct == -1.5


def test_a_debit_structure_has_no_percent_of_credit():
    """Dividing a profit by a negative credit produces a number that reads
    backwards, so it is withheld rather than shown wrong."""
    debit = Trade(
        underlying="AAPL",
        expiration=SEP,
        legs=(opt("AAPL", 150.0, "C", 1.0, 5.00, 6.00),),
    )
    assert debit.credit == -500.0
    assert debit.pnl == 100.0
    assert debit.pnl_pct is None


def test_mixed_legs_net_out():
    """An iron condor collects on its shorts and pays for its wings; the net
    credit is what actually landed in the account."""
    condor = Trade(
        underlying="AAPL",
        expiration=SEP,
        legs=(
            opt("AAPL", 150.0, "P", -1.0, 2.00, 1.00),
            opt("AAPL", 145.0, "P", 1.0, 0.50, 0.20),
            opt("AAPL", 200.0, "C", -1.0, 2.00, 1.00),
            opt("AAPL", 205.0, "C", 1.0, 0.50, 0.20),
        ),
    )
    assert condor.credit == 300.0  # (2.00 - 0.50) × 2 sides × 100
    assert condor.value == 160.0
    assert condor.pnl == 140.0


def test_missing_marks_withhold_pnl_rather_than_guessing():
    unpriced = Trade(
        underlying="AAPL",
        expiration=SEP,
        legs=(
            opt("AAPL", 150.0, "P", -1.0, 2.00, 1.00),
            opt("AAPL", 200.0, "C", -1.0, 1.00, None),
        ),
    )
    assert unpriced.value is None
    assert unpriced.pnl is None
    assert unpriced.pnl_pct is None


def test_grouping_splits_by_expiration_not_just_symbol():
    """Two cycles on one name are two trades — they expire on different days
    and are managed on different clocks."""
    oct_ = date(2026, 10, 16)
    trades = group_trades((
        opt("AAPL", 150.0, "P", -1.0, 2.00, 1.00),
        opt("AAPL", 200.0, "C", -1.0, 1.00, 0.50),
        opt("AAPL", 160.0, "P", -1.0, 3.00, 2.00, expiration=oct_),
    ))
    assert len(trades) == 2
    assert [len(t.legs) for t in trades] == [2, 1]
    assert [t.expiration for t in trades] == [SEP, oct_]


def test_describe_reads_as_the_structure_would_be_quoted():
    assert STRANGLE.describe() == "-2× 18 Sep 150P/200C"


def test_dte_and_days_held():
    assert STRANGLE.dte(date(2026, 9, 1)) == 17
    assert STRANGLE.days_held(date(2026, 7, 31)) == 30


def test_book_describes_a_holding_as_a_trade_not_loose_legs():
    book = Book(positions=STRANGLE.legs)
    assert book.describe("AAPL") == "-2× 18 Sep 150P/200C"
    assert book.trades[0].pnl == 300.0
