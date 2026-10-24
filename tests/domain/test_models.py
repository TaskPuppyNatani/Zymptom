from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility, ValidationError
from zymptom.domain.validation import unique_tags


def make_episode(**details):
    return FunctionalEpisode(activity="Homework", outcome=EpisodeOutcome.STOPPED_EARLY, **details)


def test_valid_episode(full_episode):
    assert full_episode.activity == "Homework — reading and writing"
    assert full_episode.duration_minutes == 32
    assert isinstance(full_episode.id, UUID)
    assert full_episode.created_at.tzinfo is UTC
    assert full_episode.updated_at.tzinfo is UTC


@pytest.mark.parametrize("activity", ["", " ", "\t\n", None, 42])
def test_activity_must_not_be_blank(activity):
    with pytest.raises(ValidationError, match="activity"):
        FunctionalEpisode(activity=activity, outcome=EpisodeOutcome.COMPLETED_NORMALLY)


@pytest.mark.parametrize("field", ["duration_minutes", "recovery_minutes"])
@pytest.mark.parametrize("value", [-1, 1.5, True, "5"])
def test_minutes_must_be_nonnegative_integers(field, value):
    with pytest.raises(ValidationError, match=field):
        make_episode(**{field: value})


def test_zero_minutes_and_false_are_valid():
    episode = make_episode(duration_minutes=0, recovery_minutes=0, recovery_required=False)
    assert episode.duration_minutes == episode.recovery_minutes == 0
    assert episode.recovery_required is False


def test_optional_fields():
    episode = make_episode()
    for field in (
        "duration_minutes", "symptom_notes", "help_level", "help_notes",
        "recovery_required", "recovery_minutes", "resume_ability", "notes",
    ):
        assert getattr(episode, field) is None
    for field in ("functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions"):
        assert getattr(episode, field) == []


def test_collection_defaults_are_not_shared():
    first, second = make_episode(), make_episode()
    for field in ("functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions"):
        getattr(first, field).append("tag")
        assert getattr(second, field) == []
    assert first.id != second.id


def test_unique_tags_preserves_first_occurrence_and_exact_text():
    values = ["walking", "pain", "walking", "雪 ☕", "Pain", "pain"]
    assert unique_tags(values) == ["walking", "pain", "雪 ☕", "Pain"]
    assert values == ["walking", "pain", "walking", "雪 ☕", "Pain", "pain"]


@pytest.mark.parametrize("field", ["functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions"])
def test_domain_construction_keeps_legacy_duplicates_for_faithful_reads(field):
    episode = make_episode(**{field: ["second", "first", "second"]})
    assert getattr(episode, field) == ["second", "first", "second"]


def test_string_values_are_converted_to_enums_and_uuid():
    episode = FunctionalEpisode(
        id="00000000-0000-0000-0000-000000000001",
        activity="Homework",
        outcome="stopped_early",
        help_level="none",
        resume_ability="unknown",
    )
    assert episode.id == UUID(int=1)
    assert episode.outcome is EpisodeOutcome.STOPPED_EARLY
    assert episode.help_level is HelpLevel.NONE
    assert episode.resume_ability is ResumeAbility.UNKNOWN


@pytest.mark.parametrize("field", ["outcome", "help_level", "resume_ability"])
@pytest.mark.parametrize("value", ["display label", "", 1])
def test_invalid_enum_rejected(field, value):
    details = {"activity": "Homework", "outcome": EpisodeOutcome.STOPPED_EARLY, field: value}
    with pytest.raises(ValidationError, match=field):
        FunctionalEpisode(**details)


@pytest.mark.parametrize("value", [None, "not-a-uuid", 42])
def test_invalid_id_rejected(value):
    with pytest.raises(ValidationError, match="id"):
        make_episode(id=value)


@pytest.mark.parametrize("field", ["occurred_at", "created_at", "updated_at"])
def test_naive_timestamps_are_rejected(field):
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_episode(**{field: datetime(2026, 10, 20)})


def test_timezone_offsets_normalize_to_utc():
    original = datetime(2026, 10, 20, 23, 45, 0, 123456, tzinfo=timezone(timedelta(hours=-7)))
    episode = make_episode(occurred_at=original, created_at=original, updated_at=original)
    expected = datetime(2026, 10, 21, 6, 45, 0, 123456, tzinfo=UTC)
    for field in ("occurred_at", "created_at", "updated_at"):
        assert getattr(episode, field) == expected
        assert getattr(episode, field).tzinfo is UTC


@pytest.mark.parametrize("field", ["functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions"])
@pytest.mark.parametrize("value", [[""], ["\t "], [123], "tag", None])
def test_tag_collections_are_lists_of_nonblank_strings(field, value):
    with pytest.raises(ValidationError, match=field):
        make_episode(**{field: value})


@pytest.mark.parametrize("field", ["symptom_notes", "help_notes", "notes"])
def test_optional_notes_require_text(field):
    with pytest.raises(ValidationError, match=field):
        make_episode(**{field: 1})


@pytest.mark.parametrize("value", [0, 1, "yes"])
def test_recovery_flag_requires_bool(value):
    with pytest.raises(ValidationError, match="recovery_required"):
        make_episode(recovery_required=value)
