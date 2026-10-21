import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from zymptom.domain import EpisodeOutcome, HelpLevel, ResumeAbility, ValidationError
from zymptom.domain.validation import deserialize_timestamp, serialize_timestamp


@pytest.mark.parametrize(
    ("enum_type", "expected"),
    [
        (EpisodeOutcome, [
            "completed_normally", "completed_with_difficulty", "completed_with_adaptation",
            "completed_with_help", "stopped_early", "unable_to_complete", "avoided_due_to_limitation",
        ]),
        (HelpLevel, ["none", "some", "significant", "other_person_completed_part", "other_person_completed_all"]),
        (ResumeAbility, ["immediately", "after_short_break", "later_same_day", "next_day", "could_not_resume", "unknown"]),
    ],
)
def test_stable_enum_values_and_serialization(enum_type, expected):
    assert [member.value for member in enum_type] == expected
    for member in enum_type:
        serialized = json.dumps(member)
        assert json.loads(serialized) == member.value
        assert enum_type(json.loads(serialized)) is member


def test_timestamp_round_trip_preserves_instant_and_microseconds():
    value = datetime(2026, 10, 20, 23, 1, 2, 345678, tzinfo=timezone(timedelta(hours=-7)))
    serialized = serialize_timestamp(value)
    assert serialized == "2026-10-21T06:01:02.345678+00:00"
    assert deserialize_timestamp(serialized) == value
    assert deserialize_timestamp(serialized).tzinfo is UTC


@pytest.mark.parametrize("value", ["invalid", "2026-10-21", "2026-10-21T12:34:56", None])
def test_invalid_or_naive_serialized_timestamps_rejected(value):
    with pytest.raises(ValidationError):
        deserialize_timestamp(value)
