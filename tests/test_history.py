from datetime import UTC, date, datetime, timedelta

import pytest

from tau.history import MOVE_WINDOW, Bar, History


def build(closes: list[float], start: date = date(2025, 1, 1)) -> History:
    """Bars with high/low pinned to the close, so range tests are exact."""
    bars = tuple(
        Bar(day=start + timedelta(days=i), open=c, high=c, low=c, close=c)
        for i, c in enumerate(closes)
    )
    return History(symbol="TEST", bars=bars, fetched_at=datetime.now(UTC))


def test_range_position_spans_low_to_high():
    h = build([100.0, 50.0, 150.0, 100.0])
    assert h.low_52w == 50.0
    assert h.high_52w == 150.0
    assert h.range_position == 0.5


def test_range_position_is_none_on_a_flat_year():
    # A degenerate range would divide by zero rather than mean "mid-range".
    assert build([100.0] * 30).range_position is None


def test_move_is_the_return_over_the_window():
    closes = [100.0] * 30 + [110.0]
    h = build(closes)
    # Last close vs the close MOVE_WINDOW bars back.
    assert h.move == pytest.approx(0.10)


def test_move_is_none_without_enough_bars():
    assert build([100.0] * MOVE_WINDOW).move is None


def test_baseline_vol_excludes_the_recent_move():
    """The point of the whole metric: a violent break must not be allowed to
    inflate the denominator it is measured against, or it reports as calm."""
    calm = [100.0 + (0.1 if i % 2 else -0.1) for i in range(200)]
    crash = [100.0 * 0.97**i for i in range(1, MOVE_WINDOW + 1)]
    h = build(calm + crash)

    calm_daily_vol = build(calm).baseline_vol
    assert calm_daily_vol is not None

    # The baseline is the calm period's, untouched by the crash that follows
    # it. Including those ~3%/day bars would widen it by an order of
    # magnitude and shrink the z-score that depends on it.
    assert h.baseline_vol == pytest.approx(calm_daily_vol, rel=0.05)

    assert h.move is not None and h.move < -0.4
    assert h.move_z is not None and h.move_z < -20
    assert h.stretched


def test_quiet_drift_is_not_stretched():
    closes = [100.0 * (1.001**i) + (0.4 if i % 2 else -0.4) for i in range(200)]
    h = build(closes)
    assert h.move_z is not None
    assert not h.stretched


def test_move_z_is_signed_so_direction_survives():
    calm = [100.0 + (0.2 if i % 2 else -0.2) for i in range(200)]
    up = build(calm + [130.0] * MOVE_WINDOW)
    down = build(calm + [70.0] * MOVE_WINDOW)
    assert up.move_z is not None and up.move_z > 0
    assert down.move_z is not None and down.move_z < 0


def test_year_window_drops_older_bars():
    old = date(2024, 1, 1)
    h = build([10.0] * 5 + [100.0] * 400, start=old)
    # The 10.0 bars are more than 365 days before the last bar.
    assert h.low_52w == 100.0


def test_empty_history_answers_none_rather_than_raising():
    h = History(symbol="TEST", bars=(), fetched_at=datetime.now(UTC))
    assert h.last is None
    assert h.range_position is None
    assert h.move_z is None
    assert not h.stretched
