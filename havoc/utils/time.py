"""Time parsing, timezone handling and human-friendly formatting.

All persisted timestamps are timezone-aware UTC ISO-8601 strings with second
resolution, e.g. ``2026-10-28T09:15:00+00:00``. Conversion to the user's
timezone happens only at the edges (display and scheduling input) so DST is
handled by :mod:`zoneinfo` rather than arithmetic on naive datetimes.

Feature areas: 51-53, 58, 60 (schedule validation), 78.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..localization import digits_for

UTC = UTC

_DURATION_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w|ماه|ماهه|روز|هفته|ساعت|دقیقه|ثانیه|ث)",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "ثانیه": 1, "ث": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "دقیقه": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "ساعت": 3600,
    "d": 86400, "day": 86400, "days": 86400, "روز": 86400,
    "w": 604800, "week": 604800, "weeks": 604800, "هفته": 604800,
}
_WEEKDAYS = {
    "mon": 0, "monday": 0, "دوشنبه": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "سه‌شنبه": 1, "سه شنبه": 1,
    "wed": 2, "wednesday": 2, "چهارشنبه": 2,
    "thu": 3, "thurs": 3, "thursday": 3, "پنجشنبه": 3, "پنج‌شنبه": 3,
    "fri": 4, "friday": 4, "جمعه": 4,
    "sat": 5, "saturday": 5, "شنبه": 5,
    "sun": 6, "sunday": 6, "یکشنبه": 6, "یک‌شنبه": 6,
}

PERSIAN_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)

MAX_DURATION_SECONDS = 10 * 365 * 86400


class TimeParseError(ValueError):
    """Raised when a duration/date cannot be understood."""


# ---------------------------------------------------------------------------
# Now / conversion
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def get_zone(name: str | None, default: str = "UTC") -> ZoneInfo:
    """Resolve an IANA timezone, raising a clear error for bad input."""
    candidate = (name or default or "UTC").strip()
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TimeParseError(f"unknown timezone {candidate!r}") from exc


def safe_zone(name: str | None, default: str = "UTC") -> ZoneInfo:
    try:
        return get_zone(name, default)
    except TimeParseError:
        return get_zone(default)




def to_local(dt: datetime, tz_name: str | None) -> datetime:
    return to_utc(dt).astimezone(safe_zone(tz_name))


def iso(dt: datetime) -> str:
    return to_utc(dt).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return to_utc(dt)


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------
def parse_duration(text: str) -> timedelta:
    """Parse ``30m``, ``2 hours``, ``1h30m``, ``۴۵ دقیقه`` into a timedelta."""
    if not text or not text.strip():
        raise TimeParseError("empty duration")
    normalized = _normalize_digits(text.strip().lower())
    if normalized.startswith(("-", "+")) or normalized.startswith("−"):
        raise TimeParseError("duration must be positive")
    # bare number means minutes (shortcut for ".remind 30 water")
    if re.fullmatch(r"\d+(?:[.,]\d+)?", normalized):
        minutes = float(normalized.replace(",", "."))
        return timedelta(minutes=minutes)

    total = 0.0
    matched = 0
    for match in _DURATION_RE.finditer(normalized):
        raw_value = match.group("value").replace(",", ".")
        unit = match.group("unit").lower().rstrip("s") if len(match.group("unit")) > 1 else match.group("unit").lower()
        seconds = _UNIT_SECONDS.get(unit) or _UNIT_SECONDS.get(match.group("unit").lower())
        if seconds is None:
            continue
        total += float(raw_value) * seconds
        matched += 1
    if not matched:
        raise TimeParseError("no recognizable duration (e.g. 30m, 2h, 1d)")
    # Reject "5m bananas" style input: every alphanumeric must be accounted for.
    consumed = _DURATION_RE.sub("", normalized)
    if re.search(r"[0-9a-z\u0600-\u06FF]", consumed):
        raise TimeParseError("unexpected characters in duration")
    if total <= 0:
        raise TimeParseError("duration must be positive")
    if total > MAX_DURATION_SECONDS:
        raise TimeParseError("duration is unreasonably long")
    return timedelta(seconds=total)


def _normalize_digits(text: str) -> str:
    from .text import to_latin_digits

    return to_latin_digits(text)


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_clock(text: str) -> time:
    match = _TIME_RE.match(_normalize_digits(text.strip()))
    if not match:
        raise TimeParseError("time must look like HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise TimeParseError("time is out of range")
    return time(hour, minute, second)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
_DATE_PATTERNS = (
    ("%Y-%m-%d", r"^\d{4}-\d{1,2}-\d{1,2}$"),
    ("%Y/%m/%d", r"^\d{4}/\d{1,2}/\d{1,2}$"),
    ("%d.%m.%Y", r"^\d{1,2}\.\d{1,2}\.\d{4}$"),
    ("%d/%m/%Y", r"^\d{1,2}/\d{1,2}/\d{4}$"),
)


#: "remind me friday", "شنبه": the weekday in both scripts, 0 = Monday.
_WEEKDAYS = {
    "monday": 0, "mon": 0, "دوشنبه": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "سه‌شنبه": 1, "سه شنبه": 1,
    "wednesday": 2, "wed": 2, "چهارشنبه": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "پنجشنبه": 3, "پنج‌شنبه": 3,
    "friday": 4, "fri": 4, "جمعه": 4,
    "saturday": 5, "sat": 5, "شنبه": 5,
    "sunday": 6, "sun": 6, "یکشنبه": 6, "یک‌شنبه": 6,
}


def days_until_weekday(token: str, today: date) -> int | None:
    """How many days ahead the next ``token`` weekday is; ``None`` if not one."""
    index = _WEEKDAYS.get(token.strip().lower())
    if index is None:
        return None
    ahead = (index - today.weekday()) % 7
    # "friday" said on a Friday means *today*, unless the time has gone — the
    # caller decides that, because only it knows the clock.
    return ahead


def parse_date(text: str) -> date:
    normalized = _normalize_digits(text.strip())
    for fmt, pattern in _DATE_PATTERNS:
        if re.fullmatch(pattern, normalized):
            try:
                return datetime.strptime(normalized, fmt).date()
            except ValueError as exc:
                raise TimeParseError(f"invalid date: {exc}") from exc
    raise TimeParseError("date must look like YYYY-MM-DD or DD.MM.YYYY")




def parse_local_datetime(
    text: str,
    tz_name: str | None = "UTC",
    *,
    now: datetime | None = None,
) -> datetime:
    """Parse a human date/time in ``tz_name`` and return an aware UTC datetime.

    Accepted forms: ``2026-01-02 14:30``, ``02.01.2026 14:30``, ``14:30``
    (today), ``tomorrow 09:00``, ``فردا ۹:۰۰``.
    """
    zone = safe_zone(tz_name)
    current = (now or utc_now()).astimezone(zone)
    normalized = _normalize_digits(" ".join(text.strip().split()))
    lowered = normalized.lower()

    day_offset = 0
    if lowered.startswith("tomorrow") or lowered.startswith("فردا"):
        day_offset = 1
        normalized = re.sub(r"(?i)^(tomorrow|فردا)\s*", "", normalized)
    elif lowered.startswith("today") or lowered.startswith("امروز"):
        normalized = re.sub(r"(?i)^(today|امروز)\s*", "", normalized)
    elif lowered.startswith("yesterday") or lowered.startswith("دیروز"):
        day_offset = -1
        normalized = re.sub(r"(?i)^(yesterday|دیروز)\s*", "", normalized)

    parts = normalized.split()
    target_date: date | None = None
    target_time: time | None = None
    matched = False
    weekday_used = False
    for part in parts:
        if target_time is None:
            try:
                target_time = parse_clock(part)
                matched = True
                continue
            except TimeParseError:
                pass
        if target_date is None:
            ahead = days_until_weekday(part, current.date() + timedelta(days=day_offset))
            if ahead is not None:
                # "friday at 18:30" is the next Friday, not today with a stray
                # word in it. On the day itself the clock decides — see below.
                target_date = current.date() + timedelta(days=day_offset + ahead)
                weekday_used = True
                day_offset = 0
                matched = True
                continue
        if target_date is None:
            try:
                target_date = parse_date(part)
                matched = True
                continue
            except TimeParseError:
                pass
    if not matched and not day_offset:
        # "abcdef" is neither a duration nor a date; guessing 09:00 would be a
        # silent lie, so refuse instead.
        raise TimeParseError("unrecognised time; use 30m, 14:30 or 2026-01-02 14:30")
    if target_date is None:
        target_date = current.date() + timedelta(days=day_offset)
    if target_time is None:
        target_time = time(9, 0) if parts else current.time().replace(second=0, microsecond=0)

    naive = datetime.combine(target_date, target_time)
    aware = naive.replace(tzinfo=zone)
    if weekday_used and aware <= current:
        # Said on the day itself, after the time has gone: next week's.
        aware += timedelta(days=7)
    return to_utc(aware)


_AMPM = re.compile(r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<mark>am|pm)$", re.I)


def spoken_when(text: str) -> str:
    """The time as people say it, as the parser wants it.

    ``in 2 hours`` → ``2h``, ``9am`` → ``09:00``, ``7:30 pm`` → ``19:30``. Both
    spellings are what he types; neither used to parse, so a reminder he asked
    for was answered with a syntax error.
    """
    folded = _normalize_digits(" ".join(str(text or "").strip().split())).lower()
    folded = re.sub(r"^(?:in|after|within|بعد\s*از|تا)\s+", "", folded).strip()
    match = _AMPM.match(folded)
    if match:
        hour = int(match.group("hour")) % 12
        minute = int(match.group("minute") or 0)
        if match.group("mark").lower() == "pm":
            hour += 12
        return f"{hour:02d}:{minute:02d}"
    # "tomorrow 9am", "today 7:30 pm"
    head, _, tail = folded.rpartition(" ")
    tail_match = _AMPM.match(tail)
    if head and tail_match:
        return f"{head} {spoken_when(tail)}"
    return folded


def parse_when(text: str, tz_name: str | None = "UTC", *, now: datetime | None = None) -> datetime:
    """Parse either a relative duration (``30m``) or an absolute local time."""
    current = now or utc_now()
    spoken = spoken_when(text)
    try:
        return current + parse_duration(spoken)
    except TimeParseError:
        return parse_local_datetime(spoken, tz_name, now=current)


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------
_RECURRENCE_PATTERNS = {
    "daily": r"^(?:daily|every day|هر\s*روز|روزانه)$",
    "weekly": r"^(?:weekly|every week|هر\s*هفته|هفتگی)$",
    "monthly": r"^(?:monthly|every month|هر\s*ماه|ماهانه)$",
}


def parse_recurrence(text: str | None) -> str | None:
    """Normalize a recurrence expression to a canonical spec.

    Supported: ``none``, ``daily``, ``weekly``, ``monthly``, ``every 30m``,
    ``weekdays``, ``sat``/``saturday``... and Persian equivalents.
    Returns ``None`` for one-off jobs.
    """
    if not text:
        return None
    normalized = _normalize_digits(" ".join(text.strip().split())).lower()
    if normalized in {"none", "once", "off", "هیچ", "یکبار", "یک‌بار"}:
        return None
    for canonical, pattern in _RECURRENCE_PATTERNS.items():
        if re.fullmatch(pattern, normalized):
            return canonical
    if normalized in {"weekdays", "weekday", "روزهای هفته"}:
        return "weekdays"
    if normalized in {"weekends", "weekend", "آخر هفته"}:
        return "weekends"
    if normalized in _WEEKDAYS:
        return f"weekly:{_WEEKDAYS[normalized]}"
    if normalized.startswith("every ") or normalized.startswith("هر "):
        rest = re.sub(r"^(every|هر)\s+", "", normalized)
        if rest.lower() in _WEEKDAYS:
            return f"weekly:{_WEEKDAYS[rest.lower()]}"
        try:
            delta = parse_duration(rest)
        except TimeParseError:
            return None
        if delta.total_seconds() < 60:
            return None
        return f"interval:{int(delta.total_seconds())}"
    return None


def next_occurrence(current: datetime, recurrence: str | None, tz_name: str | None = "UTC") -> datetime | None:
    """Compute the next fire time for a recurrence spec.

    Recurrences are computed in local time and converted back to UTC, so a
    daily reminder keeps its wall-clock time across DST changes.
    """
    if not recurrence:
        return None
    zone = safe_zone(tz_name)
    local = to_local(current, tz_name)
    if recurrence == "daily":
        candidate = local + timedelta(days=1)
    elif recurrence == "weekdays":
        candidate = local + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    elif recurrence == "weekends":
        candidate = local + timedelta(days=1)
        while candidate.weekday() < 5:
            candidate += timedelta(days=1)
    elif recurrence == "weekly":
        candidate = local + timedelta(weeks=1)
    elif recurrence.startswith("weekly:"):
        try:
            target = int(recurrence.split(":", 1)[1])
        except ValueError:
            return None
        candidate = local + timedelta(days=1)
        while candidate.weekday() != target:
            candidate += timedelta(days=1)
    elif recurrence == "monthly":
        candidate = _add_month(local)
    elif recurrence.startswith("interval:"):
        try:
            seconds = int(recurrence.split(":", 1)[1])
        except ValueError:
            return None
        if seconds < 60:
            return None
        candidate = local + timedelta(seconds=seconds)
    else:
        return None
    return to_utc(candidate.replace(tzinfo=zone))


def _add_month(dt: datetime) -> datetime:
    month = dt.month + 1
    year = dt.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(dt.day, _days_in_month(year, month))
    return dt.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def humanize_delta(delta: timedelta, language: str = "en") -> str:
    """Compact relative time, e.g. ``in 2h 15m`` / ``۲ ساعت و ۱۵ دقیقه دیگر``."""
    seconds = int(abs(delta.total_seconds()))
    negative = delta.total_seconds() < 0
    units = (
        (86400, ("d", "روز")),
        (3600, ("h", "ساعت")),
        (60, ("m", "دقیقه")),
        (1, ("s", "ثانیه")),
    )
    if seconds < 5:
        return "just now" if language == "en" else "همین حالا"
    parts: list[str] = []
    remaining = seconds
    for size, (en_suffix, fa_name) in units:
        amount, remaining = divmod(remaining, size)
        if amount:
            if language == "fa":
                parts.append(f"{digits_for('fa', amount)} {fa_name}")
            else:
                parts.append(f"{amount}{en_suffix}")
        if len(parts) == 2:
            break
    joiner = " و " if language == "fa" else " "
    body = joiner.join(parts)
    if language == "fa":
        return f"{body} پیش" if negative else f"{body} دیگر"
    return f"{body} ago" if negative else f"in {body}"


def format_datetime(dt: datetime, tz_name: str | None = "UTC", language: str = "en") -> str:
    """Human readable absolute time in the user's zone and language.

    Farsi output uses the Jalali (Solar Hijri) calendar and Persian digits,
    which is what Iranian users expect from a Persian interface.
    """
    local = to_local(dt, tz_name)
    if language == "fa":
        jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
        stamp = f"{jy:04d}/{jm:02d}/{jd:02d} {local:%H:%M}"
        weekday = _fa_weekday(local.weekday())
        return f"{digits_for('fa', stamp)} ({weekday})"
    return local.strftime("%Y-%m-%d %H:%M %Z").strip()


def format_short(dt: datetime, tz_name: str | None = "UTC", language: str = "en") -> str:
    """Just enough time for a list: the clock today, the date before that.

    An inbox line with a full date, a weekday and a zone is three facts where
    one is wanted — he cannot scan it.
    """
    local = to_local(dt, tz_name)
    now = to_local(utc_now(), tz_name)
    if local.date() == now.date():
        clock = local.strftime("%H:%M")
        return digits_for(language, clock) if language == "fa" else clock
    if local.year == now.year:
        stamp = local.strftime("%m/%d %H:%M")
        return digits_for(language, stamp) if language == "fa" else stamp
    stamp = local.strftime("%Y-%m-%d")
    return digits_for(language, stamp) if language == "fa" else stamp


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert a Gregorian date to the Jalali (Solar Hijri) calendar."""
    g_days_in_month = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + 365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        + gd
        + sum(g_days_in_month[: gm - 1])
    )
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def _fa_weekday(index: int) -> str:
    names = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
    return names[index % 7]


def day_bounds(day: date, tz_name: str | None = "UTC") -> tuple[datetime, datetime]:
    zone = safe_zone(tz_name)
    start = datetime.combine(day, time(0, 0), tzinfo=zone)
    end = start + timedelta(days=1)
    return to_utc(start), to_utc(end)


def week_bounds(day: date, tz_name: str | None = "UTC") -> tuple[datetime, datetime]:
    """Monday-to-Sunday week bounds (seven days, exclusive end).

    The Iranian week starts on Saturday, so callers that display a calendar
    label the first column themselves; the arithmetic stays unambiguous.
    """
    start_day = day - timedelta(days=day.weekday())
    start, _ = day_bounds(start_day, tz_name)
    _, end = day_bounds(start_day + timedelta(days=6), tz_name)
    return start, end


def in_quiet_hours(moment: datetime, quiet_start: str, quiet_end: str, tz_name: str | None = "UTC") -> bool:
    """Whether ``moment`` falls inside a quiet window that may cross midnight."""
    local = to_local(moment, tz_name).time()
    try:
        start = parse_clock(quiet_start)
        end = parse_clock(quiet_end)
    except TimeParseError:
        return False
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


