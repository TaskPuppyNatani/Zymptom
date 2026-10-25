"""The local-display/UTC boundary; Qt's naive Python conversions are avoided."""

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6.QtCore import QDate, QDateTime, QTimeZone

from zymptom.domain.validation import normalize_timestamp

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_local(value: datetime, zone: QTimeZone | None = None) -> QDateTime:
    delta = normalize_timestamp(value) - _EPOCH
    milliseconds = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    return QDateTime.fromMSecsSinceEpoch(milliseconds, zone or QTimeZone.systemTimeZone())


def to_local_datetime(value: datetime, zone: QTimeZone | None = None) -> datetime:
    """Convert the aware source instant to Python local time, retaining DST rules."""
    instant = normalize_timestamp(value)
    zone = zone or QTimeZone.systemTimeZone()
    if not zone.isValid():
        raise ValueError("Please choose a valid time zone.")
    try:
        local_zone = ZoneInfo(bytes(zone.id()).decode("utf-8"))
    except ZoneInfoNotFoundError:
        # Qt also supports constant-offset zones without an IANA identifier.
        # Only those zones can safely use a fixed Python offset.
        if zone.hasTransitions() or zone.hasDaylightTime():
            raise
        local_zone = timezone(timedelta(seconds=zone.offsetFromUtc(to_local(instant, zone))))
    return instant.astimezone(local_zone)


def to_utc(value: QDateTime) -> datetime:
    if not value.isValid():
        raise ValueError("Please choose a valid date and time.")
    return _EPOCH + timedelta(milliseconds=value.toMSecsSinceEpoch())


def local_date_bounds(
    start: QDate | None, end: QDate | None, zone: QTimeZone,
) -> tuple[datetime | None, datetime | None]:
    """Convert inclusive local dates to a half-open UTC interval.

    Advancing the calendar date, rather than adding 24 hours, handles DST days.
    Qt's startOfDay also handles zones whose transitions occur at midnight.
    """
    if (start is not None and not start.isValid()) or (end is not None and not end.isValid()):
        raise ValueError("Please choose valid dates.")
    if start is not None and end is not None and start > end:
        raise ValueError("Start date must be on or before end date.")
    lower = to_utc(start.startOfDay(zone)) if start is not None else None
    upper = to_utc(end.addDays(1).startOfDay(zone)) if end is not None else None
    return lower, upper
