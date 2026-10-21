"""Validation shared by domain construction and persistence boundaries."""

from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

from .enums import EpisodeOutcome, HelpLevel, ResumeAbility

if TYPE_CHECKING:
    from .models import FunctionalEpisode

EnumT = TypeVar("EnumT", bound=Enum)


class ValidationError(ValueError):
    """A functional episode contains an invalid value."""


def parse_id(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ValidationError("id must be a UUID or UUID string") from exc
    raise ValidationError("id must be a UUID or UUID string")


def parse_enum(enum_type: type[EnumT], value: object, field_name: str) -> EnumT:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a valid {enum_type.__name__}") from exc


def normalize_timestamp(value: datetime, field_name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be a timezone-aware datetime")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValidationError(f"{field_name} cannot be represented in UTC") from exc


def serialize_timestamp(value: datetime) -> str:
    """Use a fixed-width UTC representation that also sorts chronologically."""
    return normalize_timestamp(value).isoformat(timespec="microseconds")


def deserialize_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("timestamp must be a valid ISO 8601 datetime") from exc
    return normalize_timestamp(parsed)


def _validate_minutes(value: int | None, field_name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValidationError(f"{field_name} must be a non-negative integer or None")


def validate_episode(episode: "FunctionalEpisode") -> None:
    """Revalidate mutable records before writes as well as at construction."""
    if not isinstance(episode.id, UUID):
        raise ValidationError("id must be a UUID")
    if not isinstance(episode.activity, str) or not episode.activity.strip():
        raise ValidationError("activity must not be blank")
    if not isinstance(episode.outcome, EpisodeOutcome):
        raise ValidationError("outcome must be a valid EpisodeOutcome")
    for field_name, enum_type in (
        ("help_level", HelpLevel),
        ("resume_ability", ResumeAbility),
    ):
        value = getattr(episode, field_name)
        if value is not None and not isinstance(value, enum_type):
            raise ValidationError(f"{field_name} must be a valid {enum_type.__name__} or None")
    _validate_minutes(episode.duration_minutes, "duration_minutes")
    _validate_minutes(episode.recovery_minutes, "recovery_minutes")
    if episode.recovery_required is not None and type(episode.recovery_required) is not bool:
        raise ValidationError("recovery_required must be a bool or None")
    for field_name in (
        "functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions"
    ):
        tags = getattr(episode, field_name)
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) or not tag.strip() for tag in tags
        ):
            raise ValidationError(f"{field_name} must be a list of non-blank strings")
    for field_name in ("symptom_notes", "help_notes", "notes"):
        value = getattr(episode, field_name)
        if value is not None and not isinstance(value, str):
            raise ValidationError(f"{field_name} must be a string or None")
    for field_name in ("occurred_at", "created_at", "updated_at"):
        normalize_timestamp(getattr(episode, field_name), field_name)
