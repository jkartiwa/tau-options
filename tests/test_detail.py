from datetime import UTC, date, datetime, timedelta

from tau.tui.detail import quote_age, term_shape

NOW = datetime(2026, 7, 31, 15, 30, tzinfo=UTC)
TODAY = date(2026, 7, 31)


def test_quote_age_reads_as_a_time_when_fresh():
    assert quote_age(NOW - timedelta(seconds=20), NOW).endswith(
        (NOW - timedelta(seconds=20)).astimezone().strftime("%H:%M")
    )
    assert "ago" not in quote_age(NOW - timedelta(seconds=20), NOW)


def test_quote_age_counts_the_minutes_once_it_has_aged():
    """The cycle has carried this timestamp since it was fetched so that a
    stale quote cannot pass as a live one — which only works if the age is
    actually shown."""
    assert "(9m ago)" in quote_age(NOW - timedelta(minutes=9), NOW)
    assert "(120m ago)" in quote_age(NOW - timedelta(hours=2), NOW)


def test_term_shape_reads_the_curve_between_the_near_and_far_cycles():
    backward = (
        (TODAY + timedelta(days=10), 40.0),
        (TODAY + timedelta(days=70), 30.0),
    )
    assert "backwardation" in term_shape(backward, TODAY)
    contango = (
        (TODAY + timedelta(days=10), 30.0),
        (TODAY + timedelta(days=70), 40.0),
    )
    assert "contango" in term_shape(contango, TODAY)
    flat = (
        (TODAY + timedelta(days=10), 30.0),
        (TODAY + timedelta(days=70), 31.0),
    )
    assert "flat" in term_shape(flat, TODAY)


def test_term_shape_excludes_the_front_week_and_needs_a_far_cycle():
    """The front week is the noisiest point on the curve, and a curve with
    no far cycle has no shape to report rather than a flat one."""
    only_front = ((TODAY + timedelta(days=3), 60.0),)
    assert term_shape(only_front, TODAY) is None
    no_far = (
        (TODAY + timedelta(days=10), 30.0),
        (TODAY + timedelta(days=30), 31.0),
    )
    assert term_shape(no_far, TODAY) is None
