"""Time parsing, timezones, DST and recurrence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from havoc.utils.time import (
    TimeParseError,
    day_bounds,
    format_datetime,
    get_zone,
    humanize_delta,
    in_quiet_hours,
    next_occurrence,
    parse_clock,
    parse_date,
    parse_duration,
    parse_local_datetime,
    parse_recurrence,
    parse_when,
    safe_zone,
    to_local,
    to_utc,
    week_bounds,
)


def test_durations() -> None:
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("2h") == timedelta(hours=2)
    assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)
    assert parse_duration("1.5 days") == timedelta(days=1, hours=12)
    assert parse_duration("۴۵ دقیقه").total_seconds() == 45 * 60
    assert parse_duration("2") == timedelta(minutes=2)


def test_bad_durations() -> None:
    for value in ("", "abc", "0m", "-5m", "9999w", "5m bananas", "1h junk"):
        with pytest.raises(TimeParseError):
            parse_duration(value)


def test_clock_and_date_parsing() -> None:
    assert parse_clock("09:30") == parse_clock("۰۹:۳۰")
    assert parse_date("2026-01-02") == date(2026, 1, 2)
    assert parse_date("02.01.2026") == date(2026, 1, 2)
    with pytest.raises(TimeParseError):
        parse_clock("25:00")


def test_timezone_resolution() -> None:
    assert str(get_zone("Asia/Tehran")) == "Asia/Tehran"
    assert str(safe_zone("Not/AZone", "UTC")) == "UTC"
    with pytest.raises(TimeParseError):
        get_zone("Not/AZone")


def test_parse_local_datetime_in_tehran() -> None:
    now = datetime(2026, 3, 20, 8, 0, tzinfo=UTC)
    result = parse_local_datetime("2026-03-21 09:00", "Asia/Tehran", now=now)
    # Tehran is UTC+3:30, so 09:00 local == 05:30 UTC
    assert result == datetime(2026, 3, 21, 5, 30, tzinfo=UTC)


def test_parse_local_datetime_tomorrow_and_time_only() -> None:
    now = datetime(2026, 3, 20, 6, 0, tzinfo=UTC)  # 09:30 Tehran
    tomorrow = parse_local_datetime("tomorrow 10:00", "Asia/Tehran", now=now)
    assert tomorrow.date() == date(2026, 3, 21)
    with_time = parse_local_datetime("12:00", "Asia/Tehran", now=now)
    assert with_time.date() == date(2026, 3, 20)


def test_parse_when_relative_and_absolute() -> None:
    now = datetime(2026, 3, 20, 6, 0, tzinfo=UTC)
    assert parse_when("2h", "UTC", now=now) == now + timedelta(hours=2)
    assert parse_when("2026-04-01 10:00", "UTC", now=now) == datetime(2026, 4, 1, 10, 0, tzinfo=UTC)


def test_dst_transition_keeps_wall_clock() -> None:
    """Berlin DST starts 2026-03-29; a daily 09:00 reminder keeps 09:00 local."""
    before = parse_local_datetime("2026-03-28 09:00", "Europe/Berlin")
    nxt = next_occurrence(before, "daily", "Europe/Berlin")
    assert nxt is not None
    assert to_local(nxt, "Europe/Berlin").hour == 9
    assert nxt - before == timedelta(hours=23)  # clocks moved forward


def test_recurrence_parsing() -> None:
    assert parse_recurrence("daily") == "daily"
    assert parse_recurrence("هر روز") == "daily"
    assert parse_recurrence("weekly") == "weekly"
    assert parse_recurrence("every 2h") == "interval:7200"
    assert parse_recurrence("saturday") == "weekly:5"
    assert parse_recurrence("none") is None
    assert parse_recurrence("nonsense") is None
    assert parse_recurrence("every 5s") is None  # too aggressive


def test_next_occurrence_variants() -> None:
    base = datetime(2026, 3, 20, 6, 0, tzinfo=UTC)  # Friday
    assert next_occurrence(base, "daily", "UTC") == base + timedelta(days=1)
    weekdays = next_occurrence(base, "weekdays", "UTC")
    assert weekdays is not None and weekdays.weekday() < 5
    weekends = next_occurrence(base, "weekends", "UTC")
    assert weekends is not None and weekends.weekday() >= 5
    saturday = next_occurrence(base, "weekly:5", "UTC")
    assert saturday is not None and saturday.weekday() == 5
    assert next_occurrence(base, "interval:120", "UTC") == base + timedelta(seconds=120)
    assert next_occurrence(base, None, "UTC") is None


def test_quiet_hours_window_across_midnight() -> None:
    late = datetime(2026, 3, 20, 23, 30, tzinfo=UTC)
    early = datetime(2026, 3, 20, 6, 0, tzinfo=UTC)
    midday = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
    assert in_quiet_hours(late, "23:00", "07:00", "UTC") is True
    assert in_quiet_hours(early, "23:00", "07:00", "UTC") is True
    assert in_quiet_hours(midday, "23:00", "07:00", "UTC") is False
    assert in_quiet_hours(midday, "bad", "values", "UTC") is False


def test_day_and_week_bounds() -> None:
    start, end = day_bounds(date(2026, 3, 20), "Asia/Tehran")
    assert (end - start).total_seconds() == 86400
    week_start, week_end = week_bounds(date(2026, 3, 20), "UTC")
    assert week_start.weekday() == 0
    assert (week_end - week_start).days == 7


def test_humanize_and_format() -> None:
    assert "2h" in humanize_delta(timedelta(hours=2))
    assert "in" in humanize_delta(timedelta(minutes=5))
    fa = humanize_delta(timedelta(hours=2), "fa")
    assert "ساعت" in fa
    assert "۲" in fa
    # Persian output uses the Jalali calendar with Persian digits.
    formatted = format_datetime(datetime(2026, 3, 20, 6, 0, tzinfo=UTC), "Asia/Tehran", "fa")
    assert "۱۴۰۴/۱۲/۲۹" in formatted
    assert "جمعه" in formatted


def test_gregorian_to_jalali_known_dates() -> None:
    from havoc.utils.time import gregorian_to_jalali

    assert gregorian_to_jalali(2026, 3, 21) == (1405, 1, 1)
    assert gregorian_to_jalali(2024, 1, 1) == (1402, 10, 11)
    assert gregorian_to_jalali(1979, 2, 11) == (1357, 11, 22)


def test_to_utc_treats_naive_as_utc() -> None:
    naive = datetime(2026, 1, 1, 12, 0)
    assert to_utc(naive).tzinfo == UTC
