"""Containment for open-ended application periods."""

from datetime import UTC, datetime

from pg_bitemporal.sqlalchemy.writer import TimeRange

JAN_1 = datetime(2022, 1, 1, tzinfo=UTC)
JUN_1 = datetime(2022, 6, 1, tzinfo=UTC)
JUN_13 = datetime(2022, 6, 13, tzinfo=UTC)


def _span(lower, upper, bounds="[)"):
    return TimeRange(lower, upper, bounds=bounds)


def test_equal_open_ended_ranges_are_covered_without_a_two_sided_gap():
    whole = _span(JAN_1, None)

    assert whole.contains(whole, non_zero_gaps=False)
    assert not whole.contains(whole)


def test_open_ended_range_covers_a_later_open_suffix_but_does_not_split_it():
    whole = _span(JAN_1, None)
    suffix = _span(JUN_13, None)

    assert whole.contains(suffix, non_zero_gaps=False)
    assert not whole.contains(suffix)
    assert not suffix.contains(whole, non_zero_gaps=False)


def test_shared_start_is_covered_and_is_not_an_interior_split():
    whole = _span(JAN_1, None)
    prefix = _span(JAN_1, JUN_13)

    assert whole.contains(prefix, non_zero_gaps=False)
    assert not whole.contains(prefix)


def test_finite_window_inside_an_open_range_leaves_both_sides():
    whole = _span(JAN_1, None)
    window = _span(JUN_1, JUN_13)

    assert whole.contains(window)
    assert whole.starts_before(window)
