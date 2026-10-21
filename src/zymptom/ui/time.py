"""The local-display/UTC boundary; Qt's naive Python conversions are avoided."""

from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QDateTime, QTimeZone

from zymptom.domain.validation import normalize_timestamp

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_local(value: datetime, zone: QTimeZone | None = None) -> QDateTime:
    delta = normalize_timestamp(value) - _EPOCH
    milliseconds = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    return QDateTime.fromMSecsSinceEpoch(milliseconds, zone or QTimeZone.systemTimeZone())


def to_utc(value: QDateTime) -> datetime:
    if not value.isValid():
        raise ValueError("Please choose a valid date and time.")
    return _EPOCH + timedelta(milliseconds=value.toMSecsSinceEpoch())
